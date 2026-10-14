"""QQ 伴侣机器人入口

用法：
    pip install -r requirements.txt
    python main.py
"""
import asyncio
import logging

from bot.client import OneBotClient
from bot.config import Config
from bot.db import DB
from bot.router import Router
from plugins.admin import AdminPlugin
from plugins.aihaoke import AihaokePlugin
from plugins.bilibili import BilibiliPlugin
from plugins.canvas import CanvasPlugin
from plugins.chat import ChatPlugin
from plugins.fun import FunPlugin
from plugins.help import HelpPlugin
from plugins.jwc import JwcPlugin
from plugins.macro import MacroPlugin
from plugins.remind import RemindPlugin
from plugins.sjtu_academic import SjtuAcademicPlugin

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    # 带日期。原来只有 %H:%M:%S，日志跨天(乃至跨周)后完全无法判断某行是哪天写的，
    # 排查"某个动作到底发没发生、什么时候发生"时吃了大亏(2026-10-03)。
    datefmt="%m-%d %H:%M:%S",
)


def build_bot() -> OneBotClient:
    cfg = Config()
    db = DB(cfg.DB_PATH)
    client = OneBotClient(cfg.WS_URL)
    router = Router(client, db, cfg)
    # 注册顺序：命令类插件在前，陪聊最后兜底
    for plugin in (RemindPlugin(), AdminPlugin(), FunPlugin(), MacroPlugin(),
                   BilibiliPlugin(), CanvasPlugin(), AihaokePlugin(),
                   SjtuAcademicPlugin(), JwcPlugin(), HelpPlugin(), ChatPlugin()):
        router.register(plugin)
    client.on_event = router.handle
    return client


async def main():
    client = build_bot()
    await client.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已退出。")
