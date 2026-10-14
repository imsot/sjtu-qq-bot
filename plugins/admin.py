"""群管理：新人欢迎、关键词回复、禁言/解禁/移出（仅主人）"""
import re

from bot import events
from bot.router import Plugin

DURATION_RE = re.compile(r"(\d+)\s*(秒|分钟|小时)")
DURATION_UNIT = {"秒": 1, "分钟": 60, "小时": 3600}


def _parse_duration(arg: str) -> int:
    """把 '5分钟' 解析成秒；解析失败默认 10 分钟"""
    m = DURATION_RE.search(arg or "")
    if m:
        return int(m.group(1)) * DURATION_UNIT[m.group(2)]
    return 600


def _fmt_duration(seconds: int) -> str:
    if seconds % 60 == 0 and seconds > 0:
        return f"{seconds // 60}分钟"
    return f"{seconds}秒"


class AdminPlugin(Plugin):
    COMMANDS = (
        ("#禁言", "<@某人|QQ号> [时长]", True, "禁言群成员(时长默认10分钟)"),
        ("#解禁", "<@某人|QQ号>", True, "解除禁言"),
        ("#移出", "<@某人|QQ号>", True, "把成员移出群"),
        ("#全体禁言", "", True, "开启全员禁言"),
        ("#全体解禁", "", True, "关闭全员禁言"),
    )

    # ---------- 通知：新人进群 ----------
    async def on_notice(self, ev: events.NoticeEvent) -> bool:
        if ev.notice_type == "group_increase" and ev.group_id:
            if ev.user_id:
                msg = self.router.build_message(("at", ev.user_id), " " + self.cfg.WELCOME_MSG)
            else:
                msg = self.cfg.WELCOME_MSG
            await self.client.send_action("send_group_msg", {"group_id": ev.group_id, "message": msg})
            return True
        return False

    # ---------- 消息：关键词 / 管理命令 ----------
    async def on_message(self, ev: events.MessageEvent) -> bool:
        if ev.message_type != "group" or not ev.group_id:
            return False
        text = events.text_of(ev.segments).strip()
        if text.startswith("#"):
            return await self._admin_command(ev, text)
        for kw, reply in self.cfg.KEYWORDS.items():
            if kw and kw in text:
                await self.router.reply(ev, reply)
                return True
        return False

    async def _admin_command(self, ev: events.MessageEvent, text: str) -> bool:
        if not self.router.is_owner(ev.user_id):
            return False  # 非主人不响应，也不暴露命令存在
        cmd, _, arg = text.partition(" ")
        cmd = cmd.strip()
        group_id = ev.group_id

        if cmd in ("#禁言", "#解禁", "#移出"):
            ats = events.at_qq(ev.segments)
            if not ats:
                m = re.search(r"(\d{5,})", arg)
                if m:
                    ats = [int(m.group(1))]
            if not ats:
                await self.router.reply(ev, "要带上要处理的人哦，例如：`#禁言 @xxx 5分钟`")
                return True
            if cmd == "#禁言":
                duration = _parse_duration(arg)
                for qq in ats:
                    await self.client.send_action(
                        "set_group_ban", {"group_id": group_id, "user_id": qq, "duration": duration}
                    )
                await self.router.reply(ev, f"已禁言 {len(ats)} 人（{_fmt_duration(duration)}）✅")
            elif cmd == "#解禁":
                for qq in ats:
                    await self.client.send_action(
                        "set_group_ban", {"group_id": group_id, "user_id": qq, "duration": 0}
                    )
                await self.router.reply(ev, f"已解禁 {len(ats)} 人 ✅")
            else:
                for qq in ats:
                    await self.client.send_action("set_group_kick", {"group_id": group_id, "user_id": qq})
                await self.router.reply(ev, f"已移出 {len(ats)} 人 ✅")
            return True

        if cmd == "#全体禁言":
            await self.client.send_action("set_group_whole_ban", {"group_id": group_id, "enable": True})
            await self.router.reply(ev, "已开启全体禁言 ✅")
            return True
        if cmd == "#全体解禁":
            await self.client.send_action("set_group_whole_ban", {"group_id": group_id, "enable": False})
            await self.router.reply(ev, "已关闭全体禁言 ✅")
            return True
        return False
