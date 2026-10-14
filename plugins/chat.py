"""AI 陪聊：DeepSeek（默认）/ Claude 双后端，带记忆（最近 N 条对话）。

主人以及 SERIOUS_ALLOWED 白名单账号消息以 "serious " 开头时走独立通道，不混入陪聊人设。
"""
import logging
import re

from bot import events
from bot.router import Plugin

log = logging.getLogger("chat")

# 句首独立单词 serious,后面必须跟空白(大小写不敏感);不带井号
_SERIOUS_RE = re.compile(r"^serious\s+", re.IGNORECASE)
_QQ_CHUNK = 3500


class ChatPlugin(Plugin):
    COMMANDS = (
        ("serious", "<问题>", True, "严肃模式:交给严肃通道模型认真回答,不带人设(不带 #)"),
        ("私聊直接发消息", "", True, "和机器人聊天(带记忆)"),
        ("@机器人", "<任意内容>", False, "群里 @ 我就能聊"),
    )

    def setup(self, router):
        super().setup(router)
        self._ai = None
        self._grok = None
        self._provider = self.cfg.LLM_PROVIDER

        if self._provider == "deepseek":
            if self.cfg.DEEPSEEK_API_KEY:
                from openai import AsyncOpenAI

                self._ai = AsyncOpenAI(
                    api_key=self.cfg.DEEPSEEK_API_KEY,
                    base_url=self.cfg.DEEPSEEK_BASE_URL,
                )
            else:
                log.warning("未配置 DEEPSEEK_API_KEY，AI 陪聊功能不可用")
        elif self._provider == "anthropic":
            token = self.cfg.ANTHROPIC_AUTH_TOKEN or self.cfg.ANTHROPIC_API_KEY
            if token:
                import anthropic

                kwargs = {"base_url": self.cfg.ANTHROPIC_BASE_URL}
                if self.cfg.ANTHROPIC_AUTH_TOKEN:
                    kwargs["auth_token"] = self.cfg.ANTHROPIC_AUTH_TOKEN
                else:
                    kwargs["api_key"] = self.cfg.ANTHROPIC_API_KEY
                self._ai = anthropic.AsyncAnthropic(**kwargs)
            else:
                log.warning("未配置 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY，AI 陪聊功能不可用")
        else:
            log.warning("未知的 LLM_PROVIDER: %s（可选 deepseek / anthropic）", self._provider)

        if self.cfg.GROK_API_KEY:
            import anthropic

            self._grok = anthropic.AsyncAnthropic(
                api_key=self.cfg.GROK_API_KEY,
                base_url=self.cfg.GROK_BASE_URL,
            )
        else:
            log.warning("未配置 GROK/ANTHROPIC 凭证，serious 通道不可用")

    def _session(self, ev: events.MessageEvent) -> str:
        return f"{ev.message_type}:{ev.group_id or ev.user_id}"

    def _should_chat(self, ev: events.MessageEvent) -> bool:
        if self._ai is None:
            return False
        if ev.message_type == "private":
            # 私聊：主人 + 配置的白名单账号
            return self.router.is_owner(ev.user_id) or ev.user_id in self.cfg.CHAT_ALLOWED_PRIVATE
        # 群聊：被 @ 才回；或配置了 CHAT_MODE=all 则群内都回
        ats = events.at_qq(ev.segments)
        bot_qq = self.cfg.BOT_QQ or ev.self_id
        if ats and bot_qq in ats:
            return True
        return self.cfg.CHAT_MODE == "all"

    def _can_serious(self, qq) -> bool:
        qq = int(qq)
        return self.router.is_owner(qq) or qq in self.cfg.SERIOUS_ALLOWED

    async def _ask(self, system: str, messages: list[dict]) -> str:
        """调用大模型，返回回复文本"""
        if self._provider == "anthropic":
            resp = await self._ai.messages.create(
                model=self.cfg.MODEL,
                max_tokens=self.cfg.CHAT_MAX_TOKENS,
                system=system,
                messages=messages,
                output_config={"effort": self.cfg.CHAT_EFFORT},
            )
            return "".join(b.text for b in resp.content if b.type == "text").strip()

        # DeepSeek / OpenAI 兼容协议
        resp = await self._ai.chat.completions.create(
            model=self.cfg.DEEPSEEK_MODEL,
            max_tokens=self.cfg.CHAT_MAX_TOKENS,
            messages=[{"role": "system", "content": system}] + messages,
        )
        return (resp.choices[0].message.content or "").strip()

    async def _ask_grok(self, prompt: str) -> str:
        """走 Anthropic Messages API（凭证见 .env 的 GROK_*）。"""
        resp = await self._grok.messages.create(
            model=self.cfg.GROK_MODEL,
            max_tokens=self.cfg.GROK_MAX_TOKENS,
            system=self.cfg.GROK_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()

    @staticmethod
    def _chunks(text: str, size: int = _QQ_CHUNK):
        text = text.strip()
        if not text:
            yield "……"
            return
        for i in range(0, len(text), size):
            yield text[i:i + size]

    async def _handle_serious(self, ev: events.MessageEvent, prompt: str) -> bool:
        """serious 通道：正文交给严肃通道模型，不写入陪聊记忆。"""
        if not prompt:
            await self.router.reply(ev, "serious 后面要跟问题，例如：serious 解释一下反脆弱")
            return True
        if self._grok is None:
            await self.router.reply(ev, "serious 通道未配置 API 凭证，请在 .env 填写 ANTHROPIC_AUTH_TOKEN 后重启机器人。")
            return True
        if self.router.in_cooldown(("grok", ev.user_id), self.cfg.CHAT_COOLDOWN):
            return True
        try:
            reply = await self._ask_grok(prompt)
        except Exception as e:
            log.warning("Grok 调用失败: %s", e)
            await self.router.reply(ev, f"Grok 调用失败：{e}")
            return True
        if not reply:
            reply = "（Grok 没有返回内容）"
        first = True
        for chunk in self._chunks(reply):
            tag = self.cfg.SERIOUS_TAG or "deepseek"
            await self.router.reply(ev, f"[{tag}]\n{chunk}" if first else chunk)
            first = False
        return True

    async def on_message(self, ev: events.MessageEvent) -> bool:
        text = events.text_of(ev.segments).strip()
        # serious 通道:句首不带井号的 'serious xxx';放在 '#' 命令拦截之前判断,
        # 保证它不会被当成命令丢掉(2026-09-24)。
        if _SERIOUS_RE.match(text):
            if self._can_serious(ev.user_id):
                prompt = _SERIOUS_RE.sub("", text, count=1).strip()
                return await self._handle_serious(ev, prompt)
            text = _SERIOUS_RE.sub("", text, count=1).strip()   # 无权限者按普通聊天
            if not text:
                return False
        if not text or text.startswith("#"):
            return False  # 命令交给其他插件
        if not self._should_chat(ev):
            return False

        session = self._session(ev)
        if self.router.in_cooldown(("chat", session), self.cfg.CHAT_COOLDOWN):
            return True  # 冷却中，本轮不回复（避免刷屏）

        # 记录用户消息，带上历史上下文
        self.db.add_message(session, "user", text)
        self.db.trim(session)
        history = self.db.recent(session, self.cfg.MAX_HISTORY)
        messages = [{"role": role, "content": content} for role, content in history]
        if not messages:
            messages.append({"role": "user", "content": text})

        try:
            reply = await self._ask(self.cfg.CHAT_PERSONA, messages)
        except Exception as e:
            log.warning("AI 调用失败: %s", e)
            reply = "哎呀，我这边有点卡壳，等一会儿再叫我好不好呀～"

        if not reply:
            reply = "……"

        self.db.add_message(session, "assistant", reply)
        await self.router.reply(ev, reply)
        return True
