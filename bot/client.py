"""OneBot v11 WebSocket 客户端：连接、收发、自动重连"""
import asyncio
import json
import logging

import aiohttp

from . import events

log = logging.getLogger("client")


class OneBotClient:
    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.session: aiohttp.ClientSession | None = None
        self.ws = None
        self.on_event = None  # async 回调，收到事件时调用（由 Router 提供）
        self._send_lock = asyncio.Lock()
        self._self_id = 0

    @property
    def self_id(self) -> int:
        return self._self_id

    async def run(self):
        """主循环：连接 -> 读取事件 -> 断线重连（永久运行）"""
        while True:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("连接异常: %s", e)
            finally:
                if self.ws is not None:
                    try:
                        await self.ws.close()
                    except Exception:
                        pass
                    self.ws = None
                if self.session is not None:
                    try:
                        await self.session.close()
                    except Exception:
                        pass
                    self.session = None
            log.info("5 秒后尝试重连 %s ...", self.ws_url)
            await asyncio.sleep(5)

    async def _run_once(self):
        self.session = aiohttp.ClientSession()
        log.info("正在连接 %s ...", self.ws_url)
        async with self.session.ws_connect(self.ws_url, heartbeat=30) as ws:
            self.ws = ws
            log.info("WS 已连接 ✅")
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_text(msg.data)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.ERROR,
                ):
                    log.info("WS 连接被关闭")
                    break

    async def _handle_text(self, data: str):
        try:
            obj = json.loads(data)
        except (ValueError, TypeError):
            return
        if not isinstance(obj, dict) or obj.get("post_type") is None:
            return  # 这是 action 调用的响应，不是事件，忽略
        if obj.get("self_id"):
            self._self_id = int(obj["self_id"])
        ev = events.parse_event(obj)
        if ev is not None and self.on_event is not None:
            try:
                await self.on_event(ev)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("处理事件失败")

    async def send_action(self, action: str, params: dict) -> bool:
        """发送一个 OneBot action（如 send_group_msg / set_group_ban）。发完即返回。"""
        if self.ws is None:
            log.warning("WS 未连接，无法发送 %s", action)
            return False
        payload = {"action": action, "params": params}
        try:
            async with self._send_lock:
                await self.ws.send_json(payload)
            return True
        except Exception as e:
            log.warning("发送 %s 失败: %s", action, e)
            return False
