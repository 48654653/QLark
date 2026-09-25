"""群聊行为：发送者名字前缀 + 不引用原消息。

**为什么要带名字**：一个群共用一份记忆（需求规格 §6.1），所有人的话落在同一条
上下文里。不带名字的话模型看到的是

    user: 你好呀 / assistant: 你好！ / user: 我不吃香菜

它根本分不清这三句分别是谁说的 —— 会把 A 的忌口记到 B 头上。
"""

from __future__ import annotations

import pytest

from conftest import FakeOpenAI, _get, wait_for
from qlark.chat.store import memory_key

from test_m2_integration import _inject, _reply_texts, _reset, ready  # noqa: F401

GROUP = "888888"
MEMBER = "333333"


def _sent_segments(home) -> list[list[dict]]:
    out: list[list[dict]] = []
    for item in _get(f"http://127.0.0.1:{home.port}/_qlark/sent").get("sent", []):
        message = (item.get("params") or {}).get("message")
        if isinstance(message, list):
            out.append(message)
    return out


def _send_group(env, text: str, *, message_id: int, nickname: str = "小明", card: str = "") -> None:
    _reset(env.home)
    _inject(
        env.home,
        message_type="group",
        group_id=GROUP,
        user_id=MEMBER,
        message_id=message_id,
        text=text,
        at_bot=True,
        nickname=nickname,
        card=card,
    )
    assert wait_for(lambda: _reply_texts(env.home), timeout=30), f"没有回复：{text!r}"


def _user_content(payload: dict) -> str:
    for message in payload.get("messages") or []:
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(str(p.get("text") or "") for p in content if p.get("type") == "text")
    return ""


# --------------------------------------------------------------------------- #
# 发送者名字
# --------------------------------------------------------------------------- #
def test_group_message_carries_sender_name(ready, fake_openai: FakeOpenAI) -> None:
    ready.settings.update(group_prefix_sender=True, memory_rounds=50, auto_summary=False)
    fake_openai.reset()

    _send_group(ready, "你好呀", message_id=2001, nickname="小明")

    content = _user_content(fake_openai.requests()[-1])
    assert "小明" in content, f"发给模型的消息里没有发送者：{content!r}"
    assert content.startswith("小明："), f"名字应当在开头：{content!r}"
    assert "你好呀" in content


def test_group_card_wins_over_nickname(ready, fake_openai: FakeOpenAI) -> None:
    """群名片优先于昵称 —— 群里大家认的是名片。"""
    ready.settings.update(group_prefix_sender=True, memory_rounds=50, auto_summary=False)
    fake_openai.reset()

    _send_group(ready, "在吗", message_id=2002, nickname="小明", card="小明同学")

    assert _user_content(fake_openai.requests()[-1]).startswith("小明同学：")


def test_sender_name_is_stored_in_memory(ready, fake_openai: FakeOpenAI) -> None:
    """历史里也要带名字，否则模型回看历史照样分不清谁说的。"""
    ready.settings.update(group_prefix_sender=True, memory_rounds=50, auto_summary=False)
    fake_openai.reset()

    _send_group(ready, "我喜欢喝美式", message_id=2003, nickname="小明")

    key = memory_key(f"group:{GROUP}", 1)
    rows = ready.bot.pipeline.store.memory_messages(key)
    user_row = next(r for r in rows if r["role"] == "user")
    assert user_row["content"].startswith("小明："), user_row["content"]
    assert "我喜欢喝美式" in user_row["content"]


def test_private_chat_has_no_prefix(ready, fake_openai: FakeOpenAI) -> None:
    """私聊只有一个人，加名字纯属浪费 token。"""
    ready.settings.update(group_prefix_sender=True, memory_rounds=50, auto_summary=False)
    fake_openai.reset()

    _reset(ready.home)
    _inject(ready.home, message_type="private", user_id="2658495997", message_id=2010, text="你好")
    assert wait_for(lambda: _reply_texts(ready.home), timeout=30)

    content = _user_content(fake_openai.requests()[-1])
    assert content == "你好", f"私聊不该加前缀：{content!r}"


def test_prefix_can_be_disabled(ready, fake_openai: FakeOpenAI) -> None:
    ready.settings.update(group_prefix_sender=False, memory_rounds=50, auto_summary=False)
    fake_openai.reset()

    _send_group(ready, "你好呀", message_id=2004, nickname="小明")

    content = _user_content(fake_openai.requests()[-1])
    assert content == "你好呀", f"关掉后不该加前缀：{content!r}"


def test_weird_sender_name_is_cleaned(ready, fake_openai: FakeOpenAI) -> None:
    """名字里的换行会破坏"名字：内容"的结构，超长昵称会把每条消息撑大。"""
    ready.settings.update(group_prefix_sender=True, memory_rounds=50, auto_summary=False)
    fake_openai.reset()

    _send_group(ready, "看看", message_id=2005, nickname="坏\n名字" + "长" * 80)

    content = _user_content(fake_openai.requests()[-1])
    prefix = content.split("：", 1)[0]
    assert "\n" not in prefix, f"前缀里不该有换行：{prefix!r}"
    assert len(prefix) <= 32, f"前缀过长：{len(prefix)} 字符"


def test_image_message_also_carries_sender_name(ready, fake_openai: FakeOpenAI) -> None:
    """带图的消息同样要知道是谁发的。"""
    ready.settings.update(
        group_prefix_sender=True, vision_model="fake-vision", memory_rounds=50, auto_summary=False
    )
    ready.bot.pipeline.invalidate_clients()
    fake_openai.reset()

    _reset(ready.home)
    _inject(
        ready.home,
        message_type="group",
        group_id=GROUP,
        user_id=MEMBER,
        message_id=2006,
        text="看看这张图",
        at_bot=True,
        nickname="小明",
        segments=[
            {"type": "at", "data": {"qq": ready.settings.qq_account}},
            {"type": "text", "data": {"text": "看看这张图"}},
            {"type": "image", "data": {"file": "img"}},
        ],
    )
    assert wait_for(lambda: _reply_texts(ready.home), timeout=60)

    content = _user_content(fake_openai.requests()[-1])
    assert content.startswith("小明："), f"带图消息也要带名字：{content!r}"


# --------------------------------------------------------------------------- #
# 不引用原消息
# --------------------------------------------------------------------------- #
def test_group_reply_does_not_quote_by_default(ready, fake_openai: FakeOpenAI) -> None:
    """群聊默认只回复、不引用 —— 引用会让群里看着很吵。"""
    from qlark.settings import DEFAULTS

    assert DEFAULTS["reply_quote_group"] is False, "出厂默认就该是不引用"

    ready.settings.update(reply_quote_group=False, memory_rounds=50, auto_summary=False)
    fake_openai.reset()
    _send_group(ready, "测试一下", message_id=2007)

    kinds = [seg.get("type") for group in _sent_segments(ready.home) for seg in group]
    assert "reply" not in kinds, f"不该带引用段：{kinds}"
    assert "text" in kinds


def test_group_quote_can_be_turned_back_on(ready, fake_openai: FakeOpenAI) -> None:
    """想引用的人还是能打开。"""
    ready.settings.update(reply_quote_group=True, memory_rounds=50, auto_summary=False)
    fake_openai.reset()
    _send_group(ready, "测试一下", message_id=2008)

    kinds = [seg.get("type") for group in _sent_segments(ready.home) for seg in group]
    assert "reply" in kinds, f"打开后应当带引用段：{kinds}"


def test_private_reply_never_quotes(ready, fake_openai: FakeOpenAI) -> None:
    """引用本来只作用于群聊，私聊永远不用。"""
    ready.settings.update(reply_quote_group=True, memory_rounds=50, auto_summary=False)
    fake_openai.reset()

    _reset(ready.home)
    _inject(ready.home, message_type="private", user_id="2658495997", message_id=2009, text="你好")
    assert wait_for(lambda: _reply_texts(ready.home), timeout=30)

    kinds = [seg.get("type") for group in _sent_segments(ready.home) for seg in group]
    assert "reply" not in kinds
