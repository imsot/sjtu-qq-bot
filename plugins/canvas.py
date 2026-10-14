"""SJTU Canvas 教务监控：轮询课程公告/作业/课件/成绩,新内容私聊推送主人。

依赖：plugins/canvas.json 里的 token(推荐)或 cookie(备选)。
  在浏览器登录 https://oc.sjtu.edu.cn → /profile/settings → Approved Integrations
  → + New Access Token 生成,复制进 canvas.json 的 "token" 字段即可。
  token 拿不到时,退而求其次可填 "cookie": "canvas_session=xxx"。

流程：每 POLL_INTERVAL 秒串行拉取一次 → 与 .canvas_state.json 去重比对
→ 首次运行发一份待办汇总(不逐条刷屏) → 之后只推增量。
另有独立的时间驱动截止提醒：提前 24h / 提前 2h / 每晚汇总明日截止。

主人命令：#Canvas状态 查看运行情况；#Canvas检查 立即轮询一次。

设计要点(踩过的坑,勿改)：
- 所有请求串行。Canvas 是动态成本限流,官方文档明确"任何时刻只有一个并发
  请求的客户端几乎不会被限流",并行反而要吃额外预检惩罚。
- 分页只跟随 Link 头的 rel="next",绝不手工拼页码(URL 不透明)。
- token 只走 Authorization 头,不放 query param,否则分页返回的 Link 会丢 token。
- 403 和 429 都要当限流处理(不同版本文档说法不一致)。
- 发送成功才写 seen,WS 断开时保留待发项下轮重试——因为教务信息错过代价高。
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta

import aiohttp

from bot import events
from bot.router import Plugin

log = logging.getLogger("canvas")

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PLUGIN_DIR, "canvas.json")
STATE_PATH = os.path.join(PLUGIN_DIR, ".canvas_state.json")

DEFAULT_BASE = "https://oc.sjtu.edu.cn"
POLL_INTERVAL = 900            # 轮询间隔(秒)=15 分钟
MAX_PAGES = 20                 # 分页硬上限,防失控
PER_PAGE = 100                 # Canvas 默认每页只有 10 条,必须显式放大
AUTH_WARN_COOLDOWN = 6 * 3600  # 凭证失效提醒间隔(秒)
SEEN_CAP = 3000                # 去重列表上限
MAX_COURSES = 60               # 课程数上限,防异常账号拖垮轮询
RATE_FLOOR = 100               # 限流余量低于此值则本轮降频
SLEEP_BETWEEN_MSGS = 2         # 条与条之间的间隔,防风控
DAILY_SUMMARY_HOUR = 22        # 每晚汇总明日截止的时间点

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json",
}

# 各监测项的默认开关,可被 canvas.json 覆盖
DEFAULT_WATCH = {
    "announcements": True,
    "assignments": True,
    "modules": True,
    "files": False,   # 默认关：模块已覆盖大部分课件,且 files 噪音大(老师批量传素材)
    "grades": True,
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


def _parse_iso(s):
    """Canvas 的时间是 ISO8601 UTC(如 2026-09-25T15:59:00Z),转成本地时间。"""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone()
    except (ValueError, AttributeError, TypeError):
        return None


def _fmt_dt(dt) -> str:
    return dt.strftime("%m-%d %H:%M") if dt else "未定"


def _remain(dt) -> str:
    """距截止还有多久,给人类看的粗略粒度。"""
    if dt is None:
        return "无截止时间"
    secs = (dt - datetime.now(dt.tzinfo)).total_seconds()
    if secs < 0:
        return "已截止"
    if secs >= 86400:
        return f"还剩 {int(secs // 86400)} 天"
    if secs >= 3600:
        return f"还剩 {int(secs // 3600)} 小时"
    return f"还剩 {int(secs // 60)} 分钟"


def _next_link(header):
    """从 Link 头里取 rel="next" 的 URL。头部大小写不敏感,URL 视为不透明。"""
    if not header:
        return None
    for part in header.split(","):
        segs = part.split(";")
        if len(segs) < 2:
            continue
        url = segs[0].strip().strip("<>")
        if any(s.strip() == 'rel="next"' for s in segs[1:]):
            return url
    return None


class CanvasPlugin(Plugin):
    COMMANDS = (
        ("#Canvas状态", "", True, "查看 Canvas 监控状态"),
        ("#Canvas检查", "", True, "立即检查一次公告/作业/课件/成绩"),
    )

    def setup(self, router):
        super().setup(router)
        self._auth_warned_at = 0.0
        self._checking = asyncio.Lock()
        self._last_rate = None
        self._task = asyncio.create_task(self._poll_loop())

    # ---------- 主循环 ----------
    async def _poll_loop(self):
        await asyncio.sleep(20)  # 等 WS 连接稳定后再做首次检查
        while True:
            interval = POLL_INTERVAL
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("轮询异常: %s", e)
            # 限流余量偏低时本轮降频,避免把桶打空
            if self._last_rate is not None and self._last_rate < RATE_FLOOR:
                interval = POLL_INTERVAL * 2
                log.warning("Canvas 限流余量偏低(%s),本轮降频至 %d 秒",
                            self._last_rate, interval)
            await asyncio.sleep(interval)

    # ---------- 一次完整检查 ----------
    async def check_once(self) -> str:
        """执行一次检查,返回结果描述(也供 #Canvas检查 命令使用)。"""
        if self._checking.locked():
            return "检查正在进行中,请稍候"
        async with self._checking:
            return await self._check_inner()

    async def _check_inner(self) -> str:
        cfg = _load_json(CONFIG_PATH, {})
        cred = self._credentials(cfg)
        if cred is None:
            return "未配置凭证(canvas.json),功能停用中"
        if cfg.get("enabled") is False:
            return "已在 canvas.json 中关闭(enabled=false)"

        base = (cfg.get("base_url") or DEFAULT_BASE).rstrip("/")
        watch = dict(DEFAULT_WATCH)
        watch.update({k: bool(v) for k, v in (cfg.get("watch") or {}).items()})

        st = _load_json(STATE_PATH, {})
        seen = set(st.get("seen", []))

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout,
                                         headers=self._headers(cred)) as s:
            # 注意:_get 统一返回列表,单对象端点也包在列表里
            me_list, err = await self._get(s, base, "/api/v1/users/self")
            if err is not None:
                return await self._handle_error(err, st, seen)
            me = me_list[0] if me_list else {}

            courses, err = await self._get(s, base, "/api/v1/courses", [
                ("enrollment_state", "active"), ("per_page", str(PER_PAGE))])
            if err is not None:
                return await self._handle_error(err, st, seen)
            courses = [c for c in courses if isinstance(c, dict) and c.get("id")]
            courses = courses[:MAX_COURSES]
            names = {c["id"]: (c.get("name") or c.get("course_code") or f"课程{c['id']}")
                     for c in courses}

            # 下面全部串行拉取(见文件头"设计要点")
            anns, assigns, items, files = [], [], [], []
            for c in courses:
                cid = c["id"]
                if watch["announcements"]:
                    data, err = await self._get(
                        s, base, f"/api/v1/courses/{cid}/discussion_topics",
                        [("only_announcements", "true"), ("per_page", str(PER_PAGE))])
                    if err is not None:
                        return await self._handle_error(err, st, seen)
                    for a in data:
                        if isinstance(a, dict):
                            a["_course"] = cid
                            anns.append(a)
                if watch["assignments"] or watch["grades"]:
                    data, err = await self._get(
                        s, base, f"/api/v1/courses/{cid}/assignments",
                        [("per_page", str(PER_PAGE)), ("include[]", "submission")])
                    if err is not None:
                        return await self._handle_error(err, st, seen)
                    for a in data:
                        if isinstance(a, dict):
                            a["_course"] = cid
                            assigns.append(a)
                if watch["modules"]:
                    data, err = await self._get(
                        s, base, f"/api/v1/courses/{cid}/modules",
                        [("per_page", str(PER_PAGE)), ("include[]", "items")])
                    if err is not None:
                        return await self._handle_error(err, st, seen)
                    for m in data:
                        if isinstance(m, dict):
                            m["_course"] = cid
                            items.append(m)
                if watch["files"]:
                    data, err = await self._get(
                        s, base, f"/api/v1/courses/{cid}/files",
                        [("per_page", str(PER_PAGE)), ("sort", "created_at"),
                         ("order", "desc")])
                    if err is not None:
                        return await self._handle_error(err, st, seen)
                    for f in data:
                        if isinstance(f, dict):
                            f["_course"] = cid
                            files.append(f)

        st["last_ok"] = time.time()
        st.pop("last_error", None)

        if not st.get("seeded"):
            text = self._render_digest(me, names, anns, assigns)
            if text is None:
                text = "📚 Canvas 监控已就绪,但当前没有待办事项。今后只推送新变化。"
            ok = await self._push(text)
            if not ok:
                return "首次汇总发送失败(WS 未连接?),下轮重试"
            # 首轮把所有现有内容记为已见,今后只推真正的增量
            for a in anns:
                seen.add(f"ann:{a.get('id')}")
            for a in assigns:
                seen.add(f"asg:{a.get('id')}")
            for m in items:
                seen.add(f"mod:{m.get('id')}")
            for f in files:
                seen.add(f"file:{f.get('id')}")
            self._seed_grades(assigns, seen)
            st["seeded"] = True
            _save_json(STATE_PATH, self._cap_seen(st, seen))
            log.info("首次运行完成,已记录基线:公告 %d/作业 %d/模块 %d/文件 %d",
                     len(anns), len(assigns), len(items), len(files))
            return (f"首次运行完成,已发汇总。基线:公告 {len(anns)}、"
                    f"作业 {len(assigns)}、模块 {len(items)}、文件 {len(files)}")

        sent = await self._push_fresh(names, anns, assigns, items, files, seen,
                                      watch["grades"])
        await self._check_deadlines(names, assigns, st, seen)
        await self._maybe_daily_summary(names, assigns, st)

        _save_json(STATE_PATH, self._cap_seen(st, seen))
        return f"检查完成:已推送 {sent} 条"

    # ---------- 增量推送 ----------
    async def _push_fresh(self, names, anns, assigns, items, files, seen, watch_grades) -> int:
        """按时间顺序推送未见过的条目;发送成功才写入 seen,失败保留待下轮重试。"""
        pending = []
        for a in anns:
            pending.append((_parse_iso(a.get("posted_at")) or datetime.min.replace(
                tzinfo=datetime.now().astimezone().tzinfo), f"ann:{a.get('id')}",
                self._render_ann(a, names)))
        for a in assigns:
            pending.append((_parse_iso(a.get("created_at")) or datetime.min.replace(
                tzinfo=datetime.now().astimezone().tzinfo), f"asg:{a.get('id')}",
                self._render_asg(a, names)))
        for m in items:
            for it in (m.get("items") or []):
                if not isinstance(it, dict):
                    continue
                # SubHeader 是模块里的分隔标题(如"第一周"),不是内容,推了纯属噪音
                if it.get("type") == "SubHeader":
                    continue
                pending.append((datetime.min.replace(tzinfo=datetime.now().astimezone().tzinfo),
                                f"mod:{it.get('id')}", self._render_item(m, it, names)))
        for f in files:
            pending.append((_parse_iso(f.get("created_at")) or datetime.min.replace(
                tzinfo=datetime.now().astimezone().tzinfo), f"file:{f.get('id')}",
                self._render_file(f, names)))

        fresh = [(k, t) for _, k, t in sorted(pending, key=lambda x: x[0])
                 if k not in seen and t]
        if watch_grades:
            fresh += self._fresh_grades(assigns, names, seen)

        sent = 0
        for key, text in fresh:
            ok = await self._push(text)
            if not ok:
                log.warning("第 %d 条推送失败,下轮重试", sent + 1)
                break  # WS 异常时保留未发部分,下轮重试
            seen.add(key)
            sent += 1
            await asyncio.sleep(SLEEP_BETWEEN_MSGS)
        if fresh:
            log.info("本轮新内容 %d 条,已推送 %d 条", len(fresh), sent)
        return sent

    # ---------- 截止提醒(独立的时间驱动,不走增量去重) ----------
    async def _check_deadlines(self, names, assigns, st, seen):
        dl = st.get("deadlines") or {}
        now = datetime.now().astimezone()
        changed = False

        for a in assigns:
            due = _parse_iso(a.get("due_at"))
            if due is None:
                continue
            aid, cid = a.get("id"), a.get("_course")
            key = f"{cid}_{aid}"
            rec = dl.get(key) or {}
            remain = (due - now).total_seconds()

            if remain < 0:
                if key in dl:  # 已截止,清掉记录避免无限增长
                    dl.pop(key, None)
                    changed = True
                continue

            # 提前 24 小时
            if remain <= 24 * 3600 and not rec.get("w24"):
                if await self._push(self._render_deadline(a, names, due, "24 小时")):
                    rec["w24"] = True
                    changed = True
                    await asyncio.sleep(SLEEP_BETWEEN_MSGS)
            # 提前 2 小时
            if remain <= 2 * 3600 and not rec.get("w2"):
                if await self._push(self._render_deadline(a, names, due, "2 小时")):
                    rec["w2"] = True
                    changed = True
                    await asyncio.sleep(SLEEP_BETWEEN_MSGS)

            if rec:
                rec["due"] = a.get("due_at")
                dl[key] = rec
        if changed:
            st["deadlines"] = dl

    async def _maybe_daily_summary(self, names, assigns, st):
        """每晚汇总明天截止的作业,比逐条提醒更不吵。"""
        now = datetime.now().astimezone()
        today = now.strftime("%Y-%m-%d")
        if now.hour != DAILY_SUMMARY_HOUR or st.get("last_daily") == today:
            return
        tomorrow = (now + timedelta(days=1)).date()
        due_tomorrow = []
        for a in assigns:
            due = _parse_iso(a.get("due_at"))
            if due and due.date() == tomorrow:
                due_tomorrow.append((due, a))
        if not due_tomorrow:
            st["last_daily"] = today
            return
        lines = [f"🌙 明天有 {len(due_tomorrow)} 个作业截止"]
        for due, a in sorted(due_tomorrow, key=lambda x: x[0]):
            lines.append(f"· {names.get(a.get('_course'), '?')} · {a.get('name') or '未命名'}"
                         f"  截止 {due.strftime('%H:%M')}")
        if await self._push("\n".join(lines)):
            st["last_daily"] = today

    # ---------- HTTP ----------
    @staticmethod
    def _credentials(cfg):
        token = (cfg.get("token") or "").strip()
        if token:
            return ("token", token)
        cookie = (cfg.get("cookie") or "").strip()
        if cookie:
            return ("cookie", cookie)
        return None

    @staticmethod
    def _headers(cred):
        h = dict(HEADERS)
        kind, val = cred
        if kind == "token":
            # 只走 Authorization 头:放 query param 会导致分页 Link 丢 token
            h["Authorization"] = f"Bearer {val}"
        else:
            # 两种粘贴方式都接受:整串 Cookie 头(含多个 cookie),
            # 或只粘贴 canvas_session 的值(没有 "=" 时自动补前缀)
            h["Cookie"] = val if "=" in val else f"canvas_session={val}"
        return h

    async def _get(self, session, base, path, params=None):
        """GET 并自动跟随 Link 头的 rel=next 分页。返回 (data_list, err)。

        err 为 None / "auth" / "ratelimit" / "http<code>" / "network"。
        全程串行,不并发(见文件头"设计要点")。
        """
        url = path if path.startswith("http") else base + path
        out, pages = [], 0
        while url and pages < MAX_PAGES:
            try:
                # 只有第一页带 params;后续页的 URL 已自带查询串,不可重复拼接
                async with session.get(url, params=params if pages == 0 else None) as resp:
                    self._note_rate(resp.headers.get("X-Rate-Limit-Remaining"))
                    if resp.status == 401:
                        return [], "auth"
                    if resp.status in (403, 429):
                        # 403 与 429 都可能是限流,不同版本文档说法不一致
                        return [], "ratelimit"
                    if resp.status >= 400:
                        return [], f"http{resp.status}"
                    data = await resp.json(content_type=None)
                    nxt = _next_link(resp.headers.get("Link"))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                return [], f"network({e.__class__.__name__})"
            if isinstance(data, list):
                out.extend(data)
            elif isinstance(data, dict):
                out.append(data)
            pages += 1
            url = nxt
        return out, None

    def _note_rate(self, remaining):
        try:
            self._last_rate = float(remaining) if remaining is not None else self._last_rate
        except (TypeError, ValueError):
            pass

    # ---------- 错误与凭证告警 ----------
    async def _handle_error(self, err, st, seen) -> str:
        st["last_error"] = f"{time.strftime('%m-%d %H:%M')} {err}"
        _save_json(STATE_PATH, self._cap_seen(st, seen))
        log.warning("Canvas 拉取失败: %s", err)
        if err == "auth":
            await self._warn_auth()
            return "凭证失效(401),已提醒主人更新 token"
        if err == "ratelimit":
            return "触发 Canvas 限流(403/429),本轮跳过,下轮降频重试"
        return f"拉取失败:{err}"

    async def _warn_auth(self):
        now = time.time()
        if now - self._auth_warned_at < AUTH_WARN_COOLDOWN:
            return
        self._auth_warned_at = now
        if not self.cfg.OWNER_QQ:
            return
        text = (
            "⚠️ Canvas 监控已失效:凭证被拒(401)。\n"
            "修复方法:浏览器登录 https://oc.sjtu.edu.cn → /profile/settings →\n"
            "Approved Integrations → + New Access Token 新建一个,\n"
            "复制进 plugins/canvas.json 的 \"token\" 字段后重启机器人。\n"
            "(Canvas 对学生签发的 token 最长 120 天,到期需重签)"
        )
        await self._push(text)

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
        # 2026-08-25 教训:发送后短暂让出事件循环,若连接已被心跳判死则本轮不算数
        await asyncio.sleep(1)
        if self.client.ws is None:
            return False
        return True

    # ---------- 渲染 ----------
    @staticmethod
    def _cname(names, a):
        return names.get(a.get("_course"), "未知课程")

    def _render_ann(self, a, names) -> str:
        return "\n".join([
            f"📢 新课公告 · {self._cname(names, a)}",
            f"标题:{a.get('title') or '无标题'}",
            f"时间:{_fmt_dt(_parse_iso(a.get('posted_at')))}",
            f"链接:{a.get('html_url') or ''}",
        ])

    def _render_asg(self, a, names) -> str:
        due = _parse_iso(a.get("due_at"))
        lines = [
            f"📝 新作业发布 · {self._cname(names, a)}",
            f"作业:{a.get('name') or '未命名'}",
            f"截止:{_fmt_dt(due)}（{_remain(due)}）" if due else "截止:无截止时间",
        ]
        if a.get("points_possible") is not None:
            lines.append(f"分值:{a['points_possible']}")
        lines.append(f"链接:{a.get('html_url') or ''}")
        return "\n".join(lines)

    def _render_item(self, m, it, names) -> str:
        return "\n".join([
            f"📚 课程内容更新 · {self._cname(names, m)}",
            f"模块:{m.get('name') or '未命名模块'}",
            f"新增:{it.get('title') or '未命名'}",
            f"链接:{it.get('html_url') or ''}",
        ])

    def _render_file(self, f, names) -> str:
        return "\n".join([
            f"📎 新课件 · {self._cname(names, f)}",
            f"文件:{f.get('display_name') or '未命名'}",
            f"时间:{_fmt_dt(_parse_iso(f.get('created_at')))}",
            f"链接:{f.get('html_url') or ''}",
        ])

    def _render_deadline(self, a, names, due, label) -> str:
        return "\n".join([
            f"⏰ 作业即将截止（还剩 {label}）",
            f"{self._cname(names, a)} · {a.get('name') or '未命名'}",
            f"截止:{_fmt_dt(due)}",
            f"链接:{a.get('html_url') or ''}",
        ])

    @staticmethod
    def _grade_key(a):
        sub = a.get("submission") or {}
        return f"grade:{a.get('id')}:{sub.get('score')}"

    def _fresh_grades(self, assigns, names, seen):
        """把已出分且未见过的作业渲染成待推送项。"""
        out = []
        for a in assigns:
            sub = a.get("submission") or {}
            if sub.get("score") is None or not sub.get("graded_at"):
                continue
            key = self._grade_key(a)
            if key in seen:
                continue
            pp = a.get("points_possible")
            score = sub.get("score")
            lines = [
                f"📊 成绩发布 · {self._cname(names, a)}",
                f"作业:{a.get('name') or '未命名'}",
                f"得分:{score}" + (f" / {pp}" if pp is not None else ""),
            ]
            if a.get("html_url"):
                lines.append(f"链接:{a['html_url']}")
            out.append((key, "\n".join(lines)))
        return out

    def _seed_grades(self, assigns, seen):
        for a in assigns:
            sub = a.get("submission") or {}
            if sub.get("score") is not None and sub.get("graded_at"):
                seen.add(self._grade_key(a))

    # ---------- 首轮汇总 ----------
    def _render_digest(self, me, names, anns, assigns) -> str:
        """首次运行发一份待办汇总,而不是把几十条历史内容逐条刷屏。"""
        now = datetime.now().astimezone()
        horizon = now + timedelta(days=7)

        todo = []
        for a in assigns:
            sub = a.get("submission") or {}
            if sub.get("workflow_state") in ("submitted", "graded"):
                continue
            due = _parse_iso(a.get("due_at"))
            if due and now <= due <= horizon:
                todo.append((due, a))
        todo.sort(key=lambda x: x[0])

        recent = sorted(
            [a for a in anns if _parse_iso(a.get("posted_at"))],
            key=lambda a: _parse_iso(a.get("posted_at")), reverse=True)[:10]

        who = (me or {}).get("name") or (me or {}).get("short_name") or "同学"
        lines = [f"📚 Canvas 待办汇总（首次运行）", f"{who},共 {len(names)} 门在学课程。"]

        if todo:
            lines.append("")
            lines.append(f"⏳ 未来 7 天要交（{len(todo)} 项）")
            for due, a in todo[:15]:
                lines.append(f"· {names.get(a.get('_course'), '?')} · "
                             f"{a.get('name') or '未命名'}")
                lines.append(f"  截止 {_fmt_dt(due)}（{_remain(due)}）")
            if len(todo) > 15:
                lines.append(f"…另有 {len(todo) - 15} 项")
        else:
            lines.append("")
            lines.append("✅ 未来 7 天没有要交的作业")

        if recent:
            lines.append("")
            lines.append(f"📢 最近公告（{len(recent)} 条）")
            for a in recent:
                lines.append(f"· {names.get(a.get('_course'), '?')}:"
                             f"{a.get('title') or '无标题'}"
                             f"（{_fmt_dt(_parse_iso(a.get('posted_at')))}）")

        lines.append("")
        lines.append("今后只推送新变化。发 #Canvas状态 查看运行情况。")
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
        if not text.startswith("#Canvas") and not text.startswith("#canvas"):
            return False
        if not self.router.is_owner(ev.user_id):
            return True  # 仅主人可用,吞掉命令
        if text.startswith("#Canvas检查") or text.startswith("#canvas检查"):
            await self.router.reply(ev, "⏳ 正在检查 Canvas…")
            result = await self.check_once()
            await self.router.reply(ev, f"Canvas 检查:{result}")
            return True
        # #Canvas状态
        cfg = _load_json(CONFIG_PATH, {})
        st = _load_json(STATE_PATH, {})
        cred = self._credentials(cfg)
        if cred is None:
            cred_line = "❌ 未配置"
        elif cred[0] == "token":
            cred_line = "✅ token"
        else:
            cred_line = "⚠️ cookie（会过期,建议改用 token）"
        watch = dict(DEFAULT_WATCH)
        watch.update(cfg.get("watch") or {})
        on = "/".join(k for k, v in watch.items() if v) or "无"
        lines = [
            f"凭证:{cred_line}",
            f"主机:{(cfg.get('base_url') or DEFAULT_BASE).rstrip('/')}",
            f"监测:{on}",
            f"上次成功:{_fmt_dt(datetime.fromtimestamp(st['last_ok']).astimezone()) if st.get('last_ok') else '从未'}",
            f"已记录:{len(st.get('seen', []))} 条",
            f"轮询间隔:{POLL_INTERVAL // 60} 分钟",
        ]
        if self._last_rate is not None:
            lines.append(f"限流余量:{self._last_rate}")
        if st.get("last_error"):
            lines.append(f"最近错误:{st['last_error']}")
        await self.router.reply(ev, "#Canvas状态\n" + "\n".join(lines))
        return True
