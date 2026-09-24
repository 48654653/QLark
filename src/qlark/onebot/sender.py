"""把回复发回 QQ。

QQ 单条消息有长度上限，超长的回复必须分段，否则会发送失败。
分段优先级：段落 → 换行 → 句末标点 → 硬切。
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from ..onebot.client import OneBotError, OneBotClient
from ..onebot.events import MessageEvent
from ..onebot.segments import build_reply_segment

log = logging.getLogger(__name__)

#: 句末标点（中英文）
_SENTENCE_ENDS = "。！？!?；;\n"


def split_text(text: str, limit: int) -> list[str]:
    """把长文本切成不超过 ``limit`` 个字符的若干段。"""
    text = (text or "").strip()
    if not text:
        return []
    limit = max(1, int(limit))
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = -1

        # 1) 段落优先
        for separator in ("\n\n", "\n"):
            index = window.rfind(separator)
            if index > limit // 3:
                cut = index + len(separator)
                break

        # 2) 退而求其次：句末标点
        if cut <= 0:
            for index in range(len(window) - 1, limit // 3 - 1, -1):
                if window[index] in _SENTENCE_ENDS:
                    cut = index + 1
                    break

        # 3) 实在找不到就在空格处断
        if cut <= 0:
            index = window.rfind(" ")
            if index > limit // 2:
                cut = index + 1

        # 4) 硬切
        if cut <= 0:
            cut = limit

        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].lstrip()

    if remaining.strip():
        chunks.append(remaining.strip())
    return [chunk for chunk in chunks if chunk]


class ReplySender:
    """负责把一段文本变成 QQ 消息。"""

    def __init__(
        self,
        client: OneBotClient,
        *,
        quote_group: bool = True,
        max_chars: int = 900,
    ) -> None:
        self._client = client
        self.quote_group = quote_group
        self.max_chars = max_chars

    # ------------------------------------------------------------------ 发送
    def send_reply(self, event: MessageEvent, text: str) -> int:
        """发送回复，返回实际发出的消息条数。失败时抛 :class:`OneBotError`。"""
        chunks = split_text(text, self.max_chars)
        if not chunks:
            log.warning("模型返回了空回复，跳过发送（session=%s）", event.session_id)
            return 0

        sent = 0
        for index, chunk in enumerate(chunks):
            segments: list[dict[str, Any]] = []
            # 只在第一条上引用原消息：多条时每条都引用会很吵
            if index == 0 and event.is_group and self.quote_group and event.message_id:
                segments.append(build_reply_segment(event.message_id))
            segments.append({"type": "text", "data": {"text": chunk}})
            self._send_segments(event, segments)
            sent += 1
        return sent

    def send_notice(self, event: MessageEvent, text: str) -> bool:
        """发一条简短提示（例如出错告知）。失败不抛异常，只记日志。"""
        try:
            self._send_segments(event, [{"type": "text", "data": {"text": text}}])
            return True
        except OneBotError as exc:
            log.warning("发送提示失败：%s", exc)
            return False

    def _send_segments(self, event: MessageEvent, segments: Sequence[dict[str, Any]]) -> dict[str, Any]:
        if event.is_group:
            return self._client.call(
                "send_group_msg",
                {"group_id": event.group_id, "message": list(segments)},
            )
        return self._client.call(
            "send_private_msg",
            {"user_id": event.user_id, "message": list(segments)},
        )


__all__ = ["ReplySender", "split_text"]
