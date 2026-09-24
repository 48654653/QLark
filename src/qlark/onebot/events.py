"""OneBot 11 事件模型与解析。

QLark 关心的只有 ``post_type == "message"`` 的消息事件；其余（``notice`` /
``meta_event`` / ``request``）只做计数与日志，不进入回复流水线。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

#: 会话类型
GROUP = "group"
PRIVATE = "private"


def make_session_id(message_type: str, *, group_id: str | None = None, user_id: str | None = None) -> str:
    """会话身份。

    群聊 → ``group:<群号>``（全群共用一份记忆）
    私聊 → ``private:<QQ号>``（每个用户独立）
    """
    if message_type == GROUP and group_id:
        return f"group:{group_id}"
    return f"private:{user_id}"


@dataclass
class MessageEvent:
    """一条收到的消息。"""

    message_id: str
    message_type: str  # group | private
    sub_type: str
    user_id: str
    self_id: str
    segments: list[dict[str, Any]] = field(default_factory=list)
    sender: dict[str, Any] = field(default_factory=dict)
    raw_message: str = ""
    time: int = 0
    group_id: str | None = None

    # ------------------------------------------------------------------ 派生
    @property
    def is_group(self) -> bool:
        return self.message_type == GROUP

    @property
    def session_id(self) -> str:
        return make_session_id(
            self.message_type, group_id=self.group_id, user_id=self.user_id
        )

    @property
    def is_from_self(self) -> bool:
        return bool(self.self_id) and self.user_id == self.self_id

    @property
    def sender_name(self) -> str:
        """群名片 → 昵称 → QQ 号。"""
        for key in ("card", "nickname"):
            value = self.sender.get(key)
            if value:
                return str(value)
        return self.user_id

    @property
    def sender_role(self) -> str:
        """群内角色：``owner`` / ``admin`` / ``member``（私聊为空串）。"""
        return str(self.sender.get("role") or "")

    def describe(self) -> str:
        where = f"群 {self.group_id}" if self.is_group else "私聊"
        return f"{where} · {self.sender_name}({self.user_id}) · #{self.message_id}"


def parse_message_event(payload: Any, *, self_id: str | None = None) -> MessageEvent | None:
    """把 OneBot 事件负载解析成 :class:`MessageEvent`；不是消息事件则返回 ``None``。

    Args:
        self_id: 已知的机器人账号。负载里通常也带 ``self_id``，但显式传入可以
            在负载缺字段时兜底。
    """
    if not isinstance(payload, dict):
        return None
    if str(payload.get("post_type") or "") != "message":
        return None

    message_type = str(payload.get("message_type") or "")
    if message_type not in (GROUP, PRIVATE):
        return None

    segments_raw = payload.get("message")
    if isinstance(segments_raw, list):
        segments = [seg for seg in segments_raw if isinstance(seg, dict)]
    elif isinstance(segments_raw, str):
        # 理论上不会发生（我们配的是 array），但别让一条怪消息打断整条链路
        segments = [{"type": "text", "data": {"text": segments_raw}}]
    else:
        segments = []

    sender = payload.get("sender")
    if not isinstance(sender, dict):
        sender = {}

    group_id = payload.get("group_id")
    return MessageEvent(
        message_id=str(payload.get("message_id") or ""),
        message_type=message_type,
        sub_type=str(payload.get("sub_type") or ""),
        user_id=str(payload.get("user_id") or ""),
        self_id=str(payload.get("self_id") or self_id or ""),
        segments=segments,
        sender=sender,
        raw_message=str(payload.get("raw_message") or ""),
        time=int(payload.get("time") or time.time()),
        group_id=str(group_id) if group_id is not None else None,
    )


def describe_event(payload: Any) -> str:
    """一行话描述事件，用于日志。"""
    if not isinstance(payload, dict):
        return "无法识别的事件"
    post_type = str(payload.get("post_type") or "?")
    if post_type == "message":
        kind = str(payload.get("message_type") or "?")
        who = payload.get("sender") or {}
        name = who.get("card") or who.get("nickname") or payload.get("user_id")
        where = payload.get("group_id") or payload.get("user_id")
        return f"消息事件 {kind} {where} ← {name}"
    if post_type == "notice":
        return f"通知事件 {payload.get('notice_type')}"
    if post_type == "meta_event":
        return f"元事件 {payload.get('meta_event_type')}"
    if post_type == "request":
        return f"请求事件 {payload.get('request_type')}"
    return f"事件 {post_type}"


__all__ = [
    "GROUP",
    "PRIVATE",
    "MessageEvent",
    "describe_event",
    "make_session_id",
    "parse_message_event",
]
