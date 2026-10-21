"""消息格式：群聊与私聊都包成「昵称 / 时间 / 消息」三行块。

.. code-block:: text

    昵称：毛糙蛋
    时间：2026-9-25 17:00
    消息：早上好啊

**为什么要带昵称**：一个群共用一份记忆（需求规格 §6.1），所有人的话落在同一条
上下文里。不带昵称的话模型看到的是

    user: 你好呀 / assistant: 你好！ / user: 我不吃香菜

它根本分不清这三句分别是谁说的 —— 会把 A 的忌口记到 B 头上。

**为什么要带时间**：模型据此能判断"早上好"是几点的早上好、这句是不是隔了两小时
才回、用户是不是在半夜三点还在聊天。
"""

from __future__ import annotations

import datetime as _dt

import pytest

from conftest import FakeOpenAI, _get, wait_for, wait_memory
from qlark.chat.pipeline import build_labeled_message, clean_sender_name, format_stamp
from qlark.chat.store import memory_key

from test_m2_integration import _inject, _reply_texts, _reset, ready  # noqa: F401

GROUP = "888888"
MEMBER = "333333"

#: 2026-09-25 17:00 本地时间对应的 Unix 时间戳（用本地时区构造，避免时区把断言带偏）
STAMP_TS = _dt.datetime(2026, 9, 25, 17, 0).timestamp()
EXPECTED_STAMP = "2026-9-25 17:00"


def _sent_segments(home) -> list[list[dict]]:
    out: list[list[dict]] = []
    for item in _get(f"http://127.0.0.1:{home.port}/_qlark/sent").get("sent", []):
        message = (item.get("params") or {}).get("message")
        if isinstance(message, list):
            out.append(message)
    return out


def _user_content(payload: dict) -> str:
    for message in payload.get("messages") or []:
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(
                    str(p.get("text") or "") for p in content if p.get("type") == "text"
                )
    return ""


def _send(env, text: str, *, message_id: int, group: bool = True, **kwargs) -> None:
    _reset(env.home)
    payload = {
        "message_type": "group" if group else "private",
        "user_id": MEMBER if group else "20000002",
        "message_id": message_id,
        "text": text,
        "at_bot": True,
        "time_ms": STAMP_TS,
    }
    if group:
        payload["group_id"] = GROUP
    payload.update(kwargs)
    _inject(env.home, **payload)
    assert wait_for(lambda: _reply_texts(env.home), timeout=30), f"没有回复：{text!r}"


def _lines(content: str) -> dict[str, str]:
    """把三行块解析成字典，方便逐项断言。"""
    result: dict[str, str] = {}
    for line in content.splitlines():
        if "：" in line:
            key, _, value = line.partition("：")
            result[key] = value
    return result


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #
def test_format_stamp_has_no_padding_on_month_and_day() -> None:
    """用户要的是 ``2026-9-25`` 而不是 ``2026-09-25``，时和分则补零。"""
    assert format_stamp(_dt.datetime(2026, 9, 25, 17, 0).timestamp()) == "2026-9-25 17:00"
    assert format_stamp(_dt.datetime(2026, 1, 5, 9, 7).timestamp()) == "2026-1-5 09:07"
    assert format_stamp(_dt.datetime(2026, 12, 31, 23, 59).timestamp()) == "2026-12-31 23:59"


def test_format_stamp_handles_bad_input() -> None:
    """时间戳缺失/非法时退回当前时间，绝不抛异常把消息弄丢。"""
    assert format_stamp(None)  # 不抛
    assert format_stamp(0)  # 0 视为"没有"，用当前时间
    assert format_stamp("乱七八糟")
    assert format_stamp(-1)


def test_build_labeled_message_shape() -> None:
    text = build_labeled_message("毛糙蛋", STAMP_TS, "早上好啊")
    assert text.splitlines() == ["昵称：毛糙蛋", f"时间：{EXPECTED_STAMP}", "消息：早上好啊"]


def test_clean_sender_name() -> None:
    assert clean_sender_name("毛糙蛋") == "毛糙蛋"
    assert clean_sender_name("坏\n名字") == "坏 名字", "换行会破坏三行结构"
    assert clean_sender_name("  空格  ") == "空格"
    assert clean_sender_name("：冒号：") == "冒号"
    assert len(clean_sender_name("长" * 100)) == 32, "超长昵称会把每条消息撑大"


# --------------------------------------------------------------------------- #
# 群聊
# --------------------------------------------------------------------------- #
def test_group_message_is_labeled(ready, fake_openai: FakeOpenAI) -> None:
    ready.settings.update(message_label=True, memory_rounds=50, auto_summary=False)
    fake_openai.reset()

    _send(ready, "早上好啊", message_id=3001, nickname="毛糙蛋")

    content = _user_content(fake_openai.requests()[-1])
    fields = _lines(content)
    assert fields.get("昵称") == "毛糙蛋"
    assert fields.get("时间") == EXPECTED_STAMP, f"时间格式不对：{content!r}"
    assert fields.get("消息") == "早上好啊"


def test_card_wins_over_nickname(ready, fake_openai: FakeOpenAI) -> None:
    """群名片优先于昵称 —— 群里大家认的是名片。"""
    ready.settings.update(message_label=True, memory_rounds=50, auto_summary=False)
    fake_openai.reset()

    _send(ready, "在吗", message_id=3002, nickname="毛糙蛋", card="毛糙蛋同学")

    assert _lines(_user_content(fake_openai.requests()[-1])).get("昵称") == "毛糙蛋同学"


def test_group_label_is_stored_in_memory(ready, fake_openai: FakeOpenAI) -> None:
    """历史里也要带昵称和时间，否则模型回看历史照样分不清谁在什么时候说的。"""
    ready.settings.update(message_label=True, memory_rounds=50, auto_summary=False)
    fake_openai.reset()

    _send(ready, "我喜欢喝美式", message_id=3003, nickname="毛糙蛋")

    key = memory_key(f"group:{GROUP}", 1)
    assert wait_memory(ready.bot.pipeline.store, key, min_rows=2), "记忆没落库"
    rows = ready.bot.pipeline.store.memory_messages(key)
    user_row = next(r for r in rows if r["role"] == "user")
    fields = _lines(user_row["content"])
    assert fields.get("昵称") == "毛糙蛋"
    assert fields.get("时间") == EXPECTED_STAMP
    assert fields.get("消息") == "我喜欢喝美式"


def test_assistant_reply_is_not_labeled(ready, fake_openai: FakeOpenAI) -> None:
    """机器人自己的话不加块 —— 它是 assistant 角色，本来就不需要署名。

    注意假模型会把输入原样回显（``收到：<用户消息>``），所以回复里**会**出现
    「昵称：」这几个字。有意义的断言是：回复**本身**不是块，也就是不以它开头。
    """
    ready.settings.update(message_label=True, memory_rounds=50, auto_summary=False)
    fake_openai.reset()

    _send(ready, "你好", message_id=3004)

    key = memory_key(f"group:{GROUP}", 1)
    assert wait_memory(ready.bot.pipeline.store, key, min_rows=2), "记忆没落库"
    rows = ready.bot.pipeline.store.memory_messages(key)
    assistant_row = next(r for r in rows if r["role"] == "assistant")
    assert not assistant_row["content"].startswith("昵称："), assistant_row["content"]


# --------------------------------------------------------------------------- #
# 私聊也要（用户明确要求两边一致）
# --------------------------------------------------------------------------- #
def test_private_message_is_also_labeled(ready, fake_openai: FakeOpenAI) -> None:
    ready.settings.update(message_label=True, memory_rounds=50, auto_summary=False)
    fake_openai.reset()

    _send(ready, "在吗", message_id=3010, group=False, nickname="毛糙蛋")

    fields = _lines(_user_content(fake_openai.requests()[-1]))
    assert fields.get("昵称") == "毛糙蛋"
    assert fields.get("时间") == EXPECTED_STAMP
    assert fields.get("消息") == "在吗"


# --------------------------------------------------------------------------- #
# 开关
# --------------------------------------------------------------------------- #
def test_label_can_be_disabled(ready, fake_openai: FakeOpenAI) -> None:
    ready.settings.update(message_label=False, memory_rounds=50, auto_summary=False)
    fake_openai.reset()

    _send(ready, "早上好啊", message_id=3020, nickname="毛糙蛋")

    assert _user_content(fake_openai.requests()[-1]) == "早上好啊"


def test_default_is_on(ready) -> None:
    from qlark.settings import DEFAULTS

    assert DEFAULTS["message_label"] is True


# --------------------------------------------------------------------------- #
# 图片消息
# --------------------------------------------------------------------------- #
def test_image_message_is_labeled_and_keeps_placeholder(ready, fake_openai: FakeOpenAI) -> None:
    ready.settings.update(
        message_label=True, vision_model="fake-vision", memory_rounds=50, auto_summary=False
    )
    ready.bot.pipeline.invalidate_clients()
    fake_openai.reset()

    _reset(ready.home)
    _inject(
        ready.home,
        message_type="group",
        group_id=GROUP,
        user_id=MEMBER,
        message_id=3030,
        text="看看这张图",
        at_bot=True,
        nickname="毛糙蛋",
        time_ms=STAMP_TS,
        segments=[
            {"type": "at", "data": {"qq": ready.settings.qq_account}},
            {"type": "text", "data": {"text": "看看这张图"}},
            {"type": "image", "data": {"file": "img"}},
        ],
    )
    assert wait_for(lambda: _reply_texts(ready.home), timeout=60)

    content = _user_content(fake_openai.requests()[-1])
    assert content.startswith("昵称：毛糙蛋"), content
    assert "消息：看看这张图" in content

    # 记忆里仍然是 [图片] 占位，绝不能出现 base64
    rows = ready.bot.pipeline.store.memory_messages(memory_key(f"group:{GROUP}", 1))
    user_row = next(r for r in rows if r["role"] == "user")
    assert "[图片]" in user_row["content"]
    assert "base64" not in user_row["content"]


# --------------------------------------------------------------------------- #
# 摘要材料
# --------------------------------------------------------------------------- #
def test_summary_material_flattens_multiline_messages() -> None:
    """摘要材料是"每行一条"，多行块必须压平，否则结构全乱。"""
    from qlark.chat.summarizer import build_material

    material = build_material("", [{"role": "user", "content": build_labeled_message("小明", STAMP_TS, "你好")}])
    body = material.splitlines()
    # 「【新增对话】」之后应当只有一行，而不是被撑成三行
    assert len(body) == 2, body
    assert "昵称：小明" in body[1]
    assert "消息：你好" in body[1]
