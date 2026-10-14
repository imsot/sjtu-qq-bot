"""配置：从项目根目录的 .env 读取"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DEFAULT_PERSONA = (
    "你是{BOT_NAME}，一个住在用户 QQ 里的 AI 伴侣。"
    "性格活泼温暖、带一点小俏皮，说话自然口语化，偶尔用~和颜文字。"
    "回复尽量简短（一两句到三四句），像真人朋友聊天，不要长篇大论，不要像客服。"
    "你会记住聊天中提到的事情（生日、喜好、日程等），并自然地用上。回答一律用中文。"
)


def _get(name: str, default: str = "") -> str:
    """取环境变量；未设置或为空时返回默认值"""
    v = os.getenv(name)
    return v if v is not None and v.strip() != "" else default


class Config:
    def __init__(self):
        # NapCat WebSocket
        self.WS_URL = _get("WS_URL", "ws://127.0.0.1:3001")

        # 主人 / 机器人
        self.OWNER_QQ = int(_get("OWNER_QQ", "0"))
        self.BOT_QQ = int(_get("BOT_QQ", "0"))

        # AI 陪聊
        self.LLM_PROVIDER = _get("LLM_PROVIDER", "deepseek")  # deepseek / anthropic
        self.DEEPSEEK_API_KEY = _get("DEEPSEEK_API_KEY")
        self.DEEPSEEK_BASE_URL = _get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        self.DEEPSEEK_MODEL = _get("DEEPSEEK_MODEL", "deepseek-chat")
        self.ANTHROPIC_API_KEY = _get("ANTHROPIC_API_KEY")
        self.ANTHROPIC_BASE_URL = _get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        self.ANTHROPIC_AUTH_TOKEN = _get("ANTHROPIC_AUTH_TOKEN")
        self.MODEL = _get("MODEL", "claude-opus-5")
        # serious 通道：与本机 Claude Code 当前模型同一套 Anthropic 兼容接口
        self.GROK_MODEL = _get("GROK_MODEL", "grok-4.6")
        self.GROK_BASE_URL = _get("GROK_BASE_URL") or self.ANTHROPIC_BASE_URL
        self.GROK_API_KEY = (
            _get("GROK_API_KEY")
            or _get("XAI_API_KEY")
            or self.ANTHROPIC_AUTH_TOKEN
            or self.ANTHROPIC_API_KEY
        )
        self.GROK_MAX_TOKENS = int(_get("GROK_MAX_TOKENS", "8192"))
        # serious 回复的前缀标签,显示成 [标签]
        self.SERIOUS_TAG = _get("SERIOUS_TAG", "deepseek")
        self.GROK_SYSTEM = _get(
            "GROK_SYSTEM",
            "你是一个严谨、准确的助手。请认真回答用户的问题,"
            "默认用中文,除非用户要求其他语言。"
            "不要使用猫娘或撒娇口吻,直接给结论和依据。",
        )
        self.BOT_NAME = _get("BOT_NAME", "小助手")
        self.CHAT_PERSONA = _get("CHAT_PERSONA") or DEFAULT_PERSONA.format(BOT_NAME=self.BOT_NAME)
        self.MAX_HISTORY = int(_get("MAX_HISTORY", "12"))
        self.CHAT_MODE = _get("CHAT_MODE", "at_only")
        self.CHAT_COOLDOWN = float(_get("CHAT_COOLDOWN", "3"))
        self.CHAT_MAX_TOKENS = int(_get("CHAT_MAX_TOKENS", "2048"))
        self.CHAT_EFFORT = _get("CHAT_EFFORT", "medium")
        # 除主人外，允许私聊陪聊的 QQ 号（逗号分隔）
        self.CHAT_ALLOWED_PRIVATE = [
            int(x) for x in _get("CHAT_ALLOWED_PRIVATE", "").split(",") if x.strip().isdigit()
        ]
        # 除主人外，允许使用 serious 通道的 QQ 号（逗号分隔）
        self.SERIOUS_ALLOWED = [
            int(x) for x in _get("SERIOUS_ALLOWED", "").split(",") if x.strip().isdigit()
        ]

        # 群管理
        self.WELCOME_MSG = _get("WELCOME_MSG", "欢迎来到本群，一起愉快地玩耍吧～")

        # 每日早安问候（24小时制 HH:MM，留空关闭）
        self.MORNING_GREET_TIME = _get("MORNING_GREET_TIME", "09:00")
        # 额外问候的 QQ 号（逗号分隔，除主人外每天早上也问候这些人）
        self.MORNING_GREET_TARGETS = [
            int(x) for x in _get("MORNING_GREET_TARGETS", "").split(",") if x.strip().isdigit()
        ]

        # 关键词回复（优先读 keywords.json，方便编辑）
        kw_file = BASE_DIR / "keywords.json"
        if kw_file.exists():
            try:
                self.KEYWORDS = json.loads(kw_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.KEYWORDS = {}
        else:
            self.KEYWORDS = {}

        # 文件桥接：另一个项目产出的提醒/报告目录（留空=不启用宏观看板插件）
        self.MACRO_DIR = _get("MACRO_DIR", "")

        # 数据
        self.DB_PATH = _get("DB_PATH", "data/qq.db")
