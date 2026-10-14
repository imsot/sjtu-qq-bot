"""B站UP主更新提醒:每天固定时间轮询一次关注动态流,把新增视频打包成一条消息私聊主人。

依赖：plugins/bilibili.json 里的 sessdata(浏览器登录凭证)。
流程：每天 PUSH_HOUR 点拉一次关注动态流(必要时翻页) → 筛出视频类动态(MAJOR_TYPE_ARCHIVE)
→ 首次运行只记录基线不推送(避免刷屏) → 之后把新增视频汇总成一条消息推送:
    序号 / UP主名称 / 视频标题。
推送失败时视频保留在待推送队列,下次一并补发。
主人命令：#B站状态 查看运行情况；#B站检查 立即轮询并推送一次。
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

log = logging.getLogger("bilibili")

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PLUGIN_DIR, "bilibili.json")
STATE_PATH = os.path.join(PLUGIN_DIR, ".bilibili_state.json")

FEED_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all"
PUSH_HOUR = 8              # 每天几点推送(24小时制,本地时间)
PUSH_MINUTE = 0
MAX_PAGES = 3              # 单次最多翻几页动态(隔天轮询,翻页防止漏推)
MAX_PUSH_ITEMS = 40        # 单条消息最多列几条视频,多出的顺延到下一次
COOKIE_WARN_COOLDOWN = 6 * 3600  # 凭证失效提醒间隔(秒)
SEEN_CAP = 2000            # 去重列表上限
PENDING_CAP = 200          # 待推送队列上限


HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json",
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


def _fmt_time(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "未知时间"


class BilibiliPlugin(Plugin):
    COMMANDS = (
        ("#B站状态", "", True, "查看 B站更新监控状态"),
        ("#B站检查", "", True, "立即检查一次 UP 主更新"),
    )

    def setup(self, router):
        super().setup(router)
        self._cookie_warned_at = 0.0
        self._checking = asyncio.Lock()
        self._task = asyncio.create_task(self._daily_loop())

    # ---------- 主循环 ----------
    def _seconds_until_push(self) -> float:
        """距下一个 PUSH_HOUR:PUSH_MINUTE 的秒数。"""
        now = datetime.now()
        target = now.replace(hour=PUSH_HOUR, minute=PUSH_MINUTE, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    async def _daily_loop(self):
        await asyncio.sleep(15)  # 等 WS 连接稳定后再做首次检查
        # 首次运行:先建立基线,避免把关注列表里的历史视频当成新增刷屏
        if not _load_json(STATE_PATH, {}).get("seeded"):
            try:
                log.info("首次运行:%s", await self.check_once(push=False))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("初始化异常: %s", e)
        while True:
            delay = self._seconds_until_push()
            nxt = datetime.now() + timedelta(seconds=delay)
            log.info("下次B站推送:%s", nxt.strftime("%m-%d %H:%M"))
            await asyncio.sleep(delay)
            try:
                log.info("B站推送结果:%s", await self.check_once())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("推送异常: %s", e)

    async def check_once(self, push: bool = True) -> str:
        """执行一次检查。push=True 时把新增视频汇总成一条消息推送。"""
        if self._checking.locked():
            return "检查正在进行中,请稍候"
        async with self._checking:
            cfg = _load_json(CONFIG_PATH, {})
            sessdata = (cfg.get("sessdata") or "").strip()
            if not sessdata:
                return "未配置 sessdata(bilibili.json),功能停用中"

            st = _load_json(STATE_PATH, {})
            seen_list = list(st.get("seen", []))
            seen = set(seen_list)
            pending = list(st.get("pending", []))

            videos, err = await self._fetch_videos(sessdata, seen)
            if err is not None:
                code, msg = err
                st["last_error"] = f"{time.strftime('%m-%d %H:%M')} code={code} {msg}"
                _save_json(STATE_PATH, self._save_state(st, seen_list, pending))
                if code in (-101, -352):  # 未登录/风控:多半是凭证过期
                    await self._warn_cookie(code, msg)
                return f"拉取失败 code={code}: {msg}"

            st["last_ok"] = time.time()
            st.pop("last_error", None)

            if not st.get("seeded"):
                # 首次运行:把当前已有的视频记为已见,不推送历史内容
                for v in videos:
                    if v["id"] not in seen:
                        seen.add(v["id"])
                        seen_list.append(v["id"])
                st["seeded"] = True
                _save_json(STATE_PATH, self._save_state(st, seen_list, pending))
                log.info("首次运行,基线记录 %d 条动态", len(videos))
                return (f"首次运行完成,已记录基线 {len(videos)} 条,"
                        f"今后每天 {PUSH_HOUR}:{PUSH_MINUTE:02d} 推送新增视频")

            known = seen | {v["id"] for v in pending}
            fresh = [v for v in videos if v["id"] not in known]
            pending.extend(fresh)
            # 按发布时间升序,先推早的
            pending.sort(key=lambda v: v.get("pub_ts") or 0)
            if len(pending) > PENDING_CAP:
                pending = pending[-PENDING_CAP:]

            if not pending:
                _save_json(STATE_PATH, self._save_state(st, seen_list, pending))
                return "无新增视频"

            if not push:
                _save_json(STATE_PATH, self._save_state(st, seen_list, pending))
                return f"新增 {len(fresh)} 条,待推送共 {len(pending)} 条"

            owner = self.cfg.OWNER_QQ
            if not owner:
                _save_json(STATE_PATH, self._save_state(st, seen_list, pending))
                return f"未配置主人QQ,{len(pending)} 条待推送"

            # 一次一条消息:超出 MAX_PUSH_ITEMS 的部分留在队列里,下次补发
            batch, rest = pending[:MAX_PUSH_ITEMS], pending[MAX_PUSH_ITEMS:]
            ok = await self.client.send_action(
                "send_private_msg",
                {"user_id": int(owner), "message": self._render_batch(batch)})
            if not ok:
                # 推送失败,全部保留(含本批),下次重试
                _save_json(STATE_PATH, self._save_state(st, seen_list, pending))
                return f"新增 {len(fresh)} 条,推送失败,已保留待下次重试"

            for v in batch:
                if v["id"] not in seen:
                    seen.add(v["id"])
                    seen_list.append(v["id"])
            _save_json(STATE_PATH, self._save_state(st, seen_list, rest))
            return (f"已推送 {len(batch)} 条(本轮新增 {len(fresh)} 条)"
                    + (f",剩余 {len(rest)} 条顺延" if rest else ""))

    # ---------- 拉取与解析 ----------
    async def _fetch_videos(self, sessdata, seen):
        """最多翻 MAX_PAGES 页,返回 (videos, err)。videos=[{id,author,title,bvid,pub_ts}],err=(code,msg)|None"""
        headers = dict(HEADERS)
        headers["Cookie"] = f"SESSDATA={sessdata}"
        videos, ids = [], set()
        offset = ""
        timeout = aiohttp.ClientTimeout(total=20)
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
                for page in range(MAX_PAGES):
                    params = {"timezone_bias": 0}
                    if offset:
                        params["offset"] = offset
                    async with s.get(FEED_URL, params=params) as resp:
                        obj = await resp.json(content_type=None)
                    if not isinstance(obj, dict):
                        if page == 0:
                            return [], (0, "响应格式异常")
                        break
                    code = obj.get("code")
                    if code != 0:
                        if page == 0:
                            return [], (code, (obj.get("message") or "")[:80])
                        break  # 后续页失败就用已拿到的部分
                    data = obj.get("data") or {}
                    items = data.get("items") or []
                    if not items:
                        break
                    hit_seen = False
                    for it in items:
                        v = self._parse_item(it)
                        if not v or v["id"] in ids:
                            continue
                        if v["id"] in seen:
                            hit_seen = True  # 翻到已见过的动态,后面不用再翻了
                        ids.add(v["id"])
                        videos.append(v)
                    offset = data.get("offset") or ""
                    if hit_seen or not offset:
                        break
        except Exception as e:
            if not videos:
                return [], (0, f"网络错误 {e.__class__.__name__}")
        return videos, None

    @staticmethod
    def _parse_item(it):
        """把一条动态解析成视频信息,非投稿视频返回 None。"""
        mods = it.get("modules") or {}
        # 2026-08 实测:类型标记在 module_dynamic.major.type(旧版 major_type 字段已废弃为 None)
        dyn = mods.get("module_dynamic") or {}
        major = dyn.get("major") or {}
        mtype = major.get("type") or dyn.get("major_type") or mods.get("major_type")
        if mtype != "MAJOR_TYPE_ARCHIVE":
            return None  # 只关心投稿视频
        archive = major.get("archive") or {}
        bvid = archive.get("bvid")
        if not bvid:
            return None
        author_mod = mods.get("module_author") or {}
        return {
            "id": it.get("id_str") or bvid,
            "author": author_mod.get("name") or "未知UP主",
            "title": archive.get("title") or "无标题",
            "bvid": bvid,
            "pub_ts": author_mod.get("pub_ts") or 0,
        }

    @staticmethod
    def _save_state(st, seen_list, pending):
        st = dict(st)
        st["seen"] = list(seen_list)[-SEEN_CAP:]
        st["pending"] = list(pending)[-PENDING_CAP:]
        return st

    # ---------- 推送 ----------
    async def _warn_cookie(self, code, msg):
        now = time.time()
        if now - self._cookie_warned_at < COOKIE_WARN_COOLDOWN:
            return
        self._cookie_warned_at = now
        owner = self.cfg.OWNER_QQ
        if not owner:
            return
        text = (f"⚠️ B站更新提醒不可用:登录凭证已失效(code={code})。\n"
                f"请更新 plugins/bilibili.json 里的 sessdata 后重启机器人。")
        await self.client.send_action(
            "send_private_msg", {"user_id": int(owner), "message": text})

    @staticmethod
    def _render_batch(batch) -> str:
        lines = [f"🎬 关注的UP主更新 {len(batch)} 条"]
        lines += [f"{i}. {v['author']}:{v['title']}"
                  for i, v in enumerate(batch, 1)]
        return "\n".join(lines)

    # ---------- 主人命令 ----------
    async def on_message(self, ev: events.MessageEvent) -> bool:
        text = events.text_of(ev.segments).strip()
        if not text.startswith("#B站"):
            return False
        if not self.router.is_owner(ev.user_id):
            return True  # 仅主人可用,吞掉命令
        if text.startswith("#B站检查"):
            await self.router.reply(ev, "⏳ 正在检查B站更新…")
            result = await self.check_once()
            await self.router.reply(ev, f"B站检查:{result}")
            return True
        # #B站状态
        cfg = _load_json(CONFIG_PATH, {})
        st = _load_json(STATE_PATH, {})
        delay = self._seconds_until_push()
        nxt = datetime.now() + timedelta(seconds=delay)
        lines = [
            f"配置:{'✅ 已填 sessdata' if (cfg.get('sessdata') or '').strip() else '❌ 未填'}",
            f"推送时间:每天 {PUSH_HOUR}:{PUSH_MINUTE:02d},下次 {nxt.strftime('%m-%d %H:%M')}",
            f"上次成功:{_fmt_time(st['last_ok']) if st.get('last_ok') else '从未'}",
            f"已记录动态:{len(st.get('seen', []))} 条",
            f"待推送:{len(st.get('pending', []))} 条",
        ]
        if st.get("last_error"):
            lines.append(f"最近错误:{st['last_error']}")
        await self.router.reply(ev, "#B站状态\n" + "\n".join(lines))
        return True
