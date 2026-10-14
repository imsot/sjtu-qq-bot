"""定时提醒：SQLite 持久化 + 后台轮询（仅主人可用）"""
import asyncio
import re
import time
from datetime import datetime, timedelta

from bot import events
from bot.router import Plugin

# 相对时间：'5分钟' / '2小时' / '3天后'
REL_RE = re.compile(r"^(\d+)\s*(分钟|小时|天)(后)?")
# 时刻：'明早8点' / '明早8点半' / '8点30' / '晚上9点'
CLOCK_RE = re.compile(r"^(明天|明早|明晚|今天|今晚|早上|中午|下午|晚上)?\s*(\d{1,2})[点时](半|(\d{1,2})分?)?\s*")
DAY_HINTS = ("明天", "明早", "明晚")
PM_HINTS = ("晚上", "今晚", "下午")  # 这些提示词下的钟点按 24 小时制理解


def parse_remind(text: str) -> tuple[float | None, str | None]:
    """解析 '#提醒' 后面的内容，返回 (fire_at 时间戳, 提醒内容)；无法解析返回 (None, None)"""
    now = datetime.now()

    m = REL_RE.match(text)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"分钟": timedelta(minutes=n), "小时": timedelta(hours=n), "天": timedelta(days=n)}[unit]
        fire = now + delta
        return fire.timestamp(), (text[m.end():].strip() or "到点啦")

    m = CLOCK_RE.match(text)
    if m:
        day_hint, hour, half, minute = m.group(1), int(m.group(2)), m.group(3), m.group(4)
        if half == "半":
            minute = 30
        minute = int(minute) if minute else 0
        if day_hint in PM_HINTS:
            if hour == 12:
                hour = 0  # 晚上12点 = 午夜
            elif hour < 12:
                hour += 12  # 晚上9点 = 21点
        fire = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if day_hint in DAY_HINTS:
            fire += timedelta(days=1)
        elif fire <= now:
            fire += timedelta(days=1)  # 今天这个点已过，顺延到明天
        return fire.timestamp(), (text[m.end():].strip() or "到点啦")

    return None, None


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


class RemindPlugin(Plugin):
    COMMANDS = (
        ("#提醒", "<时间> <内容>", True, "设置定时提醒,如 #提醒 5分钟 喝水"),
        ("#我的提醒", "", True, "查看待办提醒"),
        ("#取消提醒", "<编号>", True, "取消某条提醒"),
    )

    def setup(self, router):
        super().setup(router)
        self._tick_task = asyncio.create_task(self._tick())
        self._greet_task = asyncio.create_task(self._greet_loop())
        self._greeted_date = None

    async def _tick(self):
        while True:
            try:
                await self._fire_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # 单次轮询失败不致命，下轮重试
            await asyncio.sleep(20)

    # ---------- 每日早安问候 ----------
    async def _greet_loop(self):
        """每天 MORNING_GREET_TIME 私聊问候主人"""
        while True:
            try:
                target = self.cfg.MORNING_GREET_TIME.strip()
                now = datetime.now()
                if target and now.strftime("%H:%M") == target and self._greeted_date != now.date():
                    await self._send_morning_greeting()
                    self._greeted_date = now.date()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # 单次失败不致命
            await asyncio.sleep(30)  # 每 30 秒检查一次，保证不跳过到点的那分钟

    async def _send_morning_greeting(self):
        owner = self.cfg.OWNER_QQ
        if not owner:
            return
        # 给主人的问候（带"主人"称呼）
        message = await self._build_greeting()
        await self.client.send_action("send_private_msg", {"user_id": owner, "message": message})
        # 给其他指定用户的问候（通用版，不带称呼）
        for qq in self.cfg.MORNING_GREET_TARGETS:
            message = await self._build_greeting(is_owner=False)
            await self.client.send_action("send_private_msg", {"user_id": qq, "message": message})

    async def _build_greeting(self, is_owner: bool = True) -> str:
        """用 AI 按人设生成早安问候；AI 不可用时用默认文案"""
        try:
            chat = next(
                (p for p in self.router.plugins if type(p).__name__ == "ChatPlugin"), None
            )
            if chat is not None and chat._ai is not None:
                if is_owner:
                    prompt = "现在是早上九点，请用你的人设对主人说一句早安问候，一句到两句话即可，不要解释。"
                else:
                    prompt = "现在是早上九点，请用你的人设说一句早安问候，一句到两句话即可，不要加称呼，不要解释。"
                reply = await chat._ask(chat.cfg.CHAT_PERSONA, [{"role": "user", "content": prompt}])
                if reply:
                    return reply
        except Exception:
            pass  # AI 失败走默认文案
        if is_owner:
            return f"早上好呀，{self.cfg.BOT_NAME}来问安啦～今天也要元气满满！☀️"
        return f"早安～新的一天也要元气满满哦！☀️"

    async def _fire_due(self):
        for r in self.db.due_reminders(time.time()):
            msg = f"⏰ 主人，到点了！\n{r['content']}"
            await self.client.send_action("send_private_msg", {"user_id": int(r["owner"]), "message": msg})
            self.db.delete_reminder(r["id"])

    async def on_message(self, ev: events.MessageEvent) -> bool:
        text = events.text_of(ev.segments).strip()
        if not text.startswith("#"):
            return False
        if not self.router.is_owner(ev.user_id):
            return False
        cmd, _, arg = text.partition(" ")
        cmd = cmd.strip()
        arg = arg.strip()

        if cmd == "#提醒":
            if not arg:
                await self.router.reply(ev, "用法：`#提醒 5分钟 喝水` 或 `#提醒 明早8点 打卡`")
                return True
            fire_at, content = parse_remind(arg)
            if fire_at is None:
                await self.router.reply(
                    ev, "时间格式没看懂哦～试试 `#提醒 5分钟 喝水` 或 `#提醒 明早8点 打卡`"
                )
                return True
            rid = self.db.add_reminder(
                target=f"private:{ev.user_id}", fire_at=fire_at, content=content, owner=str(ev.user_id)
            )
            await self.router.reply(ev, f"好嘞！已记下 #{rid}：{fmt_time(fire_at)} {content}，到点提醒你～")
            return True

        if cmd == "#我的提醒":
            rows = self.db.list_reminders(str(ev.user_id))
            if not rows:
                await self.router.reply(ev, "现在还没有待提醒的事项哦～")
            else:
                lines = [f"#{r['id']} {fmt_time(r['fire_at'])}：{r['content']}" for r in rows]
                await self.router.reply(ev, "你的待办提醒：\n" + "\n".join(lines))
            return True

        if cmd == "#取消提醒":
            if not arg.isdigit():
                await self.router.reply(ev, "用法：`#取消提醒 <编号>`（编号见 `#我的提醒`）")
                return True
            rid = int(arg)
            if any(r["id"] == rid for r in self.db.list_reminders(str(ev.user_id))):
                self.db.delete_reminder(rid)
                await self.router.reply(ev, f"已取消提醒 #{rid} ✅")
            else:
                await self.router.reply(ev, f"没找到 #{rid} 哦～")
            return True

        return False
