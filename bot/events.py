"""OneBot v11 事件模型与解析"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Segment:
    """消息段，如 {'type': 'text', 'data': {'text': '你好'}}"""
    type: str
    data: dict = field(default_factory=dict)


def parse_segments(message) -> list[Segment]:
    """把 OneBot 的 message 字段（数组或 CQ 码字符串）解析成 Segment 列表"""
    if isinstance(message, str):
        # 极简 CQ 码支持：大部分情况按纯文本处理
        return [Segment("text", {"text": message})]
    segs = []
    for s in message or []:
        if isinstance(s, dict):
            segs.append(Segment(s.get("type", "text"), s.get("data") or {}))
    return segs


def text_of(segments: list[Segment]) -> str:
    """提取消息里的纯文本（跳过 @、图片、表情等）"""
    return "".join(s.data.get("text", "") for s in segments if s.type == "text")


def at_qq(segments: list[Segment]) -> list[int]:
    """返回消息里所有被 @ 的 QQ 号"""
    out = []
    for s in segments:
        if s.type == "at":
            qq = s.data.get("qq")
            if qq and str(qq).isdigit():
                out.append(int(qq))
    return out


@dataclass
class MessageEvent:
    message_type: str  # group / private
    user_id: int
    group_id: Optional[int]
    message_id: int
    segments: list[Segment]
    raw: str
    self_id: int


@dataclass
class NoticeEvent:
    notice_type: str  # group_increase / group_decrease ...
    group_id: Optional[int]
    user_id: int
    operator_id: Optional[int]


@dataclass
class RequestEvent:
    request_type: str


@dataclass
class MetaEvent:
    meta_event_type: str  # heartbeat / lifecycle


def parse_event(obj: dict):
    """把一个 OneBot 事件 dict 解析成对应的 Event 对象，不认识就返回 None"""
    pt = obj.get("post_type")
    if pt == "message":
        mt = obj.get("message_type")
        if mt in ("group", "private"):
            return MessageEvent(
                message_type=mt,
                user_id=int(obj.get("user_id", 0)),
                group_id=int(obj["group_id"]) if obj.get("group_id") else None,
                message_id=int(obj.get("message_id", 0)),
                segments=parse_segments(obj.get("message")),
                raw=obj.get("raw_message") or "",
                self_id=int(obj.get("self_id", 0)),
            )
    elif pt == "notice":
        return NoticeEvent(
            notice_type=obj.get("notice_type", ""),
            group_id=int(obj["group_id"]) if obj.get("group_id") else None,
            user_id=int(obj.get("user_id", 0)),
            operator_id=int(obj["operator_id"]) if obj.get("operator_id") else None,
        )
    elif pt == "request":
        return RequestEvent(request_type=obj.get("request_type", ""))
    elif pt == "meta_event":
        return MetaEvent(meta_event_type=obj.get("meta_event_type", ""))
    return None
