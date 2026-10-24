"""OneBot 11 消息段的解析与降级。

**为什么需要这个模块**：我们在 NapCat 里配的是 ``messagePostFormat: "array"``，
所以一条消息到达时是**消息段对象数组**：

.. code-block:: json

    {"type": "record", "data": {"file": "...", "url": "..."}}

而 ``[语音消息]`` 是 **QQ 客户端界面上的显示文本**，NapCat 并不会把这个字符串给我们。
（若配成 ``string`` 格式，拿到的是 ``[CQ:record,file=...]``，那是 CQ 码，同样不是显示文本。）

所以"把不支持的消息类型降级成可读占位文本"这件事必须由 QLark 自己做。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

#: 图片类段（M5 会把这些真正发给视觉模型）
IMAGE_TYPES = frozenset({"image"})
#: 视频类段（会以 ``video_url`` 段交给支持视频的模型）
VIDEO_TYPES = frozenset({"video"})
#: 表情类段
FACE_TYPES = frozenset({"face", "mface", "bface", "sface"})
#: 合并转发类段
FORWARD_TYPES = frozenset({"forward", "node"})
#: 卡片类段
CARD_TYPES = frozenset({"json", "xml", "markdown"})

#: 直接映射的占位文本
PLACEHOLDER_TEXT: dict[str, str] = {
    "record": "[语音消息]",
    "video": "[视频]",
    "poke": "[戳一戳]",
    "shake": "[窗口抖动]",
    "dice": "[骰子]",
    "rps": "[猜拳]",
    "music": "[音乐分享]",
    "location": "[位置]",
    "contact": "[推荐联系人]",
}

#: 完全不出现在正文里的段
SILENT_TYPES = frozenset({"reply", "source", "rps_result"})

#: 未知段的兜底文案
UNKNOWN_PLACEHOLDER = "[不支持的消息类型]"


@dataclass(frozen=True)
class ImageRef:
    """一条图片消息的引用。

    ``url`` 约 **2 小时过期**（技术文档坑 17），所以 M5 拿到之后必须**立即下载**，
    不能存下来事后再取。
    """

    file: str | None = None
    url: str | None = None
    sub_type: str | None = None
    summary: str | None = None


@dataclass(frozen=True)
class VideoRef:
    """一条视频消息的引用。

    NapCat 的 ``video`` 段带 ``url``（**约 2 小时过期的直链**）和 ``file``。
    模型侧能直接吃 ``{"type": "video_url", "video_url": {"url": ...}}``，
    所以这个 url 会原样交给模型 —— 不需要 QLark 自己下载解码。
    """

    url: str | None = None
    file: str | None = None
    #: 字节数（NapCat 会以字符串给出，解析时转成 int；未知则 0）
    size: int = 0

    def to_segment(self) -> dict[str, Any]:
        return {"type": "video_url", "video_url": {"url": str(self.url or "")}}


@dataclass
class ParsedMessage:
    """把消息段数组解析成"可以直接喂给模型/落库"的形态。"""

    #: 降级后的纯文本（含 ``[图片]`` ``[语音消息]`` 这类占位符）
    text: str = ""
    images: list[ImageRef] = field(default_factory=list)
    #: 消息里带的视频（通常只有一条；多条时流水线只取第一条）
    videos: list[VideoRef] = field(default_factory=list)
    #: 消息里 @ 到的 QQ 号（``all`` 表示 @全体成员）
    ats: list[str] = field(default_factory=list)
    #: 是否 @ 了机器人自己
    mentions_bot: bool = False
    #: 引用的消息 ID
    reply_id: str | None = None
    #: 消息里带的合并转发（聊天记录）。
    #:
    #: 每项是那个 ``forward`` 段的原始 ``data`` —— 里面可能有内联的 ``content``，
    #: 也可能只有一个 ``id``（要靠 ``get_forward_msg`` 再取一层）。
    #: **内容不在这里展开**：展开要调接口，解析函数只管认出来。
    forwards: list[dict[str, Any]] = field(default_factory=list)
    #: 出现过的未知段类型（便于以后补映射）
    unknown_types: list[str] = field(default_factory=list)

    @property
    def has_image(self) -> bool:
        return bool(self.images)

    @property
    def has_video(self) -> bool:
        return bool(self.videos)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip() and not self.images and not self.videos

    def text_for_memory(self) -> str:
        """落库用的文本：图片统一压成 ``[图片]`` 占位，绝不把 base64 写进记忆。"""
        return self.text.strip()


def _data(segment: Any) -> dict[str, Any]:
    if not isinstance(segment, dict):
        return {}
    data = segment.get("data")
    return data if isinstance(data, dict) else {}


def _segment_type(segment: Any) -> str:
    if not isinstance(segment, dict):
        return ""
    return str(segment.get("type") or "")


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    # OneBot 有时会把实体转义写进 text 段
    return (
        text.replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&#44;", ",")
        .replace("&amp;", "&")
    )


def _as_int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def parse_segments(
    segments: Iterable[Any] | None,
    *,
    self_id: str | None = None,
) -> ParsedMessage:
    """把消息段数组解析成 :class:`ParsedMessage`。

    Args:
        segments: OneBot 的 ``message`` 数组。
        self_id: 机器人自己的 QQ 号。用于识别"触发用的那个 @"——它不计入正文，
            但仍然会被记录在 ``ats`` 里并把 ``mentions_bot`` 置为 ``True``。
    """
    result = ParsedMessage()
    if not segments:
        return result

    parts: list[str] = []
    self_id = str(self_id) if self_id else None

    for segment in segments:
        seg_type = _segment_type(segment)
        data = _data(segment)

        if not seg_type:
            continue

        # ---- 纯文本 ----
        if seg_type == "text":
            parts.append(_clean_text(data.get("text")))
            continue

        # ---- @ ----
        if seg_type == "at":
            target = str(data.get("qq") or data.get("user_id") or "")
            if not target:
                continue
            result.ats.append(target)
            if target == "all":
                parts.append("@全体成员")
                continue
            if self_id and target == self_id:
                # 触发用的 @，不写进正文（否则模型会看到一堆 @自己）
                result.mentions_bot = True
                continue
            parts.append(f"@{target}")
            continue

        # ---- 图片 ----
        if seg_type in IMAGE_TYPES:
            result.images.append(
                ImageRef(
                    file=data.get("file"),
                    url=data.get("url"),
                    sub_type=data.get("sub_type") or data.get("subType"),
                    summary=data.get("summary"),
                )
            )
            parts.append("[图片]")
            continue

        # ---- 表情 ----
        if seg_type in FACE_TYPES:
            parts.append("[表情]")
            continue

        # ---- 视频 ----
        #
        # **不下载、不解码**：把直链留着，交给模型的 ``video_url`` 段。
        # 占位符 ``[视频]`` 仍然写进正文 —— 落库的记忆里只该有它；
        # 只有真正发给模型的那一次才会把占位符换成视频本体。
        if seg_type in VIDEO_TYPES:
            url = str(data.get("url") or "").strip() or None
            result.videos.append(
                VideoRef(url=url, file=str(data.get("file") or "").strip() or None,
                         size=_as_int(data.get("file_size")))
            )
            parts.append(PLACEHOLDER_TEXT["video"])
            continue

        # ---- 合并转发 ----
        #
        # 这里**只记下原始 data，不在这里取内容** —— 展开聊天记录要调 OneBot 接口，
        # 属于网络操作，不能放在纯解析函数里。
        # 正文占位符保留 `[合并转发]`：落库的记忆里存这个就够了，
        # 真实内容由流水线展开后拼进"发给模型的这一轮"，不进记忆。
        if seg_type in FORWARD_TYPES:
            result.forwards.append(dict(data))
            parts.append("[合并转发]")
            continue

        # ---- 卡片 ----
        if seg_type in CARD_TYPES:
            parts.append("[卡片消息]")
            continue

        # ---- 文件 ----
        if seg_type == "file":
            name = str(data.get("name") or data.get("file") or "").strip()
            parts.append(f"[文件：{name}]" if name else "[文件]")
            continue

        # ---- 引用 / 来源：不占正文 ----
        if seg_type == "reply":
            result.reply_id = str(data.get("id") or "") or None
            continue
        if seg_type in SILENT_TYPES:
            continue

        # ---- 直接映射的占位文本 ----
        placeholder = PLACEHOLDER_TEXT.get(seg_type)
        if placeholder:
            parts.append(placeholder)
            continue

        # ---- 未知类型 ----
        if seg_type not in result.unknown_types:
            result.unknown_types.append(seg_type)
        parts.append(UNKNOWN_PLACEHOLDER)

    # 段之间不额外加空格：文本段本身就带空格
    result.text = "".join(parts).strip()
    return result


def build_text_segments(text: str) -> list[dict[str, Any]]:
    """把纯文本包成消息段数组（QQ 不接受空文本，空串返回空数组）。"""
    return [{"type": "text", "data": {"text": text}}] if text else []


def build_reply_segment(message_id: str | int) -> dict[str, Any]:
    """构造引用段。"""
    return {"type": "reply", "data": {"id": str(message_id)}}


def build_record_segment(source: str | Path) -> dict[str, Any]:
    """构造语音段（QQ 语音消息）。

    **本地文件一律用 base64 内联**，不用 ``file://`` —— 语音文件名常带中文，
    走路径要过 ``decodeURIComponent``，base64 没这个问题（发图片时踩过）。

    .. important::
       **不需要自己转 silk。** NapCat 的发送链路会自动判断：不是 silk 就调
       ``convertToNTSilkTct`` 转一遍再发（日志会打「语音文件X需要转换成silk」）。
       所以这里直接给 mp3/wav 就行。
    """
    path = Path(source).expanduser()
    raw = path.read_bytes()
    if not raw:
        raise ValueError("语音文件为空")
    encoded = base64.b64encode(raw).decode("ascii")
    return {"type": "record", "data": {"file": f"base64://{encoded}"}}


def build_image_segment(source: str | Path) -> dict[str, Any]:
    """构造图片段。

    本地文件必须写成 URI 形式（``file:///E:/...``）—— OneBot 的 ``file`` 字段
    在给本地路径时就是这个约定。已经是 URI / data URL 的原样透传。

    .. note::
       **本地路径能不能真的发出去，必须在真机上验证**（NapCat 4.18.28）。
       如果不行，退路是改用 base64 的 data URL，或先走 NapCat 的上传接口拿标识。
    """
    text = str(source or "").strip()
    if not text:
        raise ValueError("图片地址为空")
    if not text.startswith(("file://", "http://", "https://", "base64://", "data:")):
        text = Path(text).expanduser().resolve().as_uri()
    return {"type": "image", "data": {"file": text}}


__all__ = [
    "FACE_TYPES",
    "FORWARD_TYPES",
    "IMAGE_TYPES",
    "ImageRef",
    "PLACEHOLDER_TEXT",
    "ParsedMessage",
    "UNKNOWN_PLACEHOLDER",
    "VIDEO_TYPES",
    "VideoRef",
    "build_image_segment",
    "build_record_segment",
    "build_reply_segment",
    "build_text_segments",
    "parse_segments",
]
