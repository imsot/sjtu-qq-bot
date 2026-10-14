"""知途好课(sjtu.aihaoke.net)任务/作业监控：新任务 + 截止提醒私聊推送主人。

依赖：plugins/aihaoke.json 里的 token。该 token 是**会话 cookie 的值**
(名为 `haoke-token`，**HttpOnly**，所以网页里的 JS 读不到它)，通过
F12 → Network 里某条 /api/ 请求的 `Authorization: Bearer xxx` 头复制最方便。
**token 的续期由浏览器用户脚本自动完成**（见下）。

流程：每 POLL_INTERVAL 秒轮询指定课程的任务列表 → 与 .aihaoke_state.json
去重比对 → 首次发一份待办汇总(不逐条刷屏) → 之后只推增量。
另有独立的时间驱动截止提醒：提前 24h / 2h / 每晚汇总明日截止。

主人命令：#好课状态 / #好课检查 / #好课课程 / #好课同步

设计要点(实测得来,勿改)：
- **所有业务接口都是 POST**(body 当查询参数),没有 GET 版本。本插件只调查询
  接口,绝不调用 saveSubmit / removeMyClass 等写接口。
- **HTTP 状态码恒为 200**,业务码在响应体 `code` 字段。未认证时 HTTP 仍是 200
  但 `code=401`。所以不能看状态码判错。
- 每个 body 要带 uuid 的 `requestId`（漏了会报 code500"请求标识不能为空"）。
- **`endTime` 有哨兵值** `9999-12-31 23:59:59` 表示"无截止时间",`beginTime`
  的 `1900-01-01` 同理。必须过滤,否则会把"还剩 290 万年"当真提醒。

- **续期：平台根本没有续期机制**（2026-10-14 查明，别再把 auto_refresh 打开）。
  证据链：①前端源码里 `tokenLogin` 要的"票"是 cookie `haoke-auto-login`
  （`Co=()=>$t.get(Gt.AUTO_LOGIN)`），而**用户浏览器里根本没这张 cookie**，
  且前端的两处续期都写着 `if (Co())`、唯一的写入点又只在续期成功时调用——自己
  造不出来；②`queryCurrentUserInfo` 响应里的 `ticket` 字段是**一次性**的，被
  `tokenLogin` 用掉就废，接口之后永远返回同一张废票（实测：body 带、cookie 带、
  两者都带，全都 `code500(用户登录信息已失效)`）；③那次"续期成功"响应回的 token
  与送进去的**完全相同**，会话并没被延长。所以每次登录只能白烧一张票，之后必然
  失败（10-06 起静默失败 249 次就是这么来的）。**结论：唯一可靠的凭证来源是
  浏览器本身，改由用户脚本推送**（`tools` 见下）。
- **浏览器自动同步(2026-10-14 上线)**：本插件在 127.0.0.1:37219 起了个小 HTTP
  服务，`GET /aihaoke_sync.user.js` 下发预先填好端口/密钥的用户脚本，脚本把 token
  POST 到 `/token`。**关键设计**：好课 token 是 HttpOnly，脚本读不到，所以脚本
  的做法是**在页面里替调一次 `queryCurrentUserInfo`**（`credentials:'include'`
  让浏览器自动带上 HttpOnly cookie），响应里的 `data.token` 就是当前 token
  （实测：只带 cookie、不带 Authorization 头也能调通并原样返回）。安全三层：
  只绑回环 + `X-Sync-Key` 密钥 + CORS 只放行 sjtu.aihaoke.net（还要
  `Access-Control-Allow-Private-Network`，否则 Chrome 静默拦截）。落盘前
  **必须先真拉一次 listMyClass 验证**（同二课原则：存坏凭证比不存更糟）。
"""
import asyncio
import base64
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta

import aiohttp
from aiohttp import web

from bot import events
from bot.router import Plugin

log = logging.getLogger("aihaoke")

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PLUGIN_DIR, "aihaoke.json")
STATE_PATH = os.path.join(PLUGIN_DIR, ".aihaoke_state.json")

DEFAULT_BASE = "https://sjtu.aihaoke.net"
POLL_INTERVAL = 900            # 轮询间隔(秒)=15 分钟,与 Canvas 一致
AUTH_WARN_COOLDOWN = 6 * 3600  # 凭证失效提醒间隔(秒)
SEEN_CAP = 3000
# 续期连续失败的处理(2026-10-13 加)。背景:续期一旦坏掉,token 本身还能活好几天,
# 期间监控表面正常、保护机制却已失效——10-06 20:06 坏掉后静默失败 249 次(每 15 分钟
# 一次,刷满日志),直到 10-12 token 到期吃 401 才被发现。所以:连败几次就提前预警,
# 并退避到 6 小时一次,别再无意义地打接口。
REFRESH_FAIL_BACKOFF_AFTER = 4    # 连续失败达到此次数后进入退避
REFRESH_FAIL_BACKOFF_HOURS = 6.0  # 退避期间的重试间隔(小时)
RENEW_WARN_AFTER = 3              # 连续失败达到此次数就提醒主人换 token
MAX_TASKS_PER_COURSE = 5000    # 单课程任务数上限,防异常数据
SLEEP_BETWEEN_MSGS = 2
DAILY_SUMMARY_HOUR = 22
TODO_MAX_LINES = 20            # #好课检查 里最多列几条未完成的必做任务

# ---------- 浏览器用户脚本自动同步（2026-10-14 新增，替代已证伪的自动续期）----------
# 背景见文件头"续期"一节：平台对本站点根本不发续期钥匙（浏览器 cookie 里没有
# haoke-auto-login，前端那两处续期代码因此永远不会执行），所以"自动续期"在原理上
# 不成立——每次登录只能白烧掉一张一次性票。唯一可靠的凭证来源是浏览器本身，于是
# 由用户脚本在页面里取回 token 推给机器人。二课插件已用同一套机制跑通，这里照搬。
SYNC_STATE_PATH = os.path.join(PLUGIN_DIR, ".aihaoke_sync.json")
SYNC_PORT = 37219              # 本机监听端口(仅 127.0.0.1)，与二课的 37218 错开
SYNC_PATH = "/token"
SYNC_ALLOWED_ORIGIN = "https://sjtu.aihaoke.net"
SYNC_MIN_INTERVAL = 20         # 同一入口最小间隔(秒)，防本机别的程序反复打
SYNC_BIND_RETRIES = 20         # 端口被占时重试次数(重启竞态)
SYNC_BIND_RETRY_DELAY = 15     # 每次重试间隔(秒) -> 共重试 5 分钟
USERSCRIPT_FILE = "aihaoke_sync.user.js"

# 用户脚本模板。__PORT__ / __KEY__ 在服务端替换后下发。
#
# 与二课那版的**关键区别**：好课的 token 是 **HttpOnly cookie**，脚本里读不到它
# （二课那版直接读 cookie 的做法在这里行不通）。所以这里改走"在页面里替调用一次
# 接口"——`fetch(..., {credentials:'include'})` 会让浏览器自动带上 HttpOnly 的
# haoke-token，而 queryCurrentUserInfo 的响应里 data.token 正是当前会话的 token
# （2026-10-14 实测：只带 cookie、不带 Authorization 头也能调通并原样返回 token）。
USERSCRIPT = """// ==UserScript==
// @name         知途好课 token 自动同步
// @namespace    aihaoke-sync
// @version      1.0
// @description  把 sjtu.aihaoke.net 的登录 token 自动推给本机 QQ 机器人（端口/密钥已预填）
// @match        https://sjtu.aihaoke.net/*
// @run-at       document-idle
// @updateURL    http://127.0.0.1:__PORT__/aihaoke_sync.user.js
// @downloadURL  http://127.0.0.1:__PORT__/aihaoke_sync.user.js
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      127.0.0.1
// ==/UserScript==
(function () {
  'use strict';
  var PORT = __PORT__;
  var KEY  = '__KEY__';
  var ENDPOINT = 'http://127.0.0.1:' + PORT + '/token';
  var FPC = 'haokeSyncFp';        // 去重标记(存 token 的短哈希,不存 token 本身)
  var REPUSH_MS = 6 * 3600 * 1000; // 即使 token 没变,也每 6 小时重推一次(防机器人侧丢配置)
  var TAG = '[好课同步] ';
  // 带 @grant 时脚本跑在沙箱里,用 unsafeWindow 才拿得到页面真正的 fetch
  var W = (typeof unsafeWindow !== 'undefined') ? unsafeWindow : window;

  function readOwn(name) {
    var parts = (document.cookie || '').split(';');
    for (var i = 0; i < parts.length; i++) {
      var eq = parts[i].indexOf('=');
      if (eq > 0 && parts[i].slice(0, eq).trim() === name) {
        return parts[i].slice(eq + 1).trim();
      }
    }
    return '';
  }
  function writeOwn(name, val) {
    try {
      document.cookie = name + '=' + val + '; path=/; max-age=' + (30 * 86400) + '; SameSite=Lax';
    } catch (e) {}
  }
  function fp(t) {                       // djb2 短哈希:只为去重,不复制凭证
    var h = 5381;
    for (var i = 0; i < t.length; i++) { h = ((h * 33) ^ t.charCodeAt(i)) >>> 0; }
    return h.toString(16);
  }
  function uuid() {
    try { return crypto.randomUUID(); }
    catch (e) { return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
      var r = Math.random() * 16 | 0; return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16); }); }
  }

  // 在页面上下文里替调一次接口。关键就是 credentials:'include' ——
  // 浏览器会自动带上 HttpOnly 的 haoke-token，脚本自己读不到它。
  function fetchToken() {
    return W.fetch('/api/auth/queryCurrentUserInfo', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json', 'X-Language': 'zh-CN' },
      body: JSON.stringify({ requestId: uuid() })
    }).then(function (r) { return r.json(); }).then(function (o) {
      var d = o && o.data;
      var t = d && d.token;
      return (typeof t === 'string' && t.length > 100) ? t : null;
    }).catch(function () { return null; });
  }

  // 推送。**只有机器人明确回 ok 才记去重标记** —— 否则机器人临时没在跑时,
  // 脚本会以为自己推过了,要等 REPUSH_MS(6 小时)才重试,白白多停摆半天。
  function push(token, note, onOk) {
    GM_xmlhttpRequest({
      method: 'POST',
      url: ENDPOINT,
      headers: { 'Content-Type': 'application/json', 'X-Sync-Key': KEY },
      data: JSON.stringify({ token: token, ping: TAG + note }),
      onload: function (r) {
        var ok = false;
        try { ok = !!(JSON.parse(r.responseText || '{}').ok); } catch (e) {}
        console.log(TAG + '推送结果 ' + r.status + ' ' + r.responseText);
        if (ok && onOk) { onOk(); }
      },
      onerror: function () {
        console.warn(TAG + '推送失败:机器人没在跑,或端口 ' + PORT + ' 不通(下次进页面会重试)');
      }
    });
  }

  function run() {
    fetchToken().then(function (token) {
      if (!token) {
        // 没拿到 token 说明当前没登录 —— 报一次自检,方便在机器人日志里看到原因。
        // 诊断信息走 ping 通道,不占服务端限频。
        push('', '没取到 token(未登录?)');
        return;
      }
      var now = Date.now();
      var cur = readOwn(FPC) || '';
      var parts = cur.split(':');
      var lastFp = parts[0] || '', lastAt = parseInt(parts[1] || '0', 10) || 0;
      var h = fp(token);
      if (h === lastFp && (now - lastAt) < REPUSH_MS) { return; }   // 没变且不久,跳过
      push(token, (h === lastFp ? '定期重推' : 'token 有变化') + ' 长度' + token.length,
           function () { writeOwn(FPC, h + ':' + Date.now()); });
    });
  }

  run();                                        // 进页面就来一次
  setInterval(function () { if (!document.hidden) { run(); } }, 30 * 60 * 1000);
})();
"""

# 年份超出此区间视为"无截止时间"哨兵。平台用 9999-12-31 表示"无截止时间"、
# 1900-01-01 表示"无开始时间";两个值都不该被当成真截止(否则会推"还剩290万年"
# 或刷"已截止")。留 2000~9000 这个宽松但安全的区间。
MIN_SANE_YEAR = 2000
MAX_SANE_YEAR = 9000

# taskType 枚举,来自前端 bundle 原文
TASK_TYPE = {
    10: "视频", 20: "文档", 30: "AI伴读", 40: "角色模拟", 50: "练习",
    51: "自评练习", 60: "个性化练习", 70: "实验", 80: "写作", 90: "代码",
    100: "office", 110: "CAD", 120: "自适应学习", 130: "图文", 140: "PBL任务",
}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "X-Language": "zh-CN",
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
}


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _parse_dt(s):
    """解析平台的时间字符串。实测出现两种格式:
    '2026-09-19 23:59' 和 '2026-09-19 23:59:59'。返回 naive datetime(平台就是本地时间)。"""
    if not s or not isinstance(s, str):
        return None
    txt = s.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(txt, fmt)
        except ValueError:
            continue
    return None


def _has_deadline(dt) -> bool:
    """过滤掉 9999-12-31(无截止) / 1900-01-01(无开始) 这类哨兵日期。"""
    return dt is not None and MIN_SANE_YEAR <= dt.year < MAX_SANE_YEAR


def _fmt_dt(dt) -> str:
    return dt.strftime("%m-%d %H:%M") if dt else "未定"


def _remain(dt) -> str:
    if dt is None:
        return "无截止时间"
    secs = (dt - datetime.now()).total_seconds()
    if secs < 0:
        return "已截止"
    if secs >= 86400:
        return f"还剩 {int(secs // 86400)} 天"
    if secs >= 3600:
        return f"还剩 {int(secs // 3600)} 小时"
    return f"还剩 {int(secs // 60)} 分钟"


class AihaokePlugin(Plugin):
    COMMANDS = (
        ("#好课状态", "", True, "查看知途好课监控状态"),
        ("#好课检查", "", True, "立即检查一次，并列出未完成的必做任务"),
        ("#好课课程", "", True, "查看监测的课程及 classId"),
        ("#好课同步", "", True, "浏览器自动同步 token 的安装说明"),
    )

    def __init__(self):
        super().__init__()
        # 不依赖 router 的状态放这里,这样脱离 Router 也能单独测试
        self._auth_warned_at = 0.0
        self._checking = asyncio.Lock()
        self._token = None          # 当前 token(续期后会变)
        # 最近一次成功检查拿到的任务快照,供 #好课检查 列"未完成的必做任务"用。
        # 每次 _check_inner 开头清空、成功后才写入:失败时(如 401)绝不会拿旧数据
        # 冒充新结果。
        self._last_tasks = None
        self._last_names = {}
        # 用户脚本同步接口的状态(2026-10-14 加)
        self._last_sync_at = 0.0     # 上次收到脚本推送的时间(限频用)
        self._sync_up = False        # 同步接口是否成功起来了
        self._sync_error = ""        # 起不来时的原因(端口被占等)

    def setup(self, router):
        super().setup(router)
        self._task = asyncio.create_task(self._poll_loop())
        self._sync_task = asyncio.create_task(self._sync_server())

    async def _poll_loop(self):
        await asyncio.sleep(25)  # 等 WS 连接稳定后再做首次检查
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("轮询异常: %s", e)
            await asyncio.sleep(POLL_INTERVAL)

    # ---------- 一次完整检查 ----------
    async def check_once(self) -> str:
        if self._checking.locked():
            return "检查正在进行中,请稍候"
        async with self._checking:
            return await self._check_inner()

    async def _check_inner(self) -> str:
        cfg = _load_json(CONFIG_PATH, {})
        self._last_tasks = None      # 先清空快照,下面抓成功才填(见 setup 里的说明)
        self._last_names = {}
        if cfg.get("enabled") is False:
            return "已在 aihaoke.json 中关闭(enabled=false)"
        token = (cfg.get("token") or "").strip()
        if not token:
            return "未配置 token(aihaoke.json),功能停用中"
        self._token = token

        base = (cfg.get("base_url") or DEFAULT_BASE).rstrip("/")
        class_ids = [c for c in (cfg.get("courses") or []) if isinstance(c, int)]
        if not class_ids:
            return "未配置要监测的课程(courses),功能停用中"

        st = _load_json(STATE_PATH, {})
        seen = set(st.get("seen", []))
        names = {}

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as s:
            # 1. 验证凭证 + 顺手拿一个新鲜 ticket(供将来续期用)
            data, err = await self._post(s, base, "/api/auth/queryCurrentUserInfo", {})
            if err == "auth":
                data, err = await self._try_refresh(s, base, cfg, st)
                if err == "auth":
                    return await self._handle_error("auth", st, seen)
            if err is not None:
                return await self._handle_error(err, st, seen)
            self._capture_ticket(cfg, data, st)

            # 2. 课程名单(拿显示名 + 发现白名单失配)
            data, err = await self._post(s, base, "/api/teach/instance/listMyClass",
                                         {"instanceName": ""})
            if err is not None:
                return await self._handle_error(err, st, seen)
            for c in (data or {}).get("teachClassResponseList") or []:
                if isinstance(c, dict) and c.get("classId"):
                    names[c["classId"]] = (c.get("instanceName")
                                           or c.get("className") or f"课程{c['classId']}")
            missing = [c for c in class_ids if c not in names]

            # 3. 逐课程拉任务(串行)
            tasks, failed = [], []
            for cid in class_ids:
                data, err = await self._post(s, base, "/api/learn/task/listEasyTask", {
                    "classId": cid, "orderType": 1,
                    "page": {"pageNo": 1, "pageSize": MAX_TASKS_PER_COURSE},
                    "searchText": "", "status": 0})
                if err is not None:
                    if err == "auth":
                        return await self._handle_error("auth", st, seen)
                    failed.append(f"{names.get(cid, cid)}:{err}")
                    continue
                for t in (data or {}).get("rowList") or []:
                    if isinstance(t, dict) and t.get("taskId"):
                        t["_course"] = cid
                        tasks.append(t)

            # 趁会话还活着主动续期(关键!见 _maybe_proactive_refresh 的说明)
            await self._maybe_proactive_refresh(s, base, cfg, st)

        # 抓取成功才留快照(供 #好课检查 列表);失败时保持 None,不拿旧数据充数
        self._last_tasks = list(tasks)
        self._last_names = dict(names)

        st["last_ok"] = time.time()
        st.pop("last_error", None)

        if not st.get("seeded"):
            text = self._render_digest(names, tasks, class_ids)
            ok = await self._push(text)
            if not ok:
                return "首次汇总发送失败(WS 未连接?),下轮重试"
            for t in tasks:
                seen.add(f"task:{t['_course']}:{t.get('taskId')}")
            st["seeded"] = True
            st["courses"] = names
            _save_json(STATE_PATH, self._cap_seen(st, seen))
            log.info("首次运行完成,基线 %d 个任务", len(tasks))
            return f"首次运行完成,已发汇总(基线 {len(tasks)} 个任务)"

        sent = await self._push_fresh(names, tasks, seen)
        await self._check_deadlines(names, tasks, st)
        await self._maybe_daily_summary(names, tasks, st)

        st["courses"] = names
        st["unmatched"] = missing
        _save_json(STATE_PATH, self._cap_seen(st, seen))
        tail = f",{len(failed)} 门课拉取失败({'/'.join(failed)})" if failed else ""
        return f"检查完成:已推送 {sent} 条{tail}"

    # ---------- 增量推送 ----------
    async def _push_fresh(self, names, tasks, seen) -> int:
        fresh = []
        for t in tasks:
            key = f"task:{t['_course']}:{t.get('taskId')}"
            if key in seen:
                continue
            due = _parse_dt(t.get("endTime"))
            if not _has_deadline(due):
                # 用户选择"只推有截止时间的任务";仍记入 seen 避免以后反复判定
                seen.add(key)
                continue
            fresh.append((due, key, self._render_task(t, names, due)))

        fresh.sort(key=lambda x: x[0])
        sent = 0
        for _, key, text in fresh:
            ok = await self._push(text)
            if not ok:
                log.warning("第 %d 条推送失败,下轮重试", sent + 1)
                break  # WS 异常时保留未发部分,下轮重试
            seen.add(key)
            sent += 1
            await asyncio.sleep(SLEEP_BETWEEN_MSGS)
        if fresh:
            log.info("本轮新任务 %d 条,已推送 %d 条", len(fresh), sent)
        return sent

    # ---------- 截止提醒 ----------
    async def _check_deadlines(self, names, tasks, st):
        dl = st.get("deadlines") or {}
        now = datetime.now()
        changed = False

        for t in tasks:
            due = _parse_dt(t.get("endTime"))
            if not _has_deadline(due):
                continue
            if t.get("doneFlag") == 1:
                continue  # 已完成的不提醒
            key = f"{t['_course']}_{t.get('taskId')}"
            rec = dl.get(key) or {}
            remain = (due - now).total_seconds()

            if remain < 0:
                if key in dl:
                    dl.pop(key, None)
                    changed = True
                continue

            if remain <= 24 * 3600 and not rec.get("w24"):
                if await self._push(self._render_deadline(t, names, due, "24 小时")):
                    rec["w24"] = True
                    changed = True
                    await asyncio.sleep(SLEEP_BETWEEN_MSGS)
            if remain <= 2 * 3600 and not rec.get("w2"):
                if await self._push(self._render_deadline(t, names, due, "2 小时")):
                    rec["w2"] = True
                    changed = True
                    await asyncio.sleep(SLEEP_BETWEEN_MSGS)

            if rec:
                rec["due"] = t.get("endTime")
                dl[key] = rec
        if changed:
            st["deadlines"] = dl

    async def _maybe_daily_summary(self, names, tasks, st):
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if now.hour != DAILY_SUMMARY_HOUR or st.get("last_daily") == today:
            return
        tomorrow = (now + timedelta(days=1)).date()
        due_tomorrow = []
        for t in tasks:
            due = _parse_dt(t.get("endTime"))
            if _has_deadline(due) and due.date() == tomorrow and t.get("doneFlag") != 1:
                due_tomorrow.append((due, t))
        if not due_tomorrow:
            st["last_daily"] = today
            return
        lines = [f"🌙 明天有 {len(due_tomorrow)} 个好课任务截止"]
        for due, t in sorted(due_tomorrow, key=lambda x: x[0]):
            lines.append(f"· {names.get(t.get('_course'), '?')} · "
                         f"{t.get('taskName') or '未命名'}  截止 {due.strftime('%H:%M')}")
        if await self._push("\n".join(lines)):
            st["last_daily"] = today

    # ---------- HTTP ----------
    async def _post(self, session, base, path, body):
        """POST 一个查询接口。返回 (data, err)。

        err: None / "auth"(code 401) / "code<X>" / "http<X>" / "network(...)"。
        业务码在响应体里,HTTP 状态码恒为 200,不可用于判错。
        """
        payload = dict(body)
        payload.setdefault("requestId", str(uuid.uuid4()))
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            async with session.post(base + path, json=payload, headers=headers) as r:
                if r.status >= 400:
                    return None, f"http{r.status}"
                obj = await r.json(content_type=None)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return None, f"network({e.__class__.__name__})"
        if not isinstance(obj, dict):
            return None, "badjson"
        code = obj.get("code")
        if code == 401:
            return None, "auth"
        if code != 200:
            msg = (obj.get("message") or obj.get("msg") or "")[:60]
            return None, f"code{code}({msg})"
        return obj.get("data") or {}, None

    def _capture_ticket(self, cfg, data, st):
        # 续期已关闭时不必再维护这张一次性票(维护它只会让配置里挂着一个死值,
        # 让人误以为还能续期)。见文件头"续期"一节。
        if cfg.get("auto_refresh") is False:
            return
        """**只在两种情况下**从 queryCurrentUserInfo 取 ticket:引导、或旧票已失效。

        绝不能在每次轮询时无脑覆盖(2026-10-05 踩坑,导致续期连续失败 440 次):
        该接口返回的是本会话那张**固定的** ticket(实测连续三次完全相同),而
        tokenLogin 会把它**消费掉**——用过即失效,接口却仍返回同一张旧票。
        续期后 tokenLogin 会给出新会话的 ticket(`data.ticket`),那才是下一次续期
        该用的凭证;若每轮被这里的旧票覆盖,下一次续期必然报
        code=500「用户登录信息已失效」。

        实测印证:插件驱动的两次成功续期(09-27、10-03)恰好都是"用户重新登录后
        的首次续期";10-03 那次之后再没成功过。

        "_ticket_stale" 是给"换了新 token 但忘了清旧 ticket"兜底的:一旦续期失败
        就打上标记,下轮若发现接口给出了**不同**的 ticket(说明会话换了),就接受它。
        """
        if not isinstance(data, dict):
            return
        ticket = data.get("ticket")
        if not ticket:
            return
        if not (cfg.get("ticket") or "").strip():
            cfg["ticket"] = ticket
            _save_json(CONFIG_PATH, cfg)
            log.info("已从 queryCurrentUserInfo 引导首张 ticket")
            return
        if st.get("_ticket_stale") and ticket != cfg.get("ticket"):
            cfg["ticket"] = ticket
            _save_json(CONFIG_PATH, cfg)
            st.pop("_ticket_stale", None)
            log.info("旧 ticket 已失效,改用接口给出的新 ticket")

    async def _do_refresh(self, session, base, cfg, st) -> bool:
        """用 {token, ticket} 换新凭证并写回配置。成功返回 True。

        实测(2026-09-27):会话活着时调用返回 code=200 且给出新 token+ticket,
        新凭证立刻可用,旧 token 也仍然有效(不会把浏览器踢下线);
        且可连续接力(连换 3 次都成功)。"""
        token = (cfg.get("token") or "").strip()
        ticket = (cfg.get("ticket") or "").strip()
        if not (token and ticket):
            log.warning("无 token/ticket,无法续期")
            return False
        st["last_refresh_try"] = time.time()   # 退避计时用"最后一次尝试",不是"最后一次成功"
        data, err = await self._post(session, base, "/api/auth/tokenLogin",
                                     {"token": token, "ticket": ticket})
        if err is not None or not isinstance(data, dict):
            log.warning("续期失败: %s", err)
            # 必须落到 state 里:否则续期失败只进日志,状态文件和 #好课状态 都
            # 看不见——2026-10-05 就是因此连续失败 440 次而无人察觉。
            st["last_refresh_error"] = f"{time.strftime('%m-%d %H:%M')} {err}"
            st["_ticket_stale"] = True  # 允许下轮重新引导 ticket
            await self._note_refresh_failure(st, err)
            return False
        new_token = data.get("token")
        if not new_token:
            log.warning("续期响应里没有 token")
            st["last_refresh_error"] = f"{time.strftime('%m-%d %H:%M')} 响应无 token"
            st["_ticket_stale"] = True
            await self._note_refresh_failure(st, "响应无 token")
            return False
        self._token = new_token
        cfg["token"] = new_token
        if data.get("ticket"):
            cfg["ticket"] = data["ticket"]
        _save_json(CONFIG_PATH, cfg)
        st["refresh_count"] = int(st.get("refresh_count") or 0) + 1
        st["last_refresh"] = time.time()
        st.pop("last_refresh_error", None)
        st.pop("_ticket_stale", None)  # 续期成功即恢复健康,别再让接口的票覆盖
        st.pop("refresh_fail_streak", None)
        st.pop("renew_warned", None)   # 下次再坏时允许再提醒一次
        log.info("续期成功(累计第 %d 次)", st["refresh_count"])
        return True

    async def _note_refresh_failure(self, st, err):
        """记连败次数,并在达到阈值时提醒主人该换 token 了。

        这一步是 2026-10-13 新增的核心:续期坏掉后 token 还能活几天,期间
        "看起来一切正常"——原本只有 token 真到期(401)才会提醒,那时已经晚了
        (10-12 就是如此)。现在改成续期一坏就提前说,给用户留出换 token 的时间。
        """
        streak = int(st.get("refresh_fail_streak") or 0) + 1
        st["refresh_fail_streak"] = streak
        if streak < RENEW_WARN_AFTER or st.get("renew_warned"):
            return
        st["renew_warned"] = True   # 一段故障只提醒一次,不跟着每轮刷
        life = st.get("last_session_days")
        life_txt = (f"参考：上一段会话实测活了 {life} 天" if life
                    else "参考：实测会话寿命 2.8~6.6 天")
        await self._push(
            f"⚠️ 知途好课的自动续期坏了（连续 {streak} 次失败：{err}）。\n"
            "重点：token 本身现在还能用，所以任务提醒照常，但续期已经停了——\n"
            f"{life_txt}，等它到期你就会开始收不到提醒，而且不会自动恢复。\n"
            "建议趁现在换一个新 token：浏览器登录 https://sjtu.aihaoke.net → F12 →\n"
            "Network 里点任意一条 /api/ 请求 → 复制 Request Headers 的\n"
            "Authorization 值（Bearer 后面那段），替换 plugins/aihaoke.json 的 token 字段。\n"
            "改完下一轮轮询自动生效，不用重启机器人。\n"
            f"（续期已进入退避，每 {REFRESH_FAIL_BACKOFF_HOURS:g} 小时再试一次；"
            "换好 token 后自动恢复。）")

    @staticmethod
    def _refresh_interval_hours(cfg) -> float:
        """续期间隔(小时)。优先读 refresh_every_hours,兼容旧的 refresh_every_days。"""
        for key, mult in (("refresh_every_hours", 1), ("refresh_every_days", 24)):
            v = cfg.get(key)
            if v:
                try:
                    return max(0.5, float(v) * mult)
                except (TypeError, ValueError):
                    pass
        return 12.0

    async def _maybe_proactive_refresh(self, session, base, cfg, st):
        """会话"还活着"的时候主动续期——本插件最关键的一处设计。

        **两次实测的会话寿命**(这是设置间隔的唯一依据,别再拍脑袋):
          - 2026-09-18 浏览器登录给的 token → 09-24 17:32 失效,存活约 6.1 天
          - 2026-09-27 tokenLogin 续出来的  → 09-30 11:07 失效,存活约 2.8 天

        2026-09-30 近失教训:最初按"6 天寿命"设了 3 天续期一次,但续出来的会话
        只活了 2.8 天 —— 计划续期时刻是 09-30 15:04,会话 11:07 就死了,只差 4 小时。
        根因是安全余量按错误的寿命估计来定。

        现在改成小时级、默认 12 小时:相对 2.8 天有 5 倍以上余量。
        续期失败时不会更新 last_refresh,所以下一轮(15 分钟后)会自动重试,能自愈。

        还未能证实的点:反复续期是否真能**无限**延长会话(接力只验证了接口可行,
        没验证跨天有效)。每段会话的实际存活天数记在 state 的 last_session_days,
        累积几个数据点后就能判断。
        """
        # 2026-10-14 起配置文件里 auto_refresh=false(默认):平台的续期机制已证伪,
        # 详见文件头。保留这段代码只是万一平台以后真发了 haoke-auto-login 时能低成本
        # 复活——**没有新证据前别打开**,打开只会白烧一次性票并刷失败日志。
        if cfg.get("auto_refresh") is False:
            return
        every_h = self._refresh_interval_hours(cfg)
        last = float(st.get("last_refresh") or 0)
        now = time.time()
        streak = int(st.get("refresh_fail_streak") or 0)
        if streak >= REFRESH_FAIL_BACKOFF_AFTER:
            # 已经连败到退避档:按"最后一次尝试"计时,别每 15 分钟打一次接口、
            # 刷一条日志(10-06→10-12 那次刷了 249 条,把真正的信息淹了)
            last_try = float(st.get("last_refresh_try") or 0)
            if last_try and (now - last_try) < REFRESH_FAIL_BACKOFF_HOURS * 3600:
                return
            log.info("续期已连续失败 %d 次,退避中(每 %g 小时重试一次)",
                     streak, REFRESH_FAIL_BACKOFF_HOURS)
        elif last and (now - last) < every_h * 3600:
            return
        since_h = (now - last) / 3600 if last else 0.0
        log.info("距上次续期 %.1f 小时(阈值 %.1f 小时),主动续期以维持会话",
                 since_h, every_h)
        # 刻意不在这里写 last_session_days:此刻会话还活着,这个数只是"已存活",
        # 不是寿命;真正的寿命只在会话死亡时(_handle_error)记录。
        await self._do_refresh(session, base, cfg, st)

    async def _try_refresh(self, session, base, cfg, st):
        """兜底:已经 401 了才走到这里,多半已经太晚,但仍试一次。"""
        if cfg.get("auto_refresh") is False:
            return None, "auth"
        if not await self._do_refresh(session, base, cfg, st):
            return None, "auth"
        return await self._post(session, base, "/api/auth/queryCurrentUserInfo", {})

    async def _handle_error(self, err, st, seen) -> str:
        st["last_error"] = f"{time.strftime('%m-%d %H:%M')} {err}"
        log.warning("好课拉取失败: %s", err)
        if err == "auth":
            # 记下这段会话实际存活了多久——校准续期间隔最直接的依据。
            # 只记一次:此后每轮(15分钟)都会失败,若无条件重写就会把这个数字
            # 越写越大(实测踩过:2.83 天被连续覆写成 5.5 天),污染校准数据。
            last = float(st.get("last_refresh") or 0)
            if last and st.get("_death_recorded_for") != last:
                st["last_session_days"] = round((time.time() - last) / 86400, 2)
                st["_death_recorded_for"] = last
                log.warning("会话在续期后 %.2f 天失效", st["last_session_days"])
            _save_json(STATE_PATH, self._cap_seen(st, seen))
            await self._warn_auth()
            return "凭证失效(401),已提醒主人更新 token"
        _save_json(STATE_PATH, self._cap_seen(st, seen))
        return f"拉取失败:{err}"

    async def _warn_auth(self):
        now = time.time()
        if now - self._auth_warned_at < AUTH_WARN_COOLDOWN:
            return
        self._auth_warned_at = now
        if not self.cfg.OWNER_QQ:
            return
        await self._push(
            "⚠️ 知途好课监控已失效:凭证被拒(401),token 用完了。\n"
            "平台没有续期机制（见 #好课同步 的说明），所以只能重新登录换取新 token。\n"
            "① 装了自动同步脚本的话：浏览器打开 https://sjtu.aihaoke.net 重新登录一下，\n"
            "   脚本会自动把新 token 推给机器人，不用做别的（登录后约 1 分钟生效）；\n"
            "② 没装脚本：F12 → Network 里点任意一条 /api/ 请求 → 复制 Request Headers 里的\n"
            "   Authorization 值(Bearer 后面那段),填进 plugins/aihaoke.json 的 token 字段。"
        )

    # ---------- 浏览器用户脚本同步 ----------
    @staticmethod
    def _sync_port(cfg) -> int:
        try:
            return int(cfg.get("sync_port") or SYNC_PORT)
        except (TypeError, ValueError):
            return SYNC_PORT

    @staticmethod
    def _sync_secret(cfg) -> str:
        """同步密钥;没有就生成一个落盘。防止本机别的程序随便写凭证。"""
        s = (cfg.get("sync_secret") or "").strip()
        if s:
            return s
        s = base64.urlsafe_b64encode(os.urandom(18)).decode().rstrip("=")
        cfg["sync_secret"] = s
        _save_json(CONFIG_PATH, cfg)
        return s

    @staticmethod
    def _cors_headers(request) -> dict:
        """只给 sjtu.aihaoke.net 放行 —— 别的网页拿不到 CORS 许可。"""
        h = {
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-Sync-Key",
            "Access-Control-Max-Age": "600",
            "Vary": "Origin",
        }
        if request.headers.get("Origin", "") == SYNC_ALLOWED_ORIGIN:
            h["Access-Control-Allow-Origin"] = SYNC_ALLOWED_ORIGIN
            # Chrome 的 Private Network Access:公网页面(https://sjtu.aihaoke.net)
            # 请求本机地址(127.0.0.1)时预检必须明确同意,否则浏览器直接拦掉。
            # 少了这个头脚本会**静默失败**(服务端看起来一切正常,请求根本没发出来)
            # ——二课那边 2026-10-05 踩过,照搬。
            if request.headers.get(
                    "Access-Control-Request-Private-Network") == "true":
                h["Access-Control-Allow-Private-Network"] = "true"
        return h

    async def _sync_server(self):
        """在 127.0.0.1 上开个只服务本机的接口,接收浏览器推来的 token。

        三层防护(这是本机任何程序都摸得到的入口,不能只靠"别人不知道"):
        ①只绑回环,局域网碰不到;②要 X-Sync-Key;③CORS 只放行 sjtu.aihaoke.net,
        且要求 JSON 正文 —— 非简单请求会先发预检,其它网页过不了预检。
        """
        cfg = _load_json(CONFIG_PATH, {})
        if cfg.get("sync_enabled") is False:
            log.info("用户脚本同步已关闭(sync_enabled=false)")
            return
        port = self._sync_port(cfg)
        app = web.Application()
        app.router.add_get("/" + USERSCRIPT_FILE, self._http_userscript)
        app.router.add_route("OPTIONS", SYNC_PATH, self._http_preflight)
        app.router.add_post(SYNC_PATH, self._http_token)
        runner = web.AppRunner(app)
        try:
            await runner.setup()
        except OSError as e:
            self._sync_error = f"HTTP 服务起不来({e})"
            log.warning("token 同步服务初始化失败(%s)", e)
            return

        # 绑定可能因为"上一个实例还没退出"而失败 —— 重启竞态在 Windows 上很常见,
        # 而用户脚本那边是静默失败,谁都不会发现,直到 token 又过期。所以必须重试。
        for attempt in range(1, SYNC_BIND_RETRIES + 1):
            try:
                site = web.TCPSite(runner, "127.0.0.1", port)
                await site.start()
                break
            except OSError as e:
                if attempt >= SYNC_BIND_RETRIES:
                    self._sync_error = f"端口 {port} 试了 {attempt} 次仍起不来({e})"
                    log.warning("token 同步端口 %d 试了 %d 次都起不来(%s);"
                                "用户脚本同步不可用,#好课token 不受影响",
                                port, attempt, e)
                    await runner.cleanup()
                    return
                if attempt == 1:
                    log.warning("token 同步端口 %d 被占(可能上一个实例还没退),"
                                "每 %d 秒重试,最多 %d 次",
                                port, SYNC_BIND_RETRY_DELAY, SYNC_BIND_RETRIES)
                await asyncio.sleep(SYNC_BIND_RETRY_DELAY)

        self._sync_up = True
        log.info("token 同步接口已就绪: http://127.0.0.1:%d/%s", port, USERSCRIPT_FILE)
        await asyncio.Event().wait()   # 挂到进程退出

    async def _http_userscript(self, request):
        """下发用户脚本(端口/密钥填好)。"""
        cfg = _load_json(CONFIG_PATH, {})
        body = USERSCRIPT.replace("__PORT__", str(self._sync_port(cfg))) \
                         .replace("__KEY__", self._sync_secret(cfg))
        return web.Response(text=body, content_type="text/plain", charset="utf-8")

    async def _http_preflight(self, request):
        return web.Response(status=204, headers=self._cors_headers(request))

    async def _http_token(self, request):
        """接收浏览器用户脚本推来的 token。校验不过一律不写配置。"""
        cors = self._cors_headers(request)
        cfg = _load_json(CONFIG_PATH, {})
        if request.headers.get("X-Sync-Key", "") != self._sync_secret(cfg):
            return web.json_response({"ok": False, "msg": "sync key 不对"},
                                     status=403, headers=cors)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "msg": "正文不是 JSON"},
                                     status=400, headers=cors)
        # 自检上报:脚本把自身状态报过来,方便在机器人日志里定位"脚本跑没跑、
        # 取没取到 token"。放在限频之前,保证诊断不受限频影响。
        if isinstance(body, dict) and body.get("ping"):
            log.info("用户脚本自检: %s", str(body["ping"])[:500])
            if not (body.get("token") or "").strip():
                return web.json_response({"ok": True, "msg": "ping ok"}, headers=cors)

        now = time.monotonic()
        if now - self._last_sync_at < SYNC_MIN_INTERVAL:
            return web.json_response({"ok": False, "msg": "太频繁,稍后再试"},
                                     status=429, headers=cors)
        self._last_sync_at = now
        new = body.get("token") if isinstance(body, dict) else None
        if not isinstance(new, str) or not new.strip():
            return web.json_response({"ok": False, "msg": "没带 token"},
                                     status=400, headers=cors)

        ok, msg = await self._apply_token(new.strip())
        log.info("用户脚本同步 token:%s", "成功" if ok else "被拒")
        if ok:
            await self._notify_first_sync()
        return web.json_response({"ok": ok, "msg": msg.replace("\n", " ")},
                                 status=200 if ok else 400, headers=cors)

    async def _apply_token(self, new):
        """校验并写入新 token。返回 (是否成功, 面向主人的说明)。

        **必须先实测**再落盘:好课的 JWT 里**没有 exp**(会话寿命由服务端控制),
        所以没法像二课那样先解有效期,唯一可靠的判据是真拉一次业务接口。存下一个
        坏凭证比不存更糟——会静默停摆到下次 401 才发现。
        目前只有用户脚本的 HTTP 入口走这里(没有做成 QQ 命令,用户选的是脚本方案)。
        """
        new = (new or "").strip()
        if len(new.split(".")) != 3 or len(new) < 100:
            return False, ("❌ 这不像一个好课的 token（应是三段式 JWT，长度约 184），"
                           "配置没有改动。")
        cfg = _load_json(CONFIG_PATH, {})
        base = (cfg.get("base_url") or DEFAULT_BASE).rstrip("/")
        old = self._token
        self._token = new
        timeout = aiohttp.ClientTimeout(total=30)
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as s:
                data, err = await self._post(s, base, "/api/teach/instance/listMyClass",
                                             {"instanceName": ""})
        except Exception as e:
            err = f"network({e.__class__.__name__})"
            data = None
        finally:
            self._token = old
        if err == "auth":
            return False, ("❌ 平台拒绝了这个 token（401），配置没有改动。\n"
                           "多半是复制时漏了字符，或者它已经过期——请在浏览器重新登录后再来一次。")
        if err is not None:
            return False, (f"⚠️ 验证时拉取失败（{err}），无法确认 token 有效，"
                           "配置没有改动。稍后重试即可。")
        courses = (data or {}).get("teachClassResponseList") or []
        if not courses:
            return False, ("⚠️ 这个 token 能通过认证，但一个课程都查不到，配置没有改动。\n"
                           "（可能是账号本身没有在学课程；若确实如此请手动改 aihaoke.json）")
        cfg["token"] = new
        _save_json(CONFIG_PATH, cfg)
        self._token = new
        # 换了凭证,把旧会话留下的续期/失效标记一并清掉,别让状态显示骗人。
        # 顺手判断"这次是不是把一次失效救回来了"——若是,给主人一条恢复通知:
        # 否则用户只知道机器人报过 401,不知道已经自愈,还得自己发 #好课状态 确认。
        st = _load_json(STATE_PATH, {})
        rescued = "auth" in str(st.get("last_error") or "")
        for k in ("last_error", "last_refresh_error", "refresh_fail_streak",
                  "renew_warned", "_ticket_stale"):
            st.pop(k, None)
        _save_json(STATE_PATH, st)
        if rescued:
            await self._push("✅ 知途好课的新 token 已自动同步并实测通过，监控已恢复。")
        names = "、".join(str(c.get("instanceName") or c.get("className") or c.get("classId"))
                          for c in courses[:4])
        return True, f"✅ 已更新并实测通过（查到 {len(courses)} 门课程：{names}）。"

    async def _notify_first_sync(self):
        """只在第一次自动同步成功时说一声,之后不再打扰。

        用户脚本是"装完就不管"的东西,装错了(端口不通、密钥不对)不会有任何反馈,
        所以成功一次必须告知,否则没法确认它到底通了没有。
        状态存独立小文件,避免和主状态文件并发互写。
        """
        st = _load_json(SYNC_STATE_PATH, {})
        st["last_ok_at"] = time.time()
        first = not st.get("first_ok_at")
        if first:
            st["first_ok_at"] = time.time()
        _save_json(SYNC_STATE_PATH, st)
        if first:
            await self._push("✅ 知途好课 token 自动同步已生效\n"
                             "以后只要在浏览器里打开过 sjtu.aihaoke.net，"
                             "新 token 会自动推给机器人，不用再手动复制粘贴了。")

    @staticmethod
    def _sync_text(cfg) -> str:
        """#好课同步 的说明文字。"""
        if cfg.get("sync_enabled") is False:
            return ("#好课同步\n用户脚本同步已在 aihaoke.json 中关闭"
                    "（sync_enabled=false）。")
        p = cfg.get("sync_port") or SYNC_PORT
        lines = [
            "#好课同步",
            "用途：让浏览器自动把好课的 token 推给机器人，省掉手动复制粘贴。",
            "",
            "安装（一次，约 1 分钟）：",
            "1. 浏览器装扩展 Tampermonkey（篡改猴）；",
            f"2. 地址栏打开 http://127.0.0.1:{p}/{USERSCRIPT_FILE} ，",
            "    Tampermonkey 会弹出安装页，点「安装」即可（端口和密钥已预填好）；",
            "3. 之后正常使用 sjtu.aihaoke.net 就行，脚本会在页面后台自动同步。",
            "",
            "装好后第一次同步成功，机器人会私聊告诉你一声；",
            "若一直没收到，发 #好课状态 看同步接口是否正常。",
        ]
        return "\n".join(lines)

    # ---------- 推送 ----------
    async def _push(self, text) -> bool:
        """私聊主人。带僵尸连接防护:send_json 对半开连接可能假成功。"""
        owner = self.cfg.OWNER_QQ
        if not owner or not text:
            return False
        ok = await self.client.send_action(
            "send_private_msg", {"user_id": int(owner), "message": text})
        if not ok:
            return False
        await asyncio.sleep(1)
        if self.client.ws is None:
            return False
        return True

    # ---------- 渲染 ----------
    @staticmethod
    def _cname(names, t):
        return names.get(t.get("_course"), "未知课程")

    @staticmethod
    def _link(t) -> str:
        return f"{DEFAULT_BASE}/student/course/{t.get('_course')}/home"

    def _render_task(self, t, names, due) -> str:
        tt = TASK_TYPE.get(t.get("taskType"), f"类型{t.get('taskType')}")
        return "\n".join([
            f"📝 新任务 · {self._cname(names, t)}",
            f"任务:{t.get('taskName') or '未命名'}",
            f"类型:{tt}",
            f"截止:{_fmt_dt(due)}（{_remain(due)}）",
            f"链接:{self._link(t)}",
        ])

    def _render_deadline(self, t, names, due, label) -> str:
        return "\n".join([
            f"⏰ 任务即将截止（还剩 {label}）",
            f"{self._cname(names, t)} · {t.get('taskName') or '未命名'}",
            f"截止:{_fmt_dt(due)}",
            f"链接:{self._link(t)}",
        ])

    # ---------- 未完成的必做任务（#好课检查 用）----------
    def _render_todo(self, names, tasks) -> str:
        """列出未完成的必做任务。

        "必做"是平台的 requireFlag 字段(2026-10-13 实测:12 条练习全是 1、
        7 个 PDF 文档全是 0,与直觉完全一致);"未完成"是 doneFlag != 1。
        有截止的按时间正序排最前(最急的在最上面),没截止的排最后。
        """
        required = [t for t in tasks if t.get("requireFlag") == 1]
        todo = [t for t in required if t.get("doneFlag") != 1]
        if not todo:
            return (f"📋 未完成的必做任务：0 条 🎉\n"
                    f"（共 {len(tasks)} 条任务，其中必做 {len(required)} 条，都已完成）")
        # 无截止时间的用 datetime.max 垫底,免得混在有截止的前面
        def _due_key(t):
            d = _parse_dt(t.get("endTime"))
            return d if _has_deadline(d) else datetime.max
        todo.sort(key=_due_key)
        lines = [f"📋 未完成的必做任务：{len(todo)} 条"
                 f"（共 {len(tasks)} 条任务，其中必做 {len(required)} 条）"]
        for t in todo[:TODO_MAX_LINES]:
            due = _parse_dt(t.get("endTime"))
            tt = TASK_TYPE.get(t.get("taskType"), f"类型{t.get('taskType')}")
            lines.append(f"\n· {self._cname(names, t)} ｜ {t.get('taskName') or '未命名'}")
            lines.append(f"  {tt} · " + (f"截止 {_fmt_dt(due)}（{_remain(due)}）"
                                         if _has_deadline(due) else "无截止时间"))
            lines.append(f"  {self._link(t)}")
        if len(todo) > TODO_MAX_LINES:
            lines.append(f"\n…另有 {len(todo) - TODO_MAX_LINES} 条未列出（消息会太长），"
                         f"建议先清掉上面这些")
        return "\n".join(lines)

    # ---------- 首轮汇总 ----------
    def _render_digest(self, names, tasks, class_ids) -> str:
        now = datetime.now()
        horizon = now + timedelta(days=30)
        todo = []
        for t in tasks:
            due = _parse_dt(t.get("endTime"))
            if not _has_deadline(due) or t.get("doneFlag") == 1:
                continue
            if now <= due <= horizon:
                todo.append((due, t))
        todo.sort(key=lambda x: x[0])

        lines = ["📚 知途好课 待办汇总（首次运行）"]
        for cid in class_ids:
            lines.append(f"· {names.get(cid, f'课程{cid}')}")
        lines.append("")
        if todo:
            lines.append(f"⏳ 未来 30 天要完成（{len(todo)} 项）")
            for due, t in todo[:15]:
                tt = TASK_TYPE.get(t.get("taskType"), "")
                lines.append(f"· {names.get(t.get('_course'), '?')} · "
                             f"{t.get('taskName') or '未命名'}" + (f"（{tt}）" if tt else ""))
                lines.append(f"  截止 {_fmt_dt(due)}（{_remain(due)}）")
            if len(todo) > 15:
                lines.append(f"…另有 {len(todo) - 15} 项")
        else:
            lines.append("✅ 未来 30 天没有待完成的任务")
        lines.append("")
        lines.append("今后只推送新变化(仅含有截止时间的任务)。发 #好课状态 查看运行情况。")
        return "\n".join(lines)

    # ---------- 状态维护 ----------
    @staticmethod
    def _cap_seen(st, seen):
        st = dict(st)
        st["seen"] = list(seen)[-SEEN_CAP:]
        return st

    # ---------- 主人命令 ----------
    async def on_message(self, ev: events.MessageEvent) -> bool:
        text = events.text_of(ev.segments).strip()
        if not text.startswith("#好课"):
            return False
        if not self.router.is_owner(ev.user_id):
            return True  # 仅主人可用,吞掉命令

        if text.startswith("#好课检查"):
            await self.router.reply(ev, "⏳ 正在检查知途好课…")
            result = await self.check_once()
            lines = ["#好课检查"]
            if self._last_tasks is not None:
                # 这一轮真抓到了数据 → 附上"未完成的必做任务"清单
                lines.append(self._render_todo(self._last_names, self._last_tasks))
                lines.append("")
            lines.append(result)
            await self.router.reply(ev, "\n".join(lines))
            return True

        cfg = _load_json(CONFIG_PATH, {})
        st = _load_json(STATE_PATH, {})

        if text.startswith("#好课课程"):
            names = st.get("courses") or {}
            lines = ["#好课课程"]
            if names:
                for cid, nm in names.items():
                    mark = "✅" if cid in (cfg.get("courses") or []) else "　"
                    lines.append(f"{mark} {nm}（classId={cid}）")
                lines.append("✅=正在监测。改 aihaoke.json 的 courses 调整。")
            else:
                lines.append("还没有课程缓存,先执行 #好课检查")
            await self.router.reply(ev, "\n".join(lines))
            return True

        if text.startswith("#好课同步"):
            await self.router.reply(ev, self._sync_text(cfg))
            return True

        # #好课状态
        have_token = bool((cfg.get("token") or "").strip())
        sync_st = _load_json(SYNC_STATE_PATH, {})
        if cfg.get("sync_enabled") is False:
            sync_line = "已关闭(sync_enabled=false)"
        elif self._sync_up:
            sync_line = f"✅ 运行中(端口 {self._sync_port(cfg)})"
            if sync_st.get("last_ok_at"):
                sync_line += ("，最近同步 "
                              + _fmt_dt(datetime.fromtimestamp(sync_st["last_ok_at"])))
            else:
                sync_line += "，还没有同步过（脚本装了没？发 #好课同步 看步骤）"
        else:
            sync_line = f"⚠️ 没起来({self._sync_error or '尚未启动'})"
        lines = [
            f"凭证:{'✅ 已配置' if have_token else '❌ 未配置'}",
            f"自动续期:已关闭(平台不发续期钥匙，见 #好课同步 说明)"
            if cfg.get("auto_refresh") is False else
            f"自动续期:开"
            + (f",已续期 {st.get('refresh_count')} 次" if st.get("refresh_count") else ""),
            f"浏览器自动同步:{sync_line}",
            f"监测课程数:{len(cfg.get('courses') or [])}",
            f"上次成功:"
            + (_fmt_dt(datetime.fromtimestamp(st['last_ok'])) if st.get("last_ok") else "从未"),
            f"已记录:{len(st.get('seen', []))} 条",
            f"轮询间隔:{POLL_INTERVAL // 60} 分钟",
        ]
        if st.get("unmatched"):
            lines.append(f"⚠️ 白名单中已不存在的课程:{st['unmatched']}")
        if st.get("last_error"):
            lines.append(f"最近错误:{st['last_error']}")
        await self.router.reply(ev, "#好课状态\n" + "\n".join(lines))
        return True
