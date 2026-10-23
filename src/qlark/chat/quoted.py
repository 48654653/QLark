"""引用消息：把"被引用的那条消息"读出来，让模型看得见。

OneBot 的 ``reply`` 段**只带消息 ID，不带内容**，所以必须回头调
``get_msg`` 把原文取回来。转发聊天记录还要再展开一层（``get_forward_msg``）。

支持的引用内容：
  · 文字
  · 图片（交给多模态，走 ``ImageRef``）
  · 视频 / 语音 / 表情 / 文件（渲染成标记，模型至少知道"那里有个视频"）
  · 转发的聊天记录（展开成 "甲：… / 乙：…" 的行）

.. note::
   所有失败都必须**安静降级**：取不到就按"引用了一条消息"处理，
   绝不能让整条回复卡住或报错 —— 引用是锦上添花，不是必需品。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ..onebot.segments import ImageRef

log = logging.getLogger(__name__)

#: 转发聊天记录最多展开多少条
MAX_FORWARD_NODES = 20
#: 渲染出来的引用文本最长多少字符（太长会把上下文挤掉）
MAX_RENDER_CHARS = 600
#: 一次引用 / 聊天记录最多带多少张图交给视觉模型
#:
#: 一张 512px 约 258 token，三张不到 800 —— 比"看不到图"划算得多。
MAX_QUOTED_IMAGES = 3

#: 这些段直接给个标记就够了
_MARKERS = {
    "video": "[视频]",
    "record": "[语音]",
    "file": "[文件]",
    "face": "[表情]",
    "mface": "[表情]",
    "marketface": "[表情]",
    "json": "[卡片]",
    "xml": "[卡片]",
    "poke": "[戳一戳]",
    "dice": "[骰子]",
    "rps": "[猜拳]",
}


@dataclass
class QuotedContent:
    """引用到的内容。"""

    #: 渲染好的一段可读文本（会拼进用户消息）
    text: str = ""
    #: 引用里的图片，交给多模态
    images: list[ImageRef] = field(default_factory=list)
    #: 出现了哪些内容类型，仅用于日志
    kinds: list[str] = field(default_factory=list)
    #: 有没有真的取到内容
    ok: bool = False


def _flatten(segments: Sequence[Any]) -> list[dict[str, Any]]:
    return [seg for seg in segments if isinstance(seg, dict)]


def render_segments(
    segments: Sequence[Any],
    *,
    max_chars: int = MAX_RENDER_CHARS,
    max_images: int = MAX_QUOTED_IMAGES,
) -> QuotedContent:
    """把消息段数组渲染成一段可读文本 + 图片引用。**纯函数，好测。**"""
    content = QuotedContent()
    parts: list[str] = []
    for segment in _flatten(segments):
        seg_type = str(segment.get("type") or "")
        data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        kind = seg_type or "unknown"
        if kind not in content.kinds:
            content.kinds.append(kind)

        if seg_type == "text":
            text = str(data.get("text") or "").strip()
            if text:
                parts.append(text)
        elif seg_type == "image":
            if len(content.images) < max_images:
                content.images.append(
                    ImageRef(
                        file=data.get("file"),
                        url=data.get("url"),
                        sub_type=data.get("sub_type") or data.get("subType"),
                        summary=data.get("summary"),
                    )
                )
            parts.append("[图片]")
        elif seg_type in _MARKERS:
            parts.append(_MARKERS[seg_type])
        elif seg_type in {"reply", "source"}:
            # 嵌套的引用不再展开，避免套娃
            continue
        else:
            parts.append(f"[{kind}]")

    text = " ".join(part for part in parts if part).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    content.text = text
    content.ok = bool(text or content.images)
    return content


def render_forward(nodes: Sequence[Any], *, max_nodes: int = MAX_FORWARD_NODES) -> QuotedContent:
    """把转发聊天记录渲染成 "甲：… / 乙：…" 的形式。"""
    content = QuotedContent()
    lines: list[str] = []
    for node in list(nodes)[:max_nodes]:
        if not isinstance(node, dict):
            continue
        sender = node.get("sender") if isinstance(node.get("sender"), dict) else {}
        name = str(
            sender.get("card") or sender.get("nickname") or sender.get("user_id") or ""
        ).strip()
        message = node.get("message")
        if isinstance(message, dict):
            message = [message]
        inner = render_segments(message or [])
        content.images.extend(inner.images[: max(0, MAX_QUOTED_IMAGES - len(content.images))])
        body = inner.text or "（空）"
        lines.append(f"{name}：{body}" if name else body)

    total = len(list(nodes))
    if total > max_nodes:
        lines.append(f"…共 {total} 条，只展开了前 {max_nodes} 条")
    content.text = " / ".join(lines)[:MAX_RENDER_CHARS]
    content.kinds = ["forward"]
    content.ok = bool(content.text)
    return content


class QuotedFetcher:
    """按消息 ID 取回被引用的内容。"""

    def __init__(
        self,
        client_factory: Callable[[], Any],
        *,
        max_chars: int = MAX_RENDER_CHARS,
    ) -> None:
        self._client_factory = client_factory
        self._max_chars = max_chars

    def fetch(self, message_id: str) -> QuotedContent:
        """取回一条消息并渲染。取不到就返回 ``ok=False``，**不抛异常**。"""
        if not str(message_id or "").strip():
            return QuotedContent()

        try:
            response = self._client_factory().call(
                "get_msg", {"message_id": str(message_id)}
            )
        except Exception as exc:  # noqa: BLE001 引用取不到不能影响回复
            log.info("取引用消息失败（id=%s）：%s", message_id, exc)
            return QuotedContent()

        data = self._payload(response)
        if not data:
            return QuotedContent()

        segments = data.get("message")
        if isinstance(segments, dict):
            segments = [segments]
        if not isinstance(segments, list):
            segments = []

        # 转发聊天记录：段里只有 id，得再展开一层
        for segment in _flatten(segments):
            if str(segment.get("type") or "") not in {"forward", "node"}:
                continue
            inner = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            nodes = self._expand_forward(inner.get("id") or inner.get("content"))
            if nodes:
                return render_forward(nodes)

        return render_segments(segments, max_chars=self._max_chars)

    def fetch_forward(self, data: dict[str, Any] | None) -> QuotedContent:
        """展开**单独发过来的**合并转发（聊天记录）。

        和"引用一条消息"不同：这里拿到的就是那个 ``forward`` 段本身，
        ``data`` 里可能是内联的 ``content``（NapCat 常见），也可能只有一个
        ``id``，得再调 ``get_forward_msg``。两种都处理，取不到就返回空。

        这段内容**不进记忆** —— 只有 ``[合并转发]`` 这个占位符入库。
        """
        if not isinstance(data, dict):
            return QuotedContent()
        nodes = self._expand_forward(data.get("id") or data.get("content"))
        if not nodes:
            return QuotedContent()
        return render_forward(nodes)

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _payload(response: Any) -> dict[str, Any]:
        """从 OneBot 响应里挖出 ``data``。"""
        if not isinstance(response, dict):
            return {}
        data = response.get("data")
        if isinstance(data, dict):
            return data
        # 有些实现把消息直接摊在顶层
        return response if "message" in response else {}

    def _expand_forward(self, ref: Any) -> list[Any]:
        """展开转发的聊天记录。两种参数名都试一遍（各家实现不一致）。"""
        if ref is None:
            return []
        if isinstance(ref, list):          # 已经内联展开了
            return list(ref)
        client = self._client_factory()
        for action, params in (
            ("get_forward_msg", {"message_id": str(ref)}),
            ("get_forward_msg", {"id": str(ref)}),
        ):
            try:
                response = client.call(action, params)
            except Exception as exc:  # noqa: BLE001
                log.info("展开转发消息失败（%s=%s）：%s", list(params)[0], ref, exc)
                continue
            data = self._payload(response)
            nodes = data.get("messages")
            if isinstance(nodes, list) and nodes:
                return nodes
        return []


__all__ = [
    "MAX_FORWARD_NODES",
    "MAX_QUOTED_IMAGES",
    "MAX_RENDER_CHARS",
    "QuotedContent",
    "QuotedFetcher",
    "render_forward",
    "render_segments",
]
