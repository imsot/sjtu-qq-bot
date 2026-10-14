"""第二课堂(activity.sjtu.edu.cn)活动监控：有新活动开放报名时私聊推送主人。

依赖：plugins/sjtu_academic.json 里的 token。取自浏览器的 **cookie `IMPSA-Token`**
(F12 → Console 执行 `copy(document.cookie...)`，或直接用下面的用户脚本自动同步)。
**注意是 cookie，不是 localStorage** —— 2026-09-28 实测确认，此前写错过。
该平台走 jaccount OAuth，**没有刷新令牌端点**，token 过期只能重新登录一次再取，
所以本插件不做自动续期，只在快过期/401 时提醒主人。

流程：每 poll_interval 秒拉一次"报名中"活动列表 → 与 .sjtu_academic_state.json
去重比对 → 首次运行发一份在报汇总(不逐条刷屏) → 之后只推新开放报名的活动。

另有**收藏活动报名提醒**：对主人收藏的活动,在报名开始前 1 天 / 1 小时 /
10 分钟分别私聊提醒(见 _sync_collected / tick_reminders)。

还有**浏览器用户脚本自动同步**：本机 127.0.0.1 上开个小接口,浏览器里装一次
用户脚本(#二课同步 有安装地址),之后打开活动网站就会自动把新 token 推过来。
不存账号密码 —— jaccount 密码登录强制图形验证码,自动化不可行,而且密码能开
邮箱/选课/成绩整个交大身份,代价远大于收益。

主人命令：#二课检查 立即轮询一次；#二课状态 查看运行情况；#二课类型 列出活动类型编号；
#二课收藏 列出收藏的活动；#二课提醒 查看收藏活动的提醒计划；#二课同步 用户脚本安装说明。

设计要点(逆向前端打包代码得来,勿改)：
- **HTTP 状态码恒为 200**，业务码在响应体 `code` 字段。未登录时 HTTP 仍是 200 而
  `code=401`。所以只能判 `code`，不能看 HTTP 状态码。这是本插件最容易写错的地方。
- **列表接口是 `/activity/list/home`**，不是 `/activity/list`(那个是管理端列表)。
  响应结构是 `{code, message, data:[...], total:N}`：数组在顶层 `data`，
  总数在同级 `total`，**没有 current_page/last_page**，分页要自己按 total 收敛。
- **查询参数 `status` 是数字且按数组发送**：`status[0]=2` 表示"报名中"(可报名)，
  `status[0]=4` 表示"即将举办"。这是"只推可报名"的服务端过滤依据。
- **时间字段是二元数组**，格式 `"YYYY-MM-DD HH:mm:ss"`，北京时间、无时区偏移。
  活动时间是 `activity_time`，报名窗口是 `registration_time`，都是 `[开始, 结束]`。
- **不要依赖排序做新增检测**。time_sort 的取值含义(desc/signUp/asc)在打包代码里
  没有确定证据，若排序方向猜错会漏掉新活动。所以本插件把当前"报名中"的活动整批
  拉下来(有上限)做集合比对，顺序只用来决定推送先后，不影响是否被发现。
- 名额判定只对 `method==3`(站内报名且受名额限制)成立，其它报名方式前端压根不显示
  人数比例，不能拿 signed_up_num>=person_num 一刀切。
- 报名未开始的活动**不写入 seen**，等它真正开放报名时再推，否则会永久漏掉。
"""
import asyncio
import base64
import json
import logging
import os
import time
from datetime import datetime, timedelta

import aiohttp
from aiohttp import web

from bot import events
from bot.router import Plugin

log = logging.getLogger("sjtu_academic")

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PLUGIN_DIR, "sjtu_academic.json")
STATE_PATH = os.path.join(PLUGIN_DIR, ".sjtu_academic_state.json")
# 同步功能单独存一个小文件,避免和主状态文件互写(两者可能并发)
SYNC_STATE_PATH = os.path.join(PLUGIN_DIR, ".sjtu_academic_sync.json")

DEFAULT_BASE = "https://activity.sjtu.edu.cn"
API = "/api/v1"
POLL_INTERVAL = 900            # 轮询间隔(秒)=15 分钟
AUTH_WARN_COOLDOWN = 6 * 3600  # 凭证失效提醒间隔(秒)
EXPIRY_WARN_BEFORE = 3 * 3600  # 距过期不足此时长(秒)就提前提醒换 token
SEEN_CAP = 3000                # 去重列表上限
TYPES_MAX_LINES = 60           # #二课类型 输出行数上限,防刷屏
PER_PAGE = 50                  # 每页条数
MAX_PAGES = 4                  # 分页硬上限,防失控(最多 200 条)
SLEEP_BETWEEN_MSGS = 2         # 条与条之间的间隔,防风控
DIGEST_LIMIT = 15              # 首轮汇总最多列几条

# 收藏活动的报名开始提醒：提前多久提醒,以及本地滴答间隔。
# 为什么需要滴答:"报名开始前十分钟"这个窗口只有 10 分钟宽,而完整轮询是 15 分钟
# 一次,按轮询去判会整个跨过去、提醒永远发不出来。滴答只用状态文件里缓存的报名
# 开始时间算,不发任何请求,所以可以每 30 秒跑一次而不增加平台压力。
REMINDER_TICK = 30             # 本地滴答间隔(秒)
TIERS = (("d1", 86400), ("h1", 3600), ("m10", 600))   # (层级, 提前秒数)
TIER_TEXT = {"d1": "提前 1 天", "h1": "提前 1 小时", "m10": "提前 10 分钟"}

# 浏览器用户脚本自动同步:在本地回环上开个小接口,接收浏览器推来的 token。
# 为什么不用账号密码自动登录:jaccount 密码登录强制图形验证码(image/captcha.png),
# 脚本拿不到验证码;而且密码能开邮箱/选课/成绩整个交大身份,代价远大于收益。
# 这个方案只读取浏览器里已有的 IMPSA-Token 推给本机,不存长期凭证。
SYNC_PORT = 37218              # 本机监听端口(仅 127.0.0.1)
SYNC_PATH = "/token"
SYNC_ALLOWED_ORIGIN = "https://activity.sjtu.edu.cn"
SYNC_MIN_INTERVAL = 20         # 同一入口最小间隔(秒),防本机别的程序反复打
SYNC_BIND_RETRIES = 20         # 端口被占时重试次数(重启竞态,见 _sync_server)
SYNC_BIND_RETRY_DELAY = 15     # 每次重试间隔(秒) -> 共重试 5 分钟
USERSCRIPT_FILE = "sjtu_academic_sync.user.js"

# 浏览器 Console 里手动取 token 的一行命令。
# **注意 token 是存在 cookie 里的,不在 localStorage**(2026-09-28 靠用户脚本自检
# 报回的存储键名才确认;此前我给的手动指引写的是 localStorage,那是错的、取不到东西)。
TOKEN_JS = ("copy(document.cookie.split(';').map(s=>s.trim())"
            ".find(s=>s.startsWith('IMPSA-Token=')).slice(12))")

USERSCRIPT = """// ==UserScript==
// @name         第二课堂 token 自动同步
// @namespace    sjtu-academic-sync
// @version      1.6
// @description  把 activity.sjtu.edu.cn 的登录 token 自动推给本机 QQ 机器人；token 过期时自动刷新页面换取新的（端口/密钥已预填）
// @match        https://activity.sjtu.edu.cn/*
// @run-at       document-idle
// @updateURL    http://127.0.0.1:__PORT__/sjtu_academic_sync.user.js
// @downloadURL  http://127.0.0.1:__PORT__/sjtu_academic_sync.user.js
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      127.0.0.1
// ==/UserScript==
(function () {
  'use strict';
  var PORT = __PORT__;
  var KEY  = '__KEY__';
  var ENDPOINT = 'http://127.0.0.1:' + PORT + '/token';
  var FP_COOKIE = 'sjtuSyncFp';   // 去重标记放这里(自己写的 cookie,存哈希)
  var FIX_COOKIE = 'sjtuSyncFix'; // 自动刷新的节流状态(次数.上次时间戳)
  var FIX_MAX_TRIES = 3;                 // 连续刷这么多次还拿不到新 token 就停手
  var FIX_COOLDOWN_MS = 10 * 60 * 1000;  // 两次自动刷新至少间隔 10 分钟
  var FIX_IDLE_MS = 10 * 60 * 1000;      // 页面可见但这么久没操作,也允许刷新
  var TAG = '[第二课堂同步] ';
  // 带 @grant 时脚本跑在沙箱里，用 unsafeWindow 才拿得到页面真正的 localStorage
  var W = (typeof unsafeWindow !== 'undefined') ? unsafeWindow : window;
  var warnedNoToken = false;
  var warnedFixFail = false;

  // 取所有可能可用的存储对象。带 @grant 时脚本跑在 Tampermonkey 的沙箱里，
  // 直接访问 localStorage 和 unsafeWindow.localStorage 哪个能用，跟 TM 版本和
  // 沙箱模式有关 —— 只试一种的话，失败时会静默读不到 token（表现为"脚本明明
  // 跑了却毫无反应"），所以四种全试。
  function stores() {
    var out = [];
    try { if (localStorage) { out.push(localStorage); } } catch (e) {}
    try { if (sessionStorage) { out.push(sessionStorage); } } catch (e) {}
    try { if (W.localStorage) { out.push(W.localStorage); } } catch (e) {}
    try { if (W.sessionStorage) { out.push(W.sessionStorage); } } catch (e) {}
    return out;
  }

  function doc() {
    try { return (W.document) || document; } catch (e) { return document; }
  }

  function readCookie(name) {
    try {
      var parts = (doc().cookie || '').split(';');
      for (var i = 0; i < parts.length; i++) {
        var eq = parts[i].indexOf('=');
        if (eq < 0) { continue; }
        if (parts[i].slice(0, eq).trim() !== name) { continue; }
        var v = parts[i].slice(eq + 1).trim();
        try { return decodeURIComponent(v); } catch (e) { return v; }
      }
    } catch (e) {}
    return '';
  }

  function findToken() {
    // 实测这个平台把 token 放在 **cookie** 里，不是 localStorage —— 这是靠自检
    // 把存储里的键名报回机器人日志才发现的（四种存储都只有 0~2 个键且没有
    // token，cookie 里有一个名为 IMPSA-Token 的）。所以 cookie 优先，存储兜底。
    var ck = readCookie('IMPSA-Token');
    if (ck) { return { token: ck, src: 'cookie' }; }
    var ss = stores();
    for (var i = 0; i < ss.length; i++) {
      try {
        var t = ss[i].getItem('IMPSA-Token');
        if (t) { return { token: t, src: '存储' + i }; }
      } catch (e) {}
    }
    return null;
  }

  // 去重标记：存 token 的短哈希，放在脚本自己写的 cookie 里。
  // 存哈希而不是 token 本身 —— 不能为了去重把凭证再抄一份到别处。
  function fp(t) {
    var h = 5381;
    for (var i = 0; i < t.length; i++) { h = ((h * 33) ^ t.charCodeAt(i)) | 0; }
    return 'h' + (h >>> 0).toString(36);
  }
  function getSentFp() { return readCookie(FP_COOKIE); }
  function setSentFp(v) {
    try { doc().cookie = FP_COOKIE + '=' + v + '; path=/; max-age=604800'; }
    catch (e) {}
  }

  // 解 JWT 的 exp(只读 payload,不验签)。解不出来就返回 0,让调用方别自作主张。
  function jwtExp(t) {
    try {
      var part = t.split('.')[1];
      if (!part) { return 0; }
      part = part.replace(/-/g, '+').replace(/_/g, '/');
      while (part.length % 4) { part += '='; }
      return JSON.parse(atob(part)).exp || 0;
    } catch (e) { return 0; }
  }

  function fixState() {
    var a = (readCookie(FIX_COOKIE) || '').split('.');
    return { n: parseInt(a[0], 10) || 0, t: parseInt(a[1], 10) || 0 };
  }
  function setFixState(n, t) {
    try {
      doc().cookie = FIX_COOKIE + '=' + n + '.' + t + '; path=/; max-age=604800';
    } catch (e) {}
  }

  // 记录用户最后一次操作的时间。自动刷新只在"人没在看/没在动"时才做。
  var lastActive = Date.now();
  ['mousemove', 'keydown', 'click', 'scroll', 'touchstart'].forEach(function (ev) {
    window.addEventListener(ev, function () { lastActive = Date.now(); }, true);
  });

  // token 过期就刷新页面,让前端重新走一遍 OAuth 换新的(登录态还在的话是静默的)。
  // 返回 true 表示已经触发刷新,调用方本轮不要再推旧 token 了。
  function maybeReloadForExpired() {
    var found = findToken();
    if (!found) { return false; }        // 压根没 token 是另一回事,不在这里处理
    var exp = jwtExp(found.token);
    if (!exp) { return false; }          // 解不出来就别乱刷
    var now = Date.now();
    if (now / 1000 < exp) {
      setFixState(0, 0);                 // token 有效,把重试计数清零
      return false;
    }
    // ---- 已过期 ----
    var st = fixState();
    if (st.n >= FIX_MAX_TRIES) {
      // 刷了好几次还是拿不到新的,基本是 jaccount 登录态也没了。
      // 必须停手:否则会变成无限刷新,把用户困在这一页上。
      if (!warnedFixFail) {
        warnedFixFail = true;
        console.warn(TAG + 'token 已过期,但连续 ' + st.n + ' 次自动刷新都没换到新的。'
          + '多半是 jaccount 登录态也失效了,需要你手动登录一次。已停止自动刷新。');
      }
      return false;
    }
    if (now - st.t < FIX_COOLDOWN_MS) { return false; }   // 节流

    // 这站是抢报名的:万一把正在填的报名表刷掉,比 token 过期严重得多。
    // 所以只在"页面在后台"或"前台但 FIX_IDLE_MS 内没人动"时才刷。
    var idle = now - lastActive > FIX_IDLE_MS;
    var hidden = false;
    try { hidden = !!doc().hidden; } catch (e) {}
    if (!hidden && !idle) {
      console.log(TAG + 'token 已过期,但你正在用这个页面,先不自动刷新(免得打断你)');
      return false;
    }

    setFixState(st.n + 1, now);
    console.log(TAG + 'token 已过期,自动刷新页面换取新 token(第 ' + (st.n + 1)
                + ' 次,' + (hidden ? '页面在后台' : '页面已空闲') + ')');
    try { location.reload(); } catch (e) { try { W.location.reload(); } catch (e2) {} }
    return true;
  }

  // 这行是排查用的：控制台里看到它，就说明脚本确实被 Tampermonkey 执行了
  console.log(TAG + '脚本已加载（端口 ' + PORT + '，可用存储 ' + stores().length + ' 种）');

  // 发一个请求给机器人。先走页面自己的 fetch；页面发不出去（浏览器会以
  // Private Network Access / mixed content 等理由拦截，且不给有用的错误信息）
  // 就换 GM_xmlhttpRequest —— 它由 Tampermonkey 扩展发出，不受这些限制。
  // cb(是否成功, 走的哪条通道, 失败原因)
  function send(payload, cb) {
    function gm() {
      if (typeof GM_xmlhttpRequest !== 'function') {
        cb(false, 'none', 'fetch 被浏览器拦了，且 GM_xmlhttpRequest 不可用');
        return;
      }
      GM_xmlhttpRequest({
        method: 'POST',
        url: ENDPOINT,
        headers: { 'Content-Type': 'application/json', 'X-Sync-Key': KEY },
        data: JSON.stringify(payload),
        onload: function (r) {
          var j = {};
          try { j = JSON.parse(r.responseText); } catch (e) {}
          cb(r.status === 200 && j.ok, 'GM',
             'HTTP ' + r.status + ' ' + (j.msg || ''));
        },
        onerror: function () {
          cb(false, 'GM', '连不上 127.0.0.1:' + PORT
             + '（机器人开着吗？端口对吗？发 #二课同步 核对）');
        },
        ontimeout: function () { cb(false, 'GM', '请求超时'); }
      });
    }
    fetch(ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Sync-Key': KEY },
      body: JSON.stringify(payload)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        cb(r.ok && j.ok, 'fetch', 'HTTP ' + r.status + ' ' + (j.msg || ''));
      });
    }).catch(function () { gm(); });
  }

  // 自检上报：把脚本自身状态发回机器人，这样在机器人的日志里就能看到
  // "脚本跑没跑、读没读到 token、哪条通道能用"，不用让人去翻浏览器控制台。
  // 各存储里有多少键、有没有名字像 token 的键。**只报键名不报值** ——
  // 别把凭证写进机器人日志（那正是 #二课token 要脱敏的原因）。
  function storeReport() {
    var ss = stores(), out = [];
    for (var i = 0; i < ss.length; i++) {
      try {
        var names = Object.keys(ss[i]);
        var suspect = names.filter(function (n) {
          return /token|IMPSA|auth/i.test(n);
        });
        out.push(i + ':' + names.length + '键'
                 + (suspect.length ? '(疑似:' + suspect.slice(0, 3).join('|') + ')' : ''));
      } catch (e) { out.push(i + ':读不到'); }
    }
    return out.join(' ');
  }

  function cookieReport() {
    try {
      var ck = (W.document && W.document.cookie) || document.cookie || '';
      if (!ck) { return '空'; }
      var names = ck.split(';').map(function (s) { return s.split('=')[0].trim(); });
      var suspect = names.filter(function (n) {
        return /token|IMPSA|auth|session/i.test(n);
      });
      return names.length + '个'
             + (suspect.length ? '(疑似:' + suspect.join('|') + ')' : '');
    } catch (e) { return '读不到'; }
  }

  function ping() {
    var found = findToken();
    var detail = 'token=' + (found ? ('有' + found.token.length + '字符/源' + found.src)
                                   : '没有')
      + ' | 存储 ' + storeReport()
      + ' | cookie ' + cookieReport()
      + ' | gm=' + (typeof GM_xmlhttpRequest === 'function' ? '可用' : '不可用');
    send({ ping: detail }, function (ok, how, msg) {
      console.log(TAG + '自检：' + detail + ' -> '
                  + (ok ? ('已上报(' + how + ')') : ('上报失败(' + how + ' ' + msg + ')')));
    });
  }
  ping();

  function push(reason) {
    // 先看 token 是不是过期了 —— 过期就触发刷新换新的,本轮不要再推旧的
    if (maybeReloadForExpired()) { return; }
    var found = findToken();
    if (!found) {
      if (!warnedNoToken) {
        warnedNoToken = true;
        console.warn(TAG + 'cookie 和页面存储里都没找到 IMPSA-Token。'
          + '确认已登录 activity.sjtu.edu.cn；如果一直如此，告诉机器人主人。');
      }
      return;
    }
    var token = found.token;
    var mark = fp(token);
    if (getSentFp() === mark) { return; }   // 这个 token 已经推过了

    send({ token: token }, function (ok, how, msg) {
      if (ok) {
        setSentFp(mark);
        console.log(TAG + '✅ 已推送给机器人（' + reason + '，走 ' + how
                    + '，来源 ' + found.src + '）');
      } else {
        console.warn(TAG + '❌ 推送失败（' + reason + '，走 ' + how + '）：' + msg);
      }
    });
  }

  push('页面加载');
  // 登录是跳转后才写 token 的，另外页面开着时 token 也可能被刷新，所以多试几次
  [3, 10, 30, 60, 180].forEach(function (s) {
    setTimeout(function () { push('延时 ' + s + 's'); }, s * 1000);
  });
  setInterval(function () { push('定时'); }, 30 * 60 * 1000);
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) { push('回到页面'); }
  });
})();
"""

STATUS_OPEN = 2                # 查询参数 status=2 → 报名中(可报名)
STATUS_UPCOMING = 4            # 查询参数 status=4 → 即将举办(备查,本插件只用 OPEN)
METHOD_QUOTA = 3               # 报名方式=3 才受名额限制,才按 person_num 判满

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json",
    "Accept-Language": "zh-CN",
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
            json.dump(data, f, ensure_ascii=False)
    except OSError:
        pass


def _save_config(cfg):
    """写配置文件。带缩进 —— 这个文件里写着换 token 的步骤说明,压成一行就没法看了。

    (状态文件用 _save_json 紧凑写无所谓,配置文件不行。)
    """
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _jwt_exp(token):
    """从 JWT 的 payload 里解出过期时间。只读自己手里这串凭证,不验签。

    实测该平台 token 有效期恰好 24 小时(iat→exp 正好 86400 秒),而平台
    **没有刷新端点**。所以必须提前提醒主人换 token —— 只靠 401 兜底的话,
    从过期到主人发现之间新开放的活动会全部漏掉,而第二课堂的名额是先到先得。
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return datetime.fromtimestamp(int(exp)) if exp else None
    except (IndexError, ValueError, TypeError):
        return None


def _extract_token(text):
    """从 #二课token 命令里取出 token。返回 (token, 错误说明)。

    粘进来的 token 常带引号或首尾空白(从剪贴板/控制台复制),一并清掉。
    """
    parts = text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        return None, "没带 token"
    return parts[1].strip().strip('"').strip("'"), None


def _parse_dt(s):
    """平台时间是 'YYYY-MM-DD HH:mm:ss'(北京时间墙钟,无时区),按本地时间解析。

    秒可能缺失(前端格式化时 slice(0,16) 只保留到分钟),所以带秒格式失败后
    回退到无秒格式。
    """
    if not s:
        return None
    txt = str(s).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(txt, fmt)
        except ValueError:
            continue
    return None


def _range(v):
    """时间区间字段解析成 (start, end)。

    正常是 2 元素数组 ['2026-09-20 08:00:00','2026-09-26 23:59:59'],
    但容错处理单值/空值/非法值,避免异常数据把整轮轮询带崩。
    """
    if isinstance(v, (list, tuple)):
        start = _parse_dt(v[0]) if len(v) > 0 else None
        end = _parse_dt(v[1]) if len(v) > 1 else None
        return start, end
    return _parse_dt(v), None


def _fmt_dt(dt) -> str:
    return dt.strftime("%m-%d %H:%M") if dt else "未定"


def _fmt_range(pair) -> str:
    start, end = _range(pair)
    if start and end:
        return f"{_fmt_dt(start)} ~ {_fmt_dt(end)}"
    return _fmt_dt(start or end)


def _remain(dt) -> str:
    """距某时刻还有多久,给人类看的粗略粒度。"""
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


def _is_open(act, now):
    """判断活动当前是否还能报名。返回 (可报名, 不可报名原因)。

    复刻前端详情页的判定链,但只用列表项里拿得到的字段:
    报名未开始 → 报名已结束 → 名额已满(仅 method==3)。
    """
    start, end = _range(act.get("registration_time"))
    if start and now < start:
        return False, "报名未开始"
    if end and now > end:
        return False, "报名已结束"
    # 只有站内报名(method==3)才按名额判满,其它报名方式前端不显示人数比例
    if act.get("method") == METHOD_QUOTA:
        total, used = act.get("person_num"), act.get("signed_up_num")
        if isinstance(total, int) and total > 0 and isinstance(used, int) and used >= total:
            return False, "名额已满"
    return True, ""


def _quota_text(act) -> str:
    """报名人数展示。method 属于 {2,4,6,7} 时前端不显示比例,这里也不显示。"""
    total, used = act.get("person_num"), act.get("signed_up_num")
    if not isinstance(total, int) or total <= 0:
        return ""
    if act.get("method") in (2, 4, 6, 7):
        return ""
    if not isinstance(used, int):
        return f"{total} 人"
    return f"{min(used, total)}/{total} 人"


def _due_tiers(rec, now):
    """这条收藏记录现在该发哪一层提醒。

    返回 (报名开始时间, 要发的层级或 None, 该标记作罢的层级列表)。

    一次跨过多层时(断线/关机后重启,比如开机时只剩 8 分钟就报名了)只发最紧迫的
    那一层,其余标记作罢 —— 否则会一口气补发一串"提前1天/1小时/10分钟"。
    报名已经开始就不补发:报"还有负 5 分钟"没有意义(但仍要标记掉,免得每轮重判)。
    """
    rs = _parse_dt(rec.get("reg_start"))
    if rs is None:
        return None, None, []
    done = rec.get("sent") or {}
    due = sorted((secs, tier) for tier, secs in TIERS
                 if not done.get(tier) and now >= rs - timedelta(seconds=secs))
    if not due:
        return rs, None, []
    mark = [tier for _secs, tier in due]
    if now >= rs:
        return rs, None, mark
    return rs, due[0][1], mark


class SjtuAcademicPlugin(Plugin):
    # #二课token 后面跟的是凭证,别让路由把正文记进日志
    redact_prefixes = ("#二课token",)
    COMMANDS = (
        ("#二课状态", "", True, "查看第二课堂监控状态"),
        ("#二课检查", "", True, "立即检查一次活动"),
        ("#二课类型", "", True, "查看活动类型说明"),
        ("#二课收藏", "", True, "列出收藏的活动及报名开始时间"),
        ("#二课提醒", "", True, "查看收藏活动的报名提醒计划"),
        ("#二课同步", "", True, "用户脚本自动同步 token 的安装说明"),
        ("#二课token", "<token>", True, "更新凭证(必须私聊发,群里会被撤回)"),
    )


    def __init__(self):
        super().__init__()
        # 不依赖 router 的状态放这里,这样脱离 Router 也能单独测试
        self._auth_warned_at = 0.0
        self._checking = asyncio.Lock()
        self._last_sync_at = 0.0     # 用户脚本上次推 token 的时间(限频用)
        self._sync_up = False        # 同步接口是否成功起来了
        self._sync_error = ""        # 起不来时的原因(端口被占等)

    def setup(self, router):
        super().setup(router)
        self._task = asyncio.create_task(self._poll_loop())
        self._sync_task = asyncio.create_task(self._sync_server())

    # ---------- 主循环 ----------
    async def _poll_loop(self):
        await asyncio.sleep(30)  # 等 WS 连接稳定后再做首次检查
        while True:
            interval = POLL_INTERVAL
            try:
                cfg = _load_json(CONFIG_PATH, {})
                interval = int(cfg.get("poll_interval") or POLL_INTERVAL)
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("轮询异常: %s", e)

            # 完整轮询之间用小步长唤醒,见文件头关于"前十分钟"窗口的说明。
            # 每次唤醒只做本地判断(tick_reminders 不发请求),所以加密不增加平台压力。
            interval = max(60, interval)
            slept = 0
            while slept < interval:
                step = min(REMINDER_TICK, interval - slept)
                await asyncio.sleep(step)
                slept += step
                try:
                    await self.tick_reminders()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("提醒滴答异常: %s", e)

    # ---------- 一次完整检查 ----------
    async def check_once(self) -> str:
        """执行一次检查,返回结果描述(也供 #二课检查 命令使用)。"""
        if self._checking.locked():
            return "检查正在进行中,请稍候"
        async with self._checking:
            return await self._check_inner()

    async def _check_inner(self) -> str:
        cfg = _load_json(CONFIG_PATH, {})
        if cfg.get("enabled") is False:
            return "已在 sjtu_academic.json 中关闭(enabled=false)"
        token = (cfg.get("token") or "").strip()
        if not token:
            return "未配置 token(sjtu_academic.json),功能停用中"

        base = (cfg.get("base_url") or DEFAULT_BASE).rstrip("/")
        only_open = cfg.get("only_open") is not False
        types = [t for t in (cfg.get("activity_types") or []) if isinstance(t, int)]

        st = _load_json(STATE_PATH, {})
        seen = set(st.get("seen", []))
        col_items, col_err = [], None
        want_collected = cfg.get("remind_collected") is not False

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout,
                                         headers=self._headers(token)) as s:
            acts, err = await self._fetch(s, base, only_open, types)
            if err is not None:
                return await self._handle_error(err, st, seen)
            # 收藏列表独立于"新活动推送":它拉失败不该拖垮主流程
            if want_collected:
                col_items, col_err = await self._fetch_collected(s, base)

        now = datetime.now()
        st["last_ok"] = time.time()
        st.pop("last_error", None)
        # 拉取成功后顺手检查凭证还剩多久,快过期就提前提醒主人
        await self._maybe_warn_expiry(token, st, cfg)

        # 同步收藏活动的提醒计划。失败时**保留原有计划**(不调 _sync_collected),
        # 否则一次网络抖动就会把所有待提醒的收藏活动清空。
        note = ""
        if want_collected:
            if col_err is not None:
                note = f",收藏列表拉取失败({col_err})"
                log.warning("收藏列表拉取失败,沿用上次的提醒计划: %s", col_err)
            else:
                watching, changed = self._sync_collected(st, col_items, base)
                if watching:
                    note = f",盯 {watching} 个收藏活动"
                if changed:
                    log.info("收藏提醒计划更新:新增/改期 %d 个,共盯 %d 个",
                             changed, watching)

        # 首轮建立基线:发一份当前在报活动的汇总,并把它们全记入 seen,
        # 避免把几十条历史活动当"新增"逐条刷屏。
        if not st.get("seeded"):
            text = self._render_digest(acts, now, base)
            ok = await self._push(text)
            if not ok:
                return "首次汇总发送失败(WS 未连接?),下轮重试"
            for a in acts:
                seen.add(self._key(a))
            st["seeded"] = True
            _save_json(STATE_PATH, self._cap_seen(st, seen))
            log.info("首次运行完成,已记录基线:%d 个在报活动", len(acts))
            return f"首次运行完成,已发汇总(基线 {len(acts)} 个在报活动){note}"

        sent = await self._push_fresh(acts, seen, now, base)
        _save_json(STATE_PATH, self._cap_seen(st, seen))
        return f"检查完成:已推送 {sent} 条{note}"

    # ---------- 增量推送 ----------
    async def _push_fresh(self, acts, seen, now, base) -> int:
        """推送新开放报名的活动。

        发送成功才写入 seen,失败保留待下轮重试(WS 断开时数据不能丢)。
        报名未开始的条目不写 seen —— 等它真正开放时还要靠这一条推送。
        """
        fresh = []
        for a in acts:
            key = self._key(a)
            if key is None or key in seen:
                continue
            openable, _why = _is_open(a, now)
            if not openable:
                continue  # 不写 seen,让它下轮再判一次
            fresh.append((_range(a.get("registration_time"))[0] or now, key,
                          self._render(a, now, base)))

        # 报名时间早的先推,保证"先到先得"的活动按开放顺序到达
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
            log.info("本轮新活动 %d 个,已推送 %d 个", len(fresh), sent)
        return sent

    # ---------- 收藏活动：报名开始提醒 ----------
    def _sync_collected(self, st, items, base) -> tuple:
        """把收藏列表同步成提醒计划,返回 (在盯数量, 新增或改期数量)。

        只盯"报名还没开始"的活动 —— 报名一旦开始,提前提醒就没意义了,顺手清掉。
        已发标记在报名时间没变时沿用;报名时间改了就从零重算,否则改期后会沿用
        旧的已发标记,把提醒整个吞掉(改期恰恰是最需要提醒的情况)。
        """
        old = st.get("collected") or {}
        new, changed = {}, 0
        now = datetime.now()
        for a in items:
            aid = a.get("activity_id") or a.get("id")
            rs, re_ = _range(a.get("registration_time"))
            # 实测有 registration_time 为 [null,null] 的活动(不开放报名),
            # 没有报名开始时间就无从提醒,必须跳过
            if aid is None or rs is None or now >= rs:
                continue
            if re_ is not None and now > re_:
                continue
            key = str(aid)
            rec = {
                "aid": key,
                "name": a.get("name") or "未命名",
                "reg_start": rs.strftime("%Y-%m-%d %H:%M:%S"),
                "url": f"{base}/activity/detail/{key}",
                "sent": {},
            }
            prev = old.get(key)
            if prev and prev.get("reg_start") == rec["reg_start"]:
                rec["sent"] = dict(prev.get("sent") or {})
            else:
                changed += 1
            new[key] = rec
        st["collected"] = new
        return len(new), changed

    async def tick_reminders(self) -> int:
        """给到点的收藏活动发提醒,返回本次发出条数。

        只用状态文件里缓存的报名开始时间算,**不发任何 HTTP 请求** —— 所以能每
        REMINDER_TICK 秒滴答一次,既做到分钟级精确,又不给平台加压力。
        """
        if self._checking.locked():
            return 0   # 正在完整轮询,等它写完状态再滴答,免得互相覆盖
        st = _load_json(STATE_PATH, {})
        col = st.get("collected") or {}
        if not col:
            return 0

        now = datetime.now()
        sent, changed = 0, False
        for rec in col.values():
            rs, fire, mark = _due_tiers(rec, now)
            if not mark:
                continue
            if fire and await self._push(self._render_reminder(rec, rs, fire)):
                sent += 1
                await asyncio.sleep(SLEEP_BETWEEN_MSGS)
            for tier in mark:
                rec.setdefault("sent", {})[tier] = True
            changed = True

        if changed:
            _save_json(STATE_PATH, st)
        if sent:
            log.info("发出 %d 条收藏活动报名提醒", sent)
        return sent

    @staticmethod
    def _render_reminder(rec, rs, tier) -> str:
        return "\n".join([
            f"⭐ 收藏活动 · 报名快开始了（{TIER_TEXT.get(tier, tier)}）",
            f"活动:{rec.get('name') or '未命名'}",
            f"报名开始:{_fmt_dt(rs)}（{_remain(rs)}）",
            f"链接:{rec.get('url') or ''}",
        ])

    @staticmethod
    def _key(a):
        """去重键。首页列表项的 id 就是活动 id(热门/收藏列表才是 activity_id)。"""
        aid = a.get("id")
        return f"act:{aid}" if aid is not None else None

    # ---------- HTTP ----------
    @staticmethod
    def _headers(token):
        h = dict(HEADERS)
        h["Authorization"] = f"Bearer {token}"
        return h

    async def _fetch(self, session, base, only_open, types):
        """拉取"报名中"活动列表,返回 (activities, err)。"""
        params = [("time_sort", "desc")]
        if only_open:
            # status 是数组参数,后端按 PHP 风格解析
            params.append(("status[0]", str(STATUS_OPEN)))
        # 类型筛选是可选项,不配就拉全部类型
        for i, t in enumerate(types):
            params.append((f"activity_type_id[{i}]", str(t)))
        return await self._fetch_list(session, base, "/activity/list/home", params)

    async def _fetch_collected(self, session, base):
        """拉取收藏活动列表,返回 (items, err)。

        收藏项里活动 id 在 `activity_id` 字段(前端拼详情链接用的就是它),
        `id` 也一并兜住(实测两者相同,但字段含义以 `activity_id` 为准)。
        """
        return await self._fetch_list(session, base, "/collect/activity/list", [])

    async def _fetch_list(self, session, base, path, params):
        """分页拉一个 {code, data, total} 结构的列表接口,返回 (items, err)。

        err 为 None / "auth" / "http<code>" / "code<n>" / "baddata" / "network(...)"。
        """
        out, page = [], 1
        while page <= MAX_PAGES:
            query = [("page", str(page)), ("per_page", str(PER_PAGE))] + list(params)
            try:
                async with session.get(base + API + path, params=query) as resp:
                    if resp.status == 401:
                        return [], "auth"
                    if resp.status >= 400:
                        return [], f"http{resp.status}"
                    body = await resp.json(content_type=None)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                return [], f"network({e.__class__.__name__})"

            # 业务码在 body 里,HTTP 恒为 200 —— 见文件头"设计要点"
            if not isinstance(body, dict):
                return [], "badresponse"
            code = body.get("code")
            if code == 401:
                return [], "auth"
            if code != 200:
                return [], f"code{code}"

            data = body.get("data")
            if not isinstance(data, list):
                # 出错时 data 可能是 "";空值当"列表为空"处理,
                # 只有非空又不是列表才算响应异常
                return (out, None) if not data else (out, "baddata")
            out.extend(a for a in data if isinstance(a, dict))

            total = body.get("total")
            if not data or not isinstance(total, int) or len(out) >= total:
                break
            page += 1
        return out, None

    async def _fetch_types(self, session, base):
        """拉活动类型字典,供 #二课类型 展示。返回 (list, err)。"""
        try:
            async with session.get(base + API + "/system/activity_type",
                                   params=[("isAll", "true")]) as resp:
                body = await resp.json(content_type=None)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return [], f"network({e.__class__.__name__})"
        if not isinstance(body, dict):
            return [], "badresponse"
        if body.get("code") == 401:
            return [], "auth"
        if body.get("code") != 200:
            return [], f"code{body.get('code')}"
        data = body.get("data")
        return (data if isinstance(data, list) else []), None

    # ---------- 错误与凭证告警 ----------
    async def _handle_error(self, err, st, seen) -> str:
        st["last_error"] = f"{time.strftime('%m-%d %H:%M')} {err}"
        _save_json(STATE_PATH, self._cap_seen(st, seen))
        log.warning("第二课堂拉取失败: %s", err)
        if err == "auth":
            await self._warn_auth()
            return "凭证失效(401),已提醒主人更新 token"
        return f"拉取失败:{err}"

    async def _maybe_warn_expiry(self, token, st, cfg):
        """token 快过期时提前提醒。同一个 token 只提醒一次(拿 exp 当标记)。

        预告比事后 401 提醒有用得多:该平台 token 只活 24 小时又没有刷新端点,
        等真失效了才说,从失效到主人换好之间新开放的活动就全漏了 —— 而第二
        课堂的名额是先到先得。提前量由配置的 expiry_warn_before_hours 决定。
        """
        exp = _jwt_exp(token)
        if exp is None:
            return
        try:
            lead = float(cfg.get("expiry_warn_before_hours") or 3) * 3600
        except (TypeError, ValueError):
            lead = EXPIRY_WARN_BEFORE
        remain = (exp - datetime.now()).total_seconds()
        if remain <= 0 or remain > lead:
            return
        stamp = int(exp.timestamp())
        if st.get("expiry_warned") == stamp:
            return
        when = (f"{int(remain // 3600)} 小时" if remain >= 3600
                else f"{int(remain // 60)} 分钟")
        if await self._push(
            f"⏰ 第二课堂登录凭证将在 {when} 后过期\n"
            f"（过期时间 {exp.strftime('%m-%d %H:%M')}）\n"
            "这个平台的 token 只活 24 小时,且没有续期接口,过期后会停止推送新活动。\n"
            "\n装了用户脚本的浏览器:打开一次活动网站就行,\n"
            "新 token 会自动同步过来,不用做别的。\n"
            "\n要手动换:在该网站的 F12 → Console 里执行下面这行,token 会进剪贴板\n"
            "(注意 token 存在 cookie 里,不在 localStorage):\n"
            f"  {TOKEN_JS}\n"
            "然后把复制到的内容发给我:\n"
            "  #二课token 粘贴的内容"
        ):
            st["expiry_warned"] = stamp

    async def _warn_auth(self):
        now = time.time()
        if now - self._auth_warned_at < AUTH_WARN_COOLDOWN:
            return
        self._auth_warned_at = now
        if not self.cfg.OWNER_QQ:
            return
        await self._push(
            "⚠️ 第二课堂监控已失效:登录凭证被拒(401)。\n"
            "这个平台没有续期接口,token 过期只能重新登录一次。\n"
            "\n装了用户脚本的浏览器:打开一次活动网站,新 token 会自动同步过来。\n"
            "\n要手动换:在该网站的 F12 → Console 里执行下面这行,\n"
            "token 会进剪贴板(注意 token 存在 cookie 里,不在 localStorage):\n"
            f"  {TOKEN_JS}\n"
            "然后把复制到的内容发给我:\n"
            "  #二课token 粘贴的内容"
        )

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
        # 发送后短暂让出事件循环,若连接已被心跳判死则本轮不算数
        await asyncio.sleep(1)
        if self.client.ws is None:
            return False
        return True

    # ---------- 渲染 ----------
    def _link(self, a, base) -> str:
        return f"{base}/activity/detail/{a.get('id')}"

    def _render(self, a, now, base) -> str:
        lines = [
            "🎪 第二课堂 · 新活动开放报名",
            f"活动:{a.get('name') or '未命名'}",
        ]
        if a.get("sponsor"):
            lines.append(f"主办:{a['sponsor']}")
        lines.append(f"活动时间:{_fmt_range(a.get('activity_time'))}")
        start, end = _range(a.get("registration_time"))
        if end:
            lines.append(f"报名截止:{_fmt_dt(end)}（{_remain(end)}）")
        elif start:
            lines.append(f"报名开始:{_fmt_dt(start)}")
        quota = _quota_text(a)
        if quota:
            lines.append(f"名额:{quota}")
        if a.get("address") and a.get("public") == 2:
            lines.append(f"地点:{a['address']}")
        lines.append(f"链接:{self._link(a, base)}")
        return "\n".join(lines)

    # ---------- 首轮汇总 ----------
    def _render_digest(self, acts, now, base) -> str:
        """首次运行发一份当前在报活动汇总,而不是把历史活动逐条刷屏。"""
        openable = [a for a in acts if _is_open(a, now)[0]]
        openable.sort(key=lambda a: _range(a.get("registration_time"))[1] or
                      datetime.max)
        lines = ["🎪 第二课堂监控已就绪（首次运行）",
                 f"当前有 {len(openable)} 个活动可报名。"]
        if openable:
            lines.append("")
            for a in openable[:DIGEST_LIMIT]:
                end = _range(a.get("registration_time"))[1]
                tail = f"（{_remain(end)}）" if end else ""
                lines.append(f"· {a.get('name') or '未命名'}")
                lines.append(f"  报名截止 {_fmt_dt(end)}{tail}")
            if len(openable) > DIGEST_LIMIT:
                lines.append(f"…另有 {len(openable) - DIGEST_LIMIT} 个")
        lines.append("")
        lines.append("今后只推送新开放报名的活动。发 #二课状态 查看运行情况。")
        return "\n".join(lines)

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
        _save_config(cfg)
        return s

    @staticmethod
    def _cors_headers(request) -> dict:
        """只给 activity.sjtu.edu.cn 放行 —— 别的网页拿不到 CORS 许可。"""
        h = {
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-Sync-Key",
            "Access-Control-Max-Age": "600",
            "Vary": "Origin",
        }
        if request.headers.get("Origin", "") == SYNC_ALLOWED_ORIGIN:
            h["Access-Control-Allow-Origin"] = SYNC_ALLOWED_ORIGIN
            # Chrome 的 Private Network Access:公网页面(https://activity...)
            # 请求本机地址(127.0.0.1)时,预检必须明确同意,否则请求被浏览器
            # 直接拦掉。少了这个头,用户脚本在 Chrome 里会静默失败 ——
            # 服务器端看起来一切正常,但浏览器根本不把请求发出来。
            if request.headers.get(
                    "Access-Control-Request-Private-Network") == "true":
                h["Access-Control-Allow-Private-Network"] = "true"
        return h

    async def _sync_server(self):
        """在 127.0.0.1 上开个只服务本机的接口,接收浏览器推来的 token。

        三层防护(这是本机任何程序都摸得到的入口,不能只靠"别人不知道"):
        ①只绑回环,局域网碰不到;②要 X-Sync-Key;③CORS 只放行
        activity.sjtu.edu.cn,且要求 JSON 正文 —— 非简单请求会先发预检,
        其它网页过不了预检,而表单式的简单请求又因为不是 JSON 被拒。
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

        # 绑定可能因为"上一个实例还没退出"而失败 —— 重启竞态在 Windows 上很常见
        # (2026-10-05 实测:新实例 19:44:26 启动时旧实例仍占着 37218,于是同步接口
        # 永久不可用;而用户脚本那边是静默失败,谁都不会发现,直到 token 又过期)。
        # 所以失败必须重试,不能试一次就放弃。
        site = None
        for attempt in range(1, SYNC_BIND_RETRIES + 1):
            try:
                site = web.TCPSite(runner, "127.0.0.1", port)
                await site.start()
                break
            except OSError as e:
                if attempt >= SYNC_BIND_RETRIES:
                    self._sync_error = (f"端口 {port} 试了 {attempt} 次仍起不来({e})")
                    log.warning("token 同步端口 %d 试了 %d 次都起不来(%s);"
                                "用户脚本同步不可用,自动推送与手动 #二课token 不受影响",
                                port, attempt, e)
                    await runner.cleanup()
                    return
                if attempt == 1:
                    log.info("token 同步端口 %d 被占用(多半是上一个实例还没退出),"
                             "将每 %d 秒重试,最多 %d 次",
                             port, SYNC_BIND_RETRY_DELAY, SYNC_BIND_RETRIES)
                await asyncio.sleep(SYNC_BIND_RETRY_DELAY)

        self._sync_up = True
        log.info("token 同步接口已就绪: http://127.0.0.1:%d/%s",
                 port, USERSCRIPT_FILE)
        try:
            await asyncio.Event().wait()   # 挂到进程退出
        finally:
            await runner.cleanup()

    async def _http_userscript(self, request):
        """把用户脚本发出去(端口和密钥按当前配置预填),便于浏览器里一键安装。"""
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

        # 自检上报:用户脚本加载时把自身状态报过来,方便在机器人日志里定位
        # "脚本到底跑没跑、读没读到 token、哪条通道能用" —— 不必让人去翻浏览器控制台。
        # 放在限频之前,保证诊断不受限频影响。
        if isinstance(body, dict) and body.get("ping"):
            log.info("用户脚本自检: %s", str(body["ping"])[:500])
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

    async def _notify_first_sync(self):
        """只在第一次自动同步成功时说一声,之后不再打扰。

        用户脚本是"装完就不管"的东西,装错了(端口不通、密钥不对)不会有任何
        反馈;所以成功一次必须告知,否则没法确认它到底通了没有。
        状态存独立小文件,避免和主状态文件并发互写。
        """
        st = _load_json(SYNC_STATE_PATH, {})
        if st.get("first_ok_at"):
            return
        st["first_ok_at"] = time.time()
        _save_json(SYNC_STATE_PATH, st)
        await self._push("✅ 第二课堂 token 自动同步已生效\n"
                         "以后浏览器打开活动网站的时候会自动把新 token 推给机器人,\n"
                         "不用再手动复制粘贴了。")

    # ---------- 状态维护 ----------
    @staticmethod
    def _cap_seen(st, seen):
        st = dict(st)
        st["seen"] = list(seen)[-SEEN_CAP:]
        return st

    # ---------- 主人命令 ----------
    async def on_message(self, ev: events.MessageEvent) -> bool:
        text = events.text_of(ev.segments).strip()
        if not text.startswith("#二课"):
            return False
        if not self.router.is_owner(ev.user_id):
            return True  # 仅主人可用,吞掉命令

        if text.startswith("#二课token"):
            # 群里发 token 会被其他人看到;别说更新,先把消息撤回来
            if ev.message_type != "private":
                await self.client.send_action("delete_msg",
                                              {"message_id": ev.message_id})
                await self.router.reply(
                    ev, "⚠️ #二课token 只在私聊里用（已尝试撤回该消息）。\n"
                        "如果没能撤回，请当作这个 token 已经泄露，重新取一个新的。")
                return True
            await self.router.reply(ev, await self._set_token(text))
            return True

        if text.startswith("#二课检查"):
            await self.router.reply(ev, "⏳ 正在检查第二课堂…")
            result = await self.check_once()
            await self.router.reply(ev, f"第二课堂检查:{result}")
            return True

        cfg = _load_json(CONFIG_PATH, {})
        st = _load_json(STATE_PATH, {})

        if text.startswith("#二课类型"):
            await self.router.reply(ev, await self._types_text(cfg))
            return True

        if text.startswith("#二课收藏"):
            await self.router.reply(ev, await self._collection_text(cfg, st))
            return True

        if text.startswith("#二课提醒"):
            await self.router.reply(ev, self._reminders_text(cfg, st))
            return True

        if text.startswith("#二课同步"):
            await self.router.reply(ev, self._sync_text(cfg))
            return True

        # #二课状态
        token = (cfg.get("token") or "").strip()
        types = [t for t in (cfg.get("activity_types") or []) if isinstance(t, int)]
        lines = [
            f"凭证:{'✅ 已配置' if token else '❌ 未配置'}",
            f"主机:{(cfg.get('base_url') or DEFAULT_BASE).rstrip('/')}",
            f"范围:{'仅可报名' if cfg.get('only_open') is not False else '全部活动'}"
            f" · 类型:{'全部' if not types else ','.join(str(t) for t in types)}",
            f"上次成功:{_fmt_dt(datetime.fromtimestamp(st['last_ok'])) if st.get('last_ok') else '从未'}",
            f"已记录:{len(st.get('seen', []))} 个活动",
            f"轮询间隔:{int(cfg.get('poll_interval') or POLL_INTERVAL) // 60} 分钟",
        ]
        if not st.get("seeded"):
            lines.append("状态:尚未建立基线(下轮首推会发汇总)")
        if st.get("last_error"):
            lines.append(f"最近错误:{st['last_error']}")
        await self.router.reply(ev, "#二课状态\n" + "\n".join(lines))
        return True

    async def _set_token(self, text) -> str:
        """#二课token <新token>：校验后写进配置。

        该平台 token 只活 24 小时、每天都要换,所以让主人把浏览器里复制到的
        内容直接发过来最省事,不必每天去编辑 json 文件。

        写之前一定实测一次:先解 JWT 看有没有过期,再真拉一次列表确认平台认
        这个 token。存下一个坏凭证比不存更糟 —— 会静默停摆到下次 401 才发现。
        """
        new, _ = _extract_token(text)
        if new is None:
            return (
                "#二课token\n"
                "用法：把浏览器里复制到的 token 直接跟在命令后面发过来\n"
                "  #二课token eyJ0eXAiOiJKV1Qi...\n\n"
                "取 token：浏览器登录 https://activity.sjtu.edu.cn →\n"
                "F12 → Console 里执行下面这行（token 存在 cookie 里，不在 localStorage）：\n"
                f"  {TOKEN_JS}"
            )

        _ok, msg = await self._apply_token(new)
        return "#二课token\n" + msg

    async def _apply_token(self, new):
        """校验并写入新 token。返回 (是否成功, 面向主人的说明)。

        校验三步:①解 JWT 看能不能解出有效期;②看是否已过期;③**真拉一次活动
        列表**确认平台认它。存下一个坏凭证比不存更糟 —— 会静默停摆到下次 401
        才发现。QQ 命令(#二课token)和浏览器用户脚本的 HTTP 入口都走这里,
        所以两个入口的校验强度完全一致。
        """
        exp = _jwt_exp(new)
        if exp is None:
            return False, "❌ 这不像一个有效 token（解不出有效期），配置没有改动。"
        if exp <= datetime.now():
            return False, (f"❌ 这个 token 已经过期了（{exp.strftime('%m-%d %H:%M')}），"
                           "请在浏览器重新登录后再取。")

        cfg = _load_json(CONFIG_PATH, {})
        base = (cfg.get("base_url") or DEFAULT_BASE).rstrip("/")
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout,
                                         headers=self._headers(new)) as s:
            acts, err = await self._fetch(s, base, True, [])

        if err == "auth":
            return False, ("❌ 平台拒绝了这个 token（401），配置没有改动。\n"
                           "多半是复制时漏了字符，请重新复制一次完整的内容。")
        if err is not None:
            return False, (f"⚠️ 验证时拉取失败（{err}），无法确认 token 有效。\n"
                           "为避免存下一个坏凭证，这次没有改动配置，请稍后重试。")

        cfg["token"] = new
        _save_config(cfg)
        hours = int((exp - datetime.now()).total_seconds() // 3600)
        log.info("token 已更新(有效期至 %s)", exp)
        return True, (f"✅ 已更新（实测能拉到 {len(acts)} 个在报活动）\n"
                      f"有效期至 {exp.strftime('%m-%d %H:%M')}（还剩 {hours} 小时）\n"
                      "已立即生效,无需重启。到快过期时我会再提醒你。")

    @staticmethod
    def _reminders_text(cfg, st) -> str:
        """#二课提醒：列出收藏活动的报名提醒计划。

        提醒是几小时后才发的,没有这个命令主人没法确认"到底盯上了没有"。
        """
        if cfg.get("remind_collected") is False:
            return ("#二课提醒\n"
                    "收藏提醒已在 sjtu_academic.json 中关闭(remind_collected=false)。")
        col = st.get("collected") or {}
        if not col:
            return ("#二课提醒\n"
                    "当前没有待提醒的收藏活动。\n"
                    "在平台上收藏活动后,只要它的报名还没开始,就会在报名开始前\n"
                    "1 天 / 1 小时 / 10 分钟分别提醒你。\n"
                    "(报名已开始、或没有报名时间的活动不会盯)")
        now = datetime.now()
        lines = [f"#二课提醒\n正在盯 {len(col)} 个收藏活动的报名开始:"]
        for rec in sorted(col.values(), key=lambda r: r.get("reg_start") or ""):
            rs = _parse_dt(rec.get("reg_start"))
            lines.append(f"· {rec.get('name') or '未命名'}")
            lines.append(f"  报名开始 {_fmt_dt(rs)}（{_remain(rs)}）")
            lines.append("  " + SjtuAcademicPlugin._tier_status(rec, rs, now))
        return "\n".join(lines)

    @staticmethod
    def _tier_status(rec, rs, now) -> str:
        """渲染「已提醒:X；待提醒:Y」这一行。#二课提醒 和 #二课收藏 共用。"""
        done = (rec or {}).get("sent") or {}
        fired = [TIER_TEXT[t] for t, _ in TIERS if done.get(t)]
        todo = [TIER_TEXT[t] for t, secs in TIERS
                if not done.get(t) and rs and now < rs - timedelta(seconds=secs)]
        return f"已提醒:{'、'.join(fired) or '无'}；待提醒:{'、'.join(todo) or '无'}"

    async def _collection_text(self, cfg, st) -> str:
        """#二课收藏：列出收藏的活动及其报名开始时间。

        与 #二课提醒 的区别:那个只列"会被提醒的"(报名尚未开始且有时间),
        这个列**报名期内的**全部收藏 —— 包括没有报名时间的(线下报名那类)
        和报名已开始但还没截止的;报名已截止的按主人要求剔除,不列出。

        现拉一次收藏接口(所以要点时间),换来的是最新数据:刚在平台上收藏的活动
        立刻就能看到。拉不动时错误信息里会指向 #二课提醒(那个用本地缓存)。
        """
        token = (cfg.get("token") or "").strip()
        if not token:
            return "#二课收藏\n❌ 未配置 token,无法拉取收藏列表"
        base = (cfg.get("base_url") or DEFAULT_BASE).rstrip("/")
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout,
                                         headers=self._headers(token)) as s:
            items, err = await self._fetch_collected(s, base)

        if err == "auth":
            await self._warn_auth()
            return ("#二课收藏\n⚠️ 凭证失效(401),请更新 token。\n"
                    "(已在盯的提醒计划可发 #二课提醒,那个用本地缓存不依赖网络)")
        if err is not None:
            return (f"#二课收藏\n拉取失败:{err}\n"
                    "(已在盯的提醒计划可发 #二课提醒,那个用本地缓存不依赖网络)")
        return self._render_collection(items, st, base)

    @staticmethod
    def _render_collection(items, st, base) -> str:
        """渲染 #二课收藏 正文。抽成纯函数是为了能离线测(带网络那层没法单测)。

        **报名已截止的不列出**(主人要求):报名窗口存在、且结束时间已过就剔除。
        没有报名时间的(registration_time 为 [null,null],实测是"线下报名"那类)
        判断不了截止与否,保守保留 —— 否则会凭空吞掉可能还有效的活动。
        剔除数量会写在标题里,避免"少了几条"看起来像出了 bug。
        """
        if not items:
            return ("#二课收藏\n"
                    "你还没有收藏任何活动。\n"
                    "在平台的活动详情页点收藏按钮即可;收藏后只要报名还没开始,\n"
                    "就会在报名开始前 1 天 / 1 小时 / 10 分钟提醒你。")

        watched = st.get("collected") or {}
        now = datetime.now()
        rows, closed = [], 0
        for a in items:
            aid = a.get("activity_id") or a.get("id")
            rs, re_ = _range(a.get("registration_time"))
            if re_ is not None and now > re_:
                closed += 1          # 报名窗口已关
                continue
            rows.append((rs, re_, aid, a))

        dropped = f"（已剔除 {closed} 个报名已截止的）" if closed else ""
        if not rows:
            return ("#二课收藏\n"
                    f"没有还在报名期的收藏活动。{dropped}\n"
                    "可以去平台上看看别的活动,或有新活动开放报名时会提醒你。")

        # 有报名时间的按开始时间排序,没有报名时间的排到最后
        rows.sort(key=lambda r: (r[0] is None, r[0] or datetime.max))

        lines = [f"#二课收藏\n共 {len(rows)} 个收藏活动{dropped}:"]
        for rs, re_, aid, a in rows:
            lines.append("")
            lines.append(f"· {a.get('name') or '未命名'}")
            if rs:
                span = _fmt_dt(rs) + (f" ~ {_fmt_dt(re_)}" if re_ else "")
                lines.append(f"  报名开始 {span}（{_remain(rs)}）")
            else:
                lines.append("  无报名时间（不开放线上报名，不会提醒）")
            rec = watched.get(str(aid))
            if rec:
                lines.append("  " + SjtuAcademicPlugin._tier_status(rec, rs, now))
            elif rs and now >= rs:
                lines.append("  报名已开始（不再提前提醒）")
            lines.append(f"  链接:{base}/activity/detail/{aid}")
        return "\n".join(lines)

    def _sync_text(self, cfg) -> str:
        """#二课同步：给出用户脚本的安装地址与当前参数。

        脚本装在浏览器里,出问题(端口不通、密钥不对)时主人看不到任何反馈,
        所以这里必须把地址和状态都摊开,并且实话实说接口有没有起来。
        """
        if cfg.get("sync_enabled") is False:
            return ("#二课同步\n"
                    "用户脚本同步已在 sjtu_academic.json 中关闭(sync_enabled=false)。")
        port = self._sync_port(cfg)
        secret = self._sync_secret(cfg)
        lines = [
            "#二课同步",
            "在浏览器里装一次用户脚本,之后打开第二课堂网站时会自动把 token",
            "推给机器人,不用再手动复制粘贴。",
            "",
            "安装地址(用装好 Tampermonkey 的浏览器打开):",
            f"  http://127.0.0.1:{port}/{USERSCRIPT_FILE}",
            "",
            "Tampermonkey 会弹出安装页;如果只是显示了脚本源码,",
            "就全选复制,在 Tampermonkey 里新建脚本粘贴进去。",
            "",
            f"端口 {port}、密钥 {secret[:10]}… 都已预填进脚本,不用手填。",
            "脚本只读取你自己浏览器里的 IMPSA-Token 推到本机,",
            "不存账号密码,也不经过任何第三方服务器。",
        ]
        if not self._sync_up:
            lines.insert(1, f"⚠️ 同步接口当前没起来:{self._sync_error or '尚未启动'}")
            lines.insert(2, "(端口被占用?把 sjtu_academic.json 的 sync_port 换一个再重启)")
            lines.insert(3, "")
        return "\n".join(lines)

    async def _types_text(self, cfg) -> str:
        """列出活动类型及编号,方便填 activity_types 做筛选。"""
        token = (cfg.get("token") or "").strip()
        if not token:
            return "#二课类型\n❌ 未配置 token,无法拉取类型"
        base = (cfg.get("base_url") or DEFAULT_BASE).rstrip("/")
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout,
                                         headers=self._headers(token)) as s:
            data, err = await self._fetch_types(s, base)
        if err == "auth":
            await self._warn_auth()
            return "#二课类型\n⚠️ 凭证失效(401),请更新 token"
        if err is not None:
            return f"#二课类型\n拉取失败:{err}"
        if not data:
            return "#二课类型\n(类型字典为空)"

        cur = [t for t in (cfg.get("activity_types") or []) if isinstance(t, int)]
        lines = ["#二课类型"]
        for t in data:
            tid, name = t.get("id"), t.get("label") or t.get("name") or "未命名"
            if t.get("status"):
                mark = "✅" if tid in cur else "　"
                lines.append(f"{mark} {name}（activity_type_id={tid}）")
            children = t.get("children")
            if isinstance(children, list):
                for c in children:
                    if not isinstance(c, dict) or not c.get("status"):
                        continue
                    cid, cname = c.get("id"), c.get("label") or c.get("name") or "未命名"
                    mark = "✅" if cid in cur else "　"
                    lines.append(f"{mark} 　{cname}（{cid}）")
        # 类型有 60+ 个,全列会刷屏;截断并说明还剩多少
        if len(lines) > TYPES_MAX_LINES:
            hidden = len(lines) - TYPES_MAX_LINES
            lines = lines[:TYPES_MAX_LINES]
            lines.append(f"…另有 {hidden} 个类型未列出")
        lines.append("✅=正在监测。留空 activity_types 表示全部类型。")
        return "\n".join(lines)
