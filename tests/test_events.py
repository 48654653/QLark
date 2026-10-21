"""OneBot 事件解析测试。"""

from __future__ import annotations

from qlark.onebot.events import describe_event, make_session_id, parse_message_event

BOT = "10000001"


def group_payload(**overrides) -> dict:
    payload = {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": 1001,
        "group_id": 123456,
        "user_id": 999,
        "self_id": int(BOT),
        "message": [{"type": "text", "data": {"text": "你好"}}],
        "raw_message": "你好",
        "time": 1712345678,
        "sender": {"user_id": 999, "nickname": "小明", "card": "小明同学", "role": "admin"},
    }
    payload.update(overrides)
    return payload


def test_parse_group_message() -> None:
    event = parse_message_event(group_payload())
    assert event is not None
    assert event.is_group is True
    assert event.message_id == "1001"
    assert event.group_id == "123456"
    assert event.user_id == "999"
    assert event.self_id == BOT
    assert event.session_id == "group:123456"
    assert event.sender_name == "小明同学", "优先用群名片"
    assert event.sender_role == "admin"
    assert event.is_from_self is False


def test_parse_private_message() -> None:
    event = parse_message_event(
        group_payload(message_type="private", group_id=None, sub_type="friend")
    )
    assert event is not None
    assert event.is_group is False
    assert event.group_id is None
    assert event.session_id == "private:999"


def test_self_message_is_flagged() -> None:
    event = parse_message_event(group_payload(user_id=int(BOT)))
    assert event is not None
    assert event.is_from_self is True


def test_sender_name_falls_back_to_nickname_then_qq() -> None:
    event = parse_message_event(group_payload(sender={"user_id": 999, "nickname": "小明"}))
    assert event is not None and event.sender_name == "小明"

    event = parse_message_event(group_payload(sender={"user_id": 999}))
    assert event is not None and event.sender_name == "999"


def test_non_message_events_are_ignored() -> None:
    assert parse_message_event({"post_type": "notice", "notice_type": "group_recall"}) is None
    assert parse_message_event({"post_type": "meta_event", "meta_event_type": "lifecycle"}) is None
    assert parse_message_event({"post_type": "message", "message_type": "guild"}) is None
    assert parse_message_event(None) is None
    assert parse_message_event("不是字典") is None


def test_self_id_falls_back_to_argument() -> None:
    payload = group_payload()
    payload.pop("self_id")
    event = parse_message_event(payload, self_id=BOT)
    assert event is not None and event.self_id == BOT


def test_string_message_is_tolerated() -> None:
    """理论上不会发生（我们配的是 array），但不能因为一条怪消息打断链路。"""
    event = parse_message_event(group_payload(message="纯文本"))
    assert event is not None
    assert event.segments == [{"type": "text", "data": {"text": "纯文本"}}]


def test_malformed_message_becomes_empty_segments() -> None:
    event = parse_message_event(group_payload(message=None))
    assert event is not None and event.segments == []


def test_make_session_id() -> None:
    assert make_session_id("group", group_id="1", user_id="2") == "group:1"
    assert make_session_id("private", user_id="2") == "private:2"


def test_describe_event() -> None:
    assert "消息事件 group" in describe_event(group_payload())
    assert describe_event({"post_type": "notice", "notice_type": "x"}) == "通知事件 x"
    assert describe_event({"post_type": "meta_event", "meta_event_type": "y"}) == "元事件 y"
    assert describe_event({}) == "事件 ?"
