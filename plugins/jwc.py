"""SJTU 教务处（jwc.sjtu.edu.cn）通知监控：轮询「新闻通告」各栏目，新通知私聊推主人。

站点是 VSB（Visual SiteBuilder）网站群 CMS，栏目列表页是**静态 HTML**，
不需要登录凭证、没有 JS 挑战，普通 GET 就能抓。栏目页形如
https://jwc.sjtu.edu.cn/xwtg/tztg.htm ，每页 10 条，单条的日期在
<div class="sj"><h2>DD</h2><p>YYYY.MM</p></div>，标题在 .wz > a > h2。

流程：每 poll_interval 秒串行抓一遍启用栏目 → 与 .jwc_state.json 去重
→ 首次运行只发一份"最新通知"当基线（不把历史几百条全推一遍）→ 之后只推增量。

主人命令：#教务状态 查看运行情况；#教务检查 立即检查一次；#教务栏目 列出栏目开关。

设计要点（改代码前先读）：
- **去重键用文章 ID**：从 info/{栏目}/{id}.htm 或 content.jsp?...wbnewsid={id}
  里取 {id}，这样同一条通知同时出现在"通知通告"聚合页和它所属栏目页时只推一次。
  新闻中心是微信公众号外链、没有文章 ID，退回用 URL 的 sha1 摘要当键。
- **停机多日要能补齐**：正常只抓第 1 页（10 条）。若第 1 页整页都是没见过的条目，
  说明距上次轮询新增超过 10 条，则顺分页的"下页"最多再补抓 MAX_PAGES-1 页，
  直到某一页出现已见过的条目为止。
- 发送成功才写 seen，WS 断开时保留待发项下轮重试——通知错过代价高，宁重勿漏。
- 单轮新增超过 max_push_per_round 条时合并成一条汇总，不逐条刷屏。
- 整个栏目抓不到（超时/500）不算错误结果：找不到条目就等于没有新增，
  绝不会因为抓取失败把已有通知当成"新的"重推一遍。
"""
import asyncio
import hashlib
import html
import json
import logging
import os
import re
import time
from urllib.parse import urljoin

import aiohttp

from bot import events
from bot.router import Plugin

log = logging.getLogger("jwc")

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PLUGIN_DIR, "jwc.json")
STATE_PATH = os.path.join(PLUGIN_DIR, ".jwc_state.json")

BASE = "https://jwc.sjtu.edu.cn"
DEFAULT_POLL = 900             # 轮询间隔（秒）= 15 分钟
MIN_POLL = 300                 # 间隔下限，别给学校服务器添乱
MAX_PAGES = 3                  # 单栏目单轮最多抓几页（补推用）
PAGE_SIZE = 10                 # 栏目页每页条数（VSB 固定 10）
SEEN_CAP = 4000                # 去重列表上限
SLEEP_BETWEEN_MSGS = 2         # 条与条之间的间隔，防风控
MAX_PUSH_PER_ROUND = 10        # 超过则合并成一条汇总
FIRST_RUN_SHOW = 12            # 首次汇总里列几条最新通知
BULK_MAX_LINES = 40            # 汇总消息里最多列几条
SUMMARY_LEN = 80               # 推送里摘要截断长度
TOTAL_FAIL_WARN_COOLDOWN = 12 * 3600   # 全部栏目都抓不到时的提醒间隔
CFG_WARN_COOLDOWN = 6 * 3600           # 配置文件写坏时的提醒间隔

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 兜底栏目清单（jwc.json 读不出来时用），顺序即推送里"栏目"署名的优先级
DEFAULT_SOURCES = [
    {"name": "通知通告", "url": f"{BASE}/xwtg/tztg.htm", "enabled": True},
    {"name": "教学运行", "url": f"{BASE}/xwtg/jxyx.htm", "enabled": True},
    {"name": "注册学务", "url": f"{BASE}/xwtg/zcxw.htm", "enabled": True},
]

RE_LI = re.compile(r"<li[^>]*>(.*?)</li>", re.S)
RE_A = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
RE_TAGS = re.compile(r"<[^>]+>")
RE_P = re.compile(r"<p>(.*?)</p>", re.S)
# 日期：<div class="sj"><h2>24</h2><p>2026.09</p></div>
RE_SJ = re.compile(r'<div class="sj">\s*<h2>\s*(\d{1,2})\s*</h2>\s*<p>\s*(\d{4})\.(\d{1,2})\s*</p>', re.S)
# 另一种版式（列表页不用，保险起见也认）：<span>2026-09-21</span>
RE_SPAN_DATE = re.compile(r"<span>\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*</span>")
# 文章 ID：info/1222/128931.htm 里的 128931，或 content.jsp?...wbnewsid=128931
# 要求 5 位以上数字，免得把分页链接（tztg/234.htm）误当成文章 ID
RE_NID = re.compile(r"/(\d{5,})\.htm|wbnewsid=(\d+)")
RE_NEXT = re.compile(r'<span class="p_next[^"]*">\s*<a href="([^"]+)"')
RE_CHARSET = re.compile(r"charset=([\w-]+)", re.I)
RE_BAD_EXT = re.compile(r"\.(css|js|png|jpe?g|gif|svg|ico|mp4|mp3|zip|rar|docx?|xlsx?|pptx?|pdf)$", re.I)


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _load_config():
    """读 jwc.json，返回 (配置, 错误描述)。

    文件不存在 → 用内置默认栏目，方便第一次跑；
    文件存在但不是合法 JSON → **拒绝运行并告警，绝不静默退回默认值**
    （2026-09-27 踩过：手抖多写一个 `],` → 解析失败 → 插件静默只监控默认的 3 个栏目，
      用户改的 7 个栏目全没了却毫无提示。宁可停下来吵，也不要悄悄少监控。）
    """
    if not os.path.exists(CONFIG_PATH):
        return {}, None
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        return None, f"{e.__class__.__name__}: {e}"
    if not isinstance(cfg, dict):
        return None, "配置顶层必须是 JSON 对象"
    return cfg, None


def _save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except OSError:
        pass


def _clean(s) -> str:
    """去标签、还原 HTML 实体、把空白压成单空格"""
    s = RE_TAGS.sub("", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _abs_url(href: str, page_url: str) -> str:
    """把列表页里的相对链接补成绝对地址。

    站点里两种写法都有：栏目页写 `../info/1222/x.htm`（相对 /xwtg/），
    首页那种汇总块写 `info/1258/x.htm`（相对站点根）。分开处理，别一律 urljoin。
    """
    if not href:
        return ""
    if not page_url:      # 兜底：拿不到所属页面就按站点根解析
        page_url = BASE + "/"
    if href.startswith("http"):
        return href
    if href.startswith("info/") or href.startswith("content.jsp") or href.startswith("/"):
        return urljoin(BASE + "/", href)
    return urljoin(page_url, href)


def _is_news_url(u: str) -> bool:
    """像不像一条内容的链接（排除 css/js/图片/附件和 javascript 伪链）"""
    if not u or u.startswith(("javascript:", "#", "mailto:")):
        return False
    if "wbnewsid=" in u or re.search(r"/info/\d+/\d+\.htm", u):
        return True
    if u.startswith("http") and "/system/resource/" not in u and not RE_BAD_EXT.search(u):
        return True   # 新闻中心的微信公众号外链
    return False


def _news_key(url: str) -> str:
    """去重键：优先文章 ID，没有就用 URL 摘要"""
    m = RE_NID.search(url)
    if m:
        return "n" + (m.group(1) or m.group(2))
    return "u" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def _parse_list(page: str, page_url: str) -> list:
    """解析一个栏目页，返回按页面顺序（新 → 旧）的条目列表。"""
    start = page.find("Newslist")
    if start < 0:
        start = 0
    # 列表结束在分页条（pb_sys_common）或点击统计脚本处，切掉后面的页脚导航
    end = page.find("pb_sys_common", start)
    if end < 0:
        end = page.find("_showDynClickBatch", start)
    region = page[start:end if end > 0 else len(page)]

    out = []
    for m in RE_LI.finditer(region):
        block = m.group(1)
        dm = RE_SJ.search(block)
        if dm:
            date = "%04d-%02d-%02d" % (int(dm.group(2)), int(dm.group(3)), int(dm.group(1)))
        else:
            ds = RE_SPAN_DATE.search(block)
            if not ds:
                continue      # 没有日期的 <li> 是导航/页脚，不是条目
            date = "%04d-%02d-%02d" % (int(ds.group(1)), int(ds.group(2)), int(ds.group(3)))

        am = RE_A.search(block)
        if not am:
            continue
        url = _abs_url(am.group(1).strip(), page_url)
        title = _clean(am.group(2))
        if not title or not _is_news_url(url):
            continue

        # 摘要 = 去掉日期块之后剩下的那个 <p>
        summary = ""
        pm = RE_P.search(RE_SJ.sub("", block))
        if pm:
            summary = _clean(pm.group(1))

        out.append({"key": _news_key(url), "date": date, "title": title,
                    "url": url, "summary": summary})
    return out


def _next_page(page: str, page_url: str):
    """取分页条里"下页"的地址；已是最后一页时返回 None。"""
    m = RE_NEXT.search(page)
    return _abs_url(m.group(1).strip(), page_url) if m else None


class JwcPlugin(Plugin):
    COMMANDS = (
        ("#教务状态", "", True, "查看教务处通知监控状态"),
        ("#教务检查", "", True, "立即检查一次教务处通知"),
        ("#教务栏目", "", True, "查看监控栏目及开关"),
    )

    def setup(self, router):
        super().setup(router)
        self._checking = asyncio.Lock()
        self._fail_warned_at = 0.0
        self._cfg_warned_at = 0.0
        self._task = asyncio.create_task(self._poll_loop())

    # ---------- 主循环 ----------
    async def _poll_loop(self):
        await asyncio.sleep(40)   # 错开其它插件，等 WS 连接稳定后再做首次检查
        while True:
            interval = DEFAULT_POLL
            try:
                await self.check_once()
                interval = self._interval()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("轮询异常: %s", e)
            await asyncio.sleep(interval)

    @staticmethod
    def _interval() -> int:
        cfg = _load_json(CONFIG_PATH, {})
        try:
            return max(MIN_POLL, int(cfg.get("poll_interval") or DEFAULT_POLL))
        except (TypeError, ValueError):
            return DEFAULT_POLL

    # ---------- 一次完整检查 ----------
    async def check_once(self) -> str:
        """执行一次检查，返回结果描述（也供 #教务检查 命令使用）。"""
        if self._checking.locked():
            return "检查正在进行中，请稍候"
        async with self._checking:
            return await self._check_inner()

    async def _check_inner(self) -> str:
        cfg, cfg_err = _load_config()
        if cfg_err is not None:
            log.error("jwc.json 解析失败，本轮不检查: %s", cfg_err)
            await self._warn_bad_config(cfg_err)
            return f"jwc.json 格式错误（{cfg_err}），本轮不检查；修好后自动恢复"
        if cfg.get("enabled") is False:
            return "已在 jwc.json 中关闭（enabled=false）"
        sources = self._sources(cfg)
        if not sources:
            return "jwc.json 里没有启用任何栏目"

        try:
            limit = max(1, int(cfg.get("max_push_per_round") or MAX_PUSH_PER_ROUND))
        except (TypeError, ValueError):
            limit = MAX_PUSH_PER_ROUND

        st = _load_json(STATE_PATH, {})
        seen = set(st.get("seen", []))
        errors = {}

        found = []   # [(date, 栏目名, 条目)]，之后按时间正序排
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as s:
            # 串行抓取：栏目只有 10 个，且同一时刻只连一个，对学校服务器最友好
            for src in sources:
                items, err = await self._fetch_source(s, src, seen, bool(st.get("seeded")))
                if err:
                    errors[src["name"]] = err
                    log.warning("教务处栏目「%s」抓取失败: %s", src["name"], err)
                    continue
                # 页面是新→旧，翻过来变旧→新，好让后面的稳定排序按时间正序
                for it in reversed(items):
                    found.append((it["date"], src["name"], it))

        # 只保留"最近一轮"的失败信息：成功一轮就清空，免得状态里一直挂着旧错误。
        # 具体错误内容看 source_errors，这里只留个时间戳（整串拼起来太长，
        # 真抖出来就是四百多字的噪音）。
        st["source_errors"] = errors
        if errors:
            st["last_error_at"] = time.strftime("%m-%d %H:%M")
        else:
            st.pop("last_error_at", None)

        if len(errors) == len(sources):
            # 全军覆没：什么都别记，也就不会把旧通知当新的重推
            _save_json(STATE_PATH, self._cap_seen(st, seen))
            await self._warn_total_failure(errors)
            return "所有栏目都抓取失败（%s），本轮跳过" % list(errors.values())[0]

        found.sort(key=lambda x: x[0])   # 稳定排序，同一天的保持栏目内先后

        if not st.get("seeded"):
            return await self._first_run(found, sources, st, seen, errors)

        fresh = []
        known = set()
        for date, name, it in found:
            if it["key"] in seen or it["key"] in known:
                continue      # 已推过，或本轮里被别的栏目重复列出
            known.add(it["key"])
            fresh.append((it["key"], name, it))

        st["last_ok"] = time.time()
        scan = f"共扫描 {len(found)} 条"
        if errors:
            scan += "（%s 抓取失败）" % ",".join(errors)

        if not fresh:
            _save_json(STATE_PATH, self._cap_seen(st, seen))
            return f"没有新通知，{scan}"

        sent = await self._push_fresh(fresh, seen, limit)
        _save_json(STATE_PATH, self._cap_seen(st, seen))
        if sent < len(fresh):
            return f"本轮新增 {len(fresh)} 条，已推送 {sent} 条（余下轮重试），{scan}"
        return f"本轮新增 {len(fresh)} 条，已全部推送，{scan}"

    # ---------- 抓取一个栏目 ----------
    async def _fetch_source(self, session, src, seen, deep):
        """返回 (条目列表, 错误描述)。条目按页面顺序（新 → 旧）。"""
        page, err = await self._fetch(session, src["url"])
        if err:
            return [], err
        url = src["url"]
        items = _parse_list(page, url)

        # 若第 1 页整页都是没见过的（停机多日），顺"下页"补抓，直到某页出现旧条目。
        # 已经翻到的那一页只要还有旧条目就停下——那之后的内容必然是以前推过的。
        pages = 1
        while deep and pages < MAX_PAGES and len(items) >= PAGE_SIZE * pages \
                and all(it["key"] not in seen for it in items[-PAGE_SIZE:]):
            nxt = _next_page(page, url)
            if not nxt:
                break
            page, err = await self._fetch(session, nxt)   # 注意返回的是 (html, err)
            if err or not page:
                break     # 补抓失败不算整栏目失败，第 1 页的增量照样推
            url = nxt
            more = _parse_list(page, url)
            if not more:
                break
            items.extend(more)
            pages += 1
        if pages > 1:
            log.info("栏目「%s」首页全是新条目，已补抓到第 %d 页（共 %d 条）",
                     src["name"], pages, len(items))
        return items, None

    async def _fetch(self, session, url):
        """抓一个页面，返回 (html 文本, 错误描述)。"""
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None, f"http{resp.status}"
                raw = await resp.read()
                enc = "utf-8"
                m = RE_CHARSET.search(resp.headers.get("Content-Type", ""))
                if m:
                    enc = m.group(1)
                else:
                    # 响应头没写编码时，从 <meta charset=...> 嗅探（老页面可能是 gb2312）
                    m = RE_CHARSET.search(raw[:2048].decode("ascii", "ignore"))
                    if m:
                        enc = m.group(1)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return None, f"network({e.__class__.__name__})"
        return raw.decode(enc, "replace"), None

    # ---------- 首次运行 / 增量推送 ----------
    async def _first_run(self, found, sources, st, seen, errors) -> str:
        """首次运行只发一份最新通知当基线，不把历史几百条全推一遍。"""
        text = self._render_first(found, sources, errors)
        if not await self._push(text):
            return "首次汇总发送失败（WS 未连接？），下轮重试"
        for _, _, it in found:
            seen.add(it["key"])
        st["seeded"] = True
        st["last_ok"] = time.time()
        _save_json(STATE_PATH, self._cap_seen(st, seen))
        log.info("首次运行完成，已记录基线 %d 条", len(found))
        return f"首次运行完成，已发汇总。基线共记入 {len(found)} 条，今后只推新通知"

    async def _push_fresh(self, fresh, seen, limit) -> int:
        """推送未见过的条目。超过 limit 条则合并成一条汇总，避免刷屏。"""
        if len(fresh) > limit:
            if not await self._push(self._render_bulk(fresh)):
                return 0
            for key, _, _ in fresh:
                seen.add(key)
            log.info("本轮新增 %d 条，已合并成一条汇总推送", len(fresh))
            return len(fresh)

        sent = 0
        for key, name, it in fresh:
            if not await self._push(self._render_one(name, it)):
                log.warning("第 %d 条推送失败，余下轮重试", sent + 1)
                break
            seen.add(key)
            sent += 1
            await asyncio.sleep(SLEEP_BETWEEN_MSGS)
        return sent

    # ---------- 推送 ----------
    async def _push(self, text) -> bool:
        """私聊主人。带僵尸连接防护：send_json 对半开连接可能假成功。"""
        owner = self.cfg.OWNER_QQ
        if not owner or not text:
            return False
        ok = await self.client.send_action(
            "send_private_msg", {"user_id": int(owner), "message": text})
        if not ok:
            return False
        # 2026-08-25 教训：发送后短暂让出事件循环，若连接已被心跳判死则本轮不算数
        await asyncio.sleep(1)
        return self.client.ws is not None

    async def _warn_bad_config(self, err):
        """配置写坏时告警（带冷却，免得每 15 分钟刷一条）。"""
        now = time.time()
        if now - getattr(self, "_cfg_warned_at", 0.0) < CFG_WARN_COOLDOWN:
            return
        self._cfg_warned_at = now
        await self._push(
            "⚠️ 教务处监控的配置有误，已暂停检查（不会再有任何推送）。\n"
            "文件：plugins/jwc.json\n"
            f"错误：{err}\n"
            "常见原因：删条目时多留或漏了逗号、方括号不配对。改好后下轮轮询自动恢复，"
            "发 #教务状态 可随时查看。")

    async def _warn_total_failure(self, errors):
        """所有栏目都抓不到时才提醒（个别栏目失败只记状态，不打扰）。"""
        now = time.time()
        if now - self._fail_warned_at < TOTAL_FAIL_WARN_COOLDOWN:
            return
        self._fail_warned_at = now
        kinds = set(errors.values())
        if len(kinds) == 1 and next(iter(kinds)).startswith("network"):
            # 全是网络类错误 → 大概率是本机断网/DNS 抽风，说清楚免得用户去查教务处
            kind = next(iter(kinds))
            await self._push(
                "⚠️ 教务处监控暂时停摆：本机网络/DNS 出问题了。\n"
                f"错误：{kind}\n"
                "同一时刻 Canvas、好课、B站、第二课堂这些联网插件也会一起失败，"
                "所以是这台电脑的网络问题，不是教务处网站的事。\n"
                "联网恢复后会自动继续，并把停摆期间的新通知补推给你"
                "（一次最多补 30 条，更多就合并成一条汇总）。\n"
                "12 小时内不再重复提醒；发 #教务状态 可随时看情况。")
            return
        await self._push(
            "⚠️ 教务处监控异常：所有栏目都抓取失败。\n"
            + "\n".join(f"· {k}：{v}" for k, v in errors.items())
            + "\n可能是教务处网站在维护。已自动重试，"
              "发 #教务状态 可查看最近错误。")

    # ---------- 渲染 ----------
    @staticmethod
    def _summary_line(it) -> str:
        s = it.get("summary") or ""
        if len(s) > SUMMARY_LEN:
            s = s[:SUMMARY_LEN] + "…"
        return s

    def _render_one(self, name, it) -> str:
        lines = [
            f"📢 教务处新通知 · {name}",
            it["title"],
            f"日期：{it['date']}",
        ]
        s = self._summary_line(it)
        # 摘要就是正文开头，常常把标题又重复一遍，那就别占了
        if s and not s.startswith(it["title"][:10]):
            lines.append(f"摘要：{s}")
        lines.append(f"链接：{it['url']}")
        return "\n".join(lines)

    def _render_bulk(self, fresh) -> str:
        newest = list(reversed(fresh))     # fresh 是旧→新，这里要新→旧
        lines = [f"📢 教务处有一批新通知（{len(fresh)} 条）"]
        for _, name, it in newest[:BULK_MAX_LINES]:
            lines.append(f"\n· [{name}] {it['date'][5:]} {it['title']}\n  {it['url']}")
        if len(newest) > BULK_MAX_LINES:
            lines.append(f"\n…另有 {len(newest) - BULK_MAX_LINES} 条，"
                         f"完整列表见 https://jwc.sjtu.edu.cn/xwtg/tztg.htm")
        return "\n".join(lines)

    def _render_first(self, found, sources, errors) -> str:
        newest = list(reversed(found))[:FIRST_RUN_SHOW]
        lines = [
            "📢 教务处通知监控已上线",
            f"正在监控 {len(sources)} 个栏目，每 {self._interval() // 60} 分钟检查一次，"
            f"有新通知会私聊推给你。",
            f"（共记入基线 {len(found)} 条历史通知，今后只推新内容）",
        ]
        if errors:
            lines.append("⚠️ 本次抓取失败：" + "、".join(errors))
        if newest:
            lines.append("")
            lines.append(f"📌 目前最新的 {len(newest)} 条：")
            for _, name, it in newest:
                lines.append(f"\n· [{name}] {it['date'][5:]} {it['title']}\n  {it['url']}")
        lines.append("")
        lines.append("发 #教务状态 查看运行情况，#教务检查 立即查一次，#教务栏目 看栏目开关。")
        return "\n".join(lines)

    # ---------- 状态维护 ----------
    @staticmethod
    def _cap_seen(st, seen):
        st = dict(st)
        st["seen"] = list(seen)[-SEEN_CAP:]
        return st

    @staticmethod
    def _sources(cfg):
        raw = cfg.get("sources")
        if not isinstance(raw, list) or not raw:
            raw = DEFAULT_SOURCES
        out = []
        for s in raw:
            if not isinstance(s, dict) or s.get("enabled") is False:
                continue
            url = (s.get("url") or "").strip()
            if not url:
                continue
            out.append({"name": (s.get("name") or url).strip(), "url": url})
        return out

    @staticmethod
    def _all_sources(cfg):
        raw = cfg.get("sources")
        if not isinstance(raw, list) or not raw:
            raw = DEFAULT_SOURCES
        return [s for s in raw if isinstance(s, dict) and (s.get("url") or "").strip()]

    @staticmethod
    def _ago(ts) -> str:
        """距现在多久，给人看的粗略粒度（配合绝对时间一起显示）。"""
        if not ts:
            return ""
        try:
            mins = (time.time() - float(ts)) / 60
        except (TypeError, ValueError):
            return ""
        if mins < 2:
            return "刚刚"
        if mins < 60:
            return f"{int(mins)} 分钟前"
        if mins < 48 * 60:
            return f"{int(mins // 60)} 小时前"
        return f"{int(mins // 1440)} 天前"

    @staticmethod
    def _error_lines(st, sources, errs) -> list:
        """把抓取错误整理成人话。

        全部挂在同一原因上时合并成一行：7 个栏目各报一行 ⚠️ 会让人以为插件坏了
        （2026-09-27 用户就被这排 ⚠️ 误导过），而且看不出到底是不是本机网络的事。
        """
        if not errs:
            # 正常流程里成功一轮就把错误清空了，这里只是老状态文件的兜底
            return ([f"最近错误：{st['last_error_at']}（已恢复）"]
                    if st.get("last_error_at") else [])
        kinds = set(errs.values())
        if len(kinds) == 1 and len(errs) >= max(1, len(sources)):
            kind = next(iter(kinds))
            if kind.startswith("network"):
                return [
                    f"❌ 全部 {len(errs)} 个栏目都抓不到（{kind}）",
                    "   本机网络/DNS 出问题了：同一时刻 Canvas、好课、B站、第二课堂"
                    "这些联网插件也会一起失败，跟教务处网站无关。联网恢复后自动继续抓。",
                ]
            return [f"❌ 全部 {len(errs)} 个栏目都抓不到（{kind}）："
                    "若持续如此，多半是教务处在维护，会自动重试。"]
        if len(errs) > 4:
            return [f"⚠️ {len(errs)} 个栏目抓取失败：{'、'.join(errs)}"]
        return [f"⚠️ {n}：{e}" for n, e in errs.items()]

    async def _probe(self, url) -> str:
        """现在能不能抓到教务处？给 #教务状态 用来区分"刚才网络抽风"和"现在还坏着"。

        超时压到 6 秒，任何异常都吞掉——状态查询本身绝不能因为探测失败而失败。
        """
        timeout = aiohttp.ClientTimeout(total=6)
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as s:
                page, err = await self._fetch(s, url)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return f"探测异常（{e.__class__.__name__}）"
        if err:
            return f"现在仍然抓不到（{err}）"
        return "现在已恢复正常，下一轮（≤%d 分钟）会自动把新通知补上" % (self._interval() // 60)

    # ---------- 主人命令 ----------
    async def on_message(self, ev: events.MessageEvent) -> bool:
        text = events.text_of(ev.segments).strip()
        if not text.startswith("#教务"):
            return False
        if not self.router.is_owner(ev.user_id):
            return True      # 仅主人可用，别人的命令直接吞掉

        if text.startswith("#教务检查"):
            await self.router.reply(ev, "⏳ 正在检查教务处网站…")
            await self.router.reply(ev, f"教务处检查：{await self.check_once()}")
            return True

        if text.startswith("#教务栏目"):
            cfg, cfg_err = _load_config()
            if cfg_err is not None:
                await self.router.reply(ev, f"#教务栏目\n❌ jwc.json 格式错误：{cfg_err}\n"
                                            "修好之前监控是停的（其余栏目照旧，不受影响）。")
                return True
            lines = ["#教务栏目"]
            for s in self._all_sources(cfg):
                on = s.get("enabled") is not False
                lines.append(f"{'✅' if on else '⬜'} {s.get('name') or s.get('url')}")
            lines.append("")
            lines.append("想增减就编辑 plugins/jwc.json 的 sources，把 enabled 改成 "
                         "false，下一轮轮询（≤15 分钟）自动生效，不用重启机器人。")
            await self.router.reply(ev, "\n".join(lines))
            return True

        # #教务状态
        cfg, cfg_err = _load_config()
        st = _load_json(STATE_PATH, {})
        cfg = cfg or {}
        lines = []
        if cfg_err is not None:
            lines.append(f"❌ jwc.json 格式错误：{cfg_err}")
            lines.append("监控已暂停，修好后下一轮轮询自动恢复。")
        on = self._sources(cfg)
        ok_at = self._fmt_ts(st.get("last_ok"))
        ago = self._ago(st.get("last_ok"))
        lines += [
            f"监控栏目：{len(on)}/{len(self._all_sources(cfg))} 个已启用",
            f"轮询间隔：{self._interval() // 60} 分钟",
            f"上次成功：{ok_at}" + (f"（{ago}）" if ago else ""),
            f"已记录：{len(st.get('seen', []))} 条",
            f"状态：{'已建立基线，只推增量' if st.get('seeded') else '尚未完成首次检查'}",
        ]
        errs = st.get("source_errors") or {}
        lines += self._error_lines(st, on, errs)
        if errs and on:
            # 有错误时实测一下现在通不通，否则用户没法判断是"刚才的事"还是"现在还坏"
            lines.append(f"实测：{await self._probe(on[0]['url'])}")
        lines.append("入口：https://jwc.sjtu.edu.cn/xwtg/tztg.htm")
        await self.router.reply(ev, "#教务状态\n" + "\n".join(lines))
        return True

    @staticmethod
    def _fmt_ts(ts) -> str:
        if not ts:
            return "从未"
        try:
            return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
        except (TypeError, ValueError):
            return "从未"
