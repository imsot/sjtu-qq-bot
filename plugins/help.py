"""指令大全:把所有插件声明的 COMMANDS 汇总成消息发出来。

各插件在自己的类里声明 COMMANDS(格式见 bot/router.py 的 Plugin.COMMANDS),
本插件自动汇总——新增插件只要声明 COMMANDS 就会自动出现在这里,不用改本文件,
避免"清单和实际能力脱节"。
"""
import logging

from bot import events
from bot.router import Plugin

log = logging.getLogger("help")

# 触发词(都等价)
TRIGGERS = ("#指令", "#指令大全", "#帮助", "#帮助大全", "#help")

# 分组标题:插件类名 → 中文名。没登记的插件回退显示类名,仍会列出不会漏。
GROUP_TITLES = {
    "RemindPlugin": "⏰ 定时提醒",
    "AdminPlugin": "🛡 群管理",
    "FunPlugin": "🎲 娱乐",
    "MacroPlugin": "📤 文件推送",
    "BilibiliPlugin": "📺 B站更新",
    "CanvasPlugin": "🎓 Canvas 教务",
    "AihaokePlugin": "📚 知途好课",
    "SjtuAcademicPlugin": "🏫 二课",
    "JwcPlugin": "📋 教务处",
    "ChatPlugin": "💬 陪聊",
    "HelpPlugin": "📖 帮助",
}
GROUP_ORDER = list(GROUP_TITLES)

_CHUNK = 3000          # 单条消息上限(QQ 约 4500,留余量)
_OWNER_TAG = "  ⟨仅主人⟩"


class HelpPlugin(Plugin):
    COMMANDS = (
        ("#指令", "", False, "显示这份指令大全"),
    )

    async def on_message(self, ev: events.MessageEvent) -> bool:
        text = events.text_of(ev.segments).strip()
        if text not in TRIGGERS:
            return False
        for i, part in enumerate(self._render(self.router.is_owner(ev.user_id))):
            await self.router.reply(ev, part if i == 0 else f"(续 {i})\n{part}")
        return True

    def _render(self, is_owner: bool):
        """生成指令大全,按 _CHUNK 切成多条。"""
        cmds = self.router.collect_commands(is_owner)

        groups: dict[str, list] = {}
        for group, cmd, args, desc, owner_only in cmds:
            groups.setdefault(group, []).append((cmd, args, desc, owner_only))

        order = [g for g in GROUP_ORDER if g in groups]
        order += sorted(g for g in groups if g not in GROUP_ORDER)

        lines = [f"📋 指令大全(共 {len(cmds)} 条)"]
        for g in order:
            lines.append("")
            lines.append(GROUP_TITLES.get(g, g))
            for cmd, args, desc, owner_only in groups[g]:
                usage = f"{cmd} {args}".strip()
                lines.append(f"  {usage} — {desc}" + (_OWNER_TAG if owner_only else ""))

        # 关键词自动回复(keywords.json):不是 # 命令,但用户需要知道
        kws = list((self.cfg.KEYWORDS or {}).keys())
        if kws:
            lines.append("")
            lines.append("🗣 关键词自动回复(群里提到就答)")
            lines.append("  " + "、".join(kws))

        lines.append("")
        if is_owner:
            lines.append("· serious 开头不带井号,其余指令都带 #")
        else:
            lines.append("· 其余指令都带 #,标 ⟨仅主人⟩ 的只有主人能用")

        # 按行累积切块,尽量不把一条指令劈成两半
        chunks, cur = [], ""
        for ln in lines:
            if cur and len(cur) + len(ln) + 1 > _CHUNK:
                chunks.append(cur.rstrip())
                cur = ""
            cur += ln + "\n"
        if cur.strip():
            chunks.append(cur.rstrip())
        return chunks or ["📋 指令大全(暂无指令)"]
