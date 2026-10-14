"""插件路由：事件分发、权限校验、冷却、发送助手"""
import asyncio
import logging
import time

from . import events

log = logging.getLogger("router")


class Plugin:
    """插件基类：子类实现 on_message / on_notice，返回 True 表示已处理该事件。"""

    # 消息正文以这些前缀开头时，路由日志只记前缀不记正文。凭证类命令
    # (如 #二课token <JWT>)必须声明，否则 token 会落进 bot_out.log/bot_err.log。
    redact_prefixes: tuple = ()

    # 本插件对外提供的指令，供 `#指令` 指令大全自动汇总。声明格式：
    #   ((触发词, 参数说明, 是否仅主人, 作用), ...)
    # 参数说明留空串表示无需参数。新增插件时声明这里即可，指令大全自动收录，
    # 不需要改 help.py（避免清单与实际能力脱节）。
    COMMANDS: tuple = ()

    def __init__(self):
        self.router: "Router | None" = None
        self.cfg = None
        self.client = None
        self.db = None

    def setup(self, router: "Router"):
        self.router = router
        self.cfg = router.cfg
        self.client = router.client
        self.db = router.db

    async def on_message(self, ev: events.MessageEvent) -> bool:
        return False

    async def on_notice(self, ev: events.NoticeEvent) -> bool:
        return False


class Router:
    def __init__(self, client, db, cfg):
        self.client = client
        self.db = db
        self.cfg = cfg
        self.plugins: list[Plugin] = []
        self._cooldowns: dict = {}

    def register(self, plugin: Plugin):
        plugin.setup(self)
        self.plugins.append(plugin)

    async def handle(self, ev):
        """事件总入口：按注册顺序交给插件，第一个处理的插件胜出"""
        if isinstance(ev, events.MessageEvent):
            log.info("收到消息 type=%s user=%s group=%s text=%r",
                     ev.message_type, ev.user_id, ev.group_id,
                     self._log_text(events.text_of(ev.segments)))
            if ev.self_id and ev.user_id == ev.self_id:
                return  # 不处理机器人自己发的消息
            for p in self.plugins:
                try:
                    if await p.on_message(ev):
                        return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("插件 %s 处理消息出错", type(p).__name__)
        elif isinstance(ev, events.NoticeEvent):
            for p in self.plugins:
                try:
                    if await p.on_notice(ev):
                        return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("插件 %s 处理通知出错", type(p).__name__)

    # ---------- 工具 ----------
    def _log_text(self, text: str) -> str:
        """日志里的消息正文：凭证类命令只留前缀，其余截断到 40 字符。"""
        for p in self.plugins:
            for pre in getattr(p, "redact_prefixes", ()):
                if text.startswith(pre):
                    return f"{pre} <正文已隐去>"
        return text[:40]

    def is_owner(self, qq) -> bool:
        return bool(self.cfg.OWNER_QQ) and int(qq) == self.cfg.OWNER_QQ

    def collect_commands(self, is_owner: bool) -> list[tuple[str, str, str, str, bool]]:
        """汇总所有插件声明的指令，返回 [(分组, 触发词, 参数, 作用, 仅主人)]。

        is_owner=False 时过滤掉仅主人可用的指令（非主人看到也没用）。
        """
        out = []
        for p in self.plugins:
            group = type(p).__name__
            for item in getattr(p, "COMMANDS", ()):
                cmd, args, owner_only, desc = item
                if owner_only and not is_owner:
                    continue
                out.append((group, cmd, args, desc, owner_only))
        return out

    def in_cooldown(self, key, seconds: float) -> bool:
        """冷却：key 在 seconds 秒内再次触发返回 True"""
        now = time.monotonic()
        last = self._cooldowns.get(key, 0.0)
        if now - last < seconds:
            return True
        self._cooldowns[key] = now
        return False

    async def reply(self, ev: events.MessageEvent, message):
        """回复到消息来源（群 / 私聊）"""
        if ev.message_type == "group":
            await self.client.send_action("send_group_msg", {"group_id": ev.group_id, "message": message})
        else:
            await self.client.send_action("send_private_msg", {"user_id": ev.user_id, "message": message})

    async def send_to_target(self, target: str, message):
        """target 形如 'group:123' / 'private:456'"""
        kind, _, id_ = target.partition(":")
        if not id_.isdigit():
            return
        if kind == "group":
            await self.client.send_action("send_group_msg", {"group_id": int(id_), "message": message})
        else:
            await self.client.send_action("send_private_msg", {"user_id": int(id_), "message": message})

    @staticmethod
    def build_message(*parts):
        """把字符串和 ('at', qq) 拼成 OneBot 消息段数组"""
        segs = []
        for p in parts:
            if isinstance(p, str):
                segs.append({"type": "text", "data": {"text": p}})
            else:
                segs.append({"type": "at", "data": {"qq": str(p[1])}})
        return segs
