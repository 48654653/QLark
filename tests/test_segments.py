"""消息段解析与降级映射表的测试。

**M2 的验收标准之一就是这张表**：语音消息到达时是 ``record`` 段，
必须由我们自己转成 ``[语音消息]`` 文本（``[语音消息]`` 是 QQ 客户端的显示文本，
NapCat 不会给我们这个字符串）。
"""

from __future__ import annotations

import pytest

from qlark.onebot.segments import build_reply_segment, build_text_segments, parse_segments

BOT = "10000001"


def test_text_segments_are_joined() -> None:
    parsed = parse_segments(
        [
            {"type": "text", "data": {"text": "你好"}},
            {"type": "text", "data": {"text": "，世界"}},
        ]
    )
    assert parsed.text == "你好，世界"
    assert parsed.is_empty is False


def test_trigger_at_is_excluded_from_text_but_recorded() -> None:
    parsed = parse_segments(
        [
            {"type": "at", "data": {"qq": BOT}},
            {"type": "text", "data": {"text": " 在吗"}},
        ],
        self_id=BOT,
    )
    assert parsed.mentions_bot is True
    assert parsed.ats == [BOT]
    assert parsed.text == "在吗", "触发用的 @ 不该留在正文里"


def test_other_at_keeps_a_visible_marker() -> None:
    parsed = parse_segments(
        [
            {"type": "at", "data": {"qq": "10001"}},
            {"type": "text", "data": {"text": " 看一下"}},
        ],
        self_id=BOT,
    )
    assert parsed.mentions_bot is False
    assert parsed.ats == ["10001"]
    assert parsed.text == "@10001 看一下"


def test_at_all() -> None:
    parsed = parse_segments([{"type": "at", "data": {"qq": "all"}}], self_id=BOT)
    assert parsed.ats == ["all"]
    assert parsed.text == "@全体成员"
    assert parsed.mentions_bot is False


def test_at_user_id_alias() -> None:
    """有些实现用 ``user_id`` 而不是 ``qq``。"""
    parsed = parse_segments([{"type": "at", "data": {"user_id": BOT}}], self_id=BOT)
    assert parsed.mentions_bot is True


@pytest.mark.parametrize(
    ("segment", "expected"),
    [
        ({"type": "record", "data": {"file": "x.silk"}}, "[语音消息]"),
        ({"type": "video", "data": {"file": "x.mp4"}}, "[视频]"),
        ({"type": "face", "data": {"id": "1"}}, "[表情]"),
        ({"type": "mface", "data": {"id": "2"}}, "[表情]"),
        ({"type": "bface", "data": {"id": "3"}}, "[表情]"),
        ({"type": "json", "data": {"data": "{}"}}, "[卡片消息]"),
        ({"type": "xml", "data": {"data": "<a/>"}}, "[卡片消息]"),
        ({"type": "forward", "data": {"id": "1"}}, "[合并转发]"),
        ({"type": "poke", "data": {"id": "1"}}, "[戳一戳]"),
    ],
)
def test_placeholder_mapping(segment: dict, expected: str) -> None:
    parsed = parse_segments([segment])
    assert parsed.text == expected


def test_file_segment_includes_name() -> None:
    parsed = parse_segments([{"type": "file", "data": {"name": "报告.pdf"}}])
    assert parsed.text == "[文件：报告.pdf]"

    parsed = parse_segments([{"type": "file", "data": {}}])
    assert parsed.text == "[文件]"


def test_image_segment_is_recorded_and_placeholder_added() -> None:
    parsed = parse_segments(
        [
            {"type": "image", "data": {"file": "a.jpg", "url": "http://x/a.jpg"}},
            {"type": "text", "data": {"text": "看看这个"}},
        ]
    )
    assert parsed.has_image is True
    assert len(parsed.images) == 1
    assert parsed.images[0].url == "http://x/a.jpg"
    assert parsed.text == "[图片]看看这个"
    # 落库时绝不能把 url / base64 写进记忆
    assert parsed.text_for_memory() == "[图片]看看这个"


def test_reply_segment_does_not_pollute_text() -> None:
    parsed = parse_segments(
        [
            {"type": "reply", "data": {"id": "777"}},
            {"type": "text", "data": {"text": "接着说"}},
        ]
    )
    assert parsed.reply_id == "777"
    assert parsed.text == "接着说"


def test_unknown_type_degrades_with_marker() -> None:
    parsed = parse_segments([{"type": "brand_new_thing", "data": {}}])
    assert parsed.text == "[不支持的消息类型]"
    assert parsed.unknown_types == ["brand_new_thing"]


def test_entity_escapes_are_decoded() -> None:
    parsed = parse_segments([{"type": "text", "data": {"text": "&#91;测试&#93;"}}])
    assert parsed.text == "[测试]"


def test_empty_and_malformed_segments() -> None:
    assert parse_segments(None).is_empty is True
    assert parse_segments([]).is_empty is True
    # 不能因为一条怪段就整条崩掉
    parsed = parse_segments(["不是字典", {}, {"type": "text", "data": {"text": "ok"}}])
    assert parsed.text == "ok"


def test_mixed_message_memory_text() -> None:
    parsed = parse_segments(
        [
            {"type": "at", "data": {"qq": BOT}},
            {"type": "text", "data": {"text": " "}},
            {"type": "image", "data": {"file": "a.jpg"}},
            {"type": "record", "data": {"file": "b.silk"}},
            {"type": "text", "data": {"text": " 你看"}},
        ],
        self_id=BOT,
    )
    assert parsed.text_for_memory() == "[图片][语音消息] 你看"


def test_builders() -> None:
    assert build_text_segments("hi") == [{"type": "text", "data": {"text": "hi"}}]
    assert build_text_segments("") == []
    assert build_reply_segment(123) == {"type": "reply", "data": {"id": "123"}}
