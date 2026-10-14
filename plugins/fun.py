"""娱乐功能：骰子 / 硬币 / 今日运势"""
import random

from bot import events
from bot.router import Plugin

FORTUNES = [
    ("大吉", "鸿运当头，今天想做什么都能成！🍀"),
    ("吉", "顺顺利利，记得对自己好一点～"),
    ("中吉", "平平淡淡也是福，稳住心态～"),
    ("小吉", "小确幸在等你，留意身边的小美好。"),
    ("凶", "今天的坎儿都是纸老虎，别怕！"),
    ("大凶", "？赶紧放下手机出门走走！"),
]


class FunPlugin(Plugin):
    COMMANDS = (
        ("#掷骰子", "", False, "掷一个骰子(1-6)"),
        ("#翻硬币", "", False, "抛硬币猜正反"),
        ("#今日运势", "", False, "抽一条今日运势"),
    )

    async def on_message(self, ev: events.MessageEvent) -> bool:
        text = events.text_of(ev.segments).strip()
        if not text.startswith("#"):
            return False
        cmd = text[1:].strip()

        if cmd == "掷骰子":
            await self.router.reply(ev, f"🎲 你掷出了 {random.randint(1, 6)} 点！")
            return True
        if cmd == "翻硬币":
            await self.router.reply(ev, "🪙 " + random.choice(["正面", "反面"]) + "！")
            return True
        if cmd == "今日运势":
            level, tip = random.choice(FORTUNES)
            await self.router.reply(ev, f"🔮 今日运势：{level}\n{tip}")
            return True
        return False
