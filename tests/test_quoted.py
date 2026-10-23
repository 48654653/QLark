"""引用消息：机器人要能看到被引用的那条消息。

OneBot 的 ``reply`` 段**只带消息 ID，不带内容**，所以必须回头调 ``get_msg``。
转发聊天记录还要再展开一层（``get_forward_msg``）。

必须覆盖的内容类型：文字、图片、视频、语音、转发的聊天记录。
失败要安静降级 —— 对方可能已经把原消息删了，不能因此把回复卡死。
"""

from __future__ import annotations

import pytest

from conftest import FakeOpenAI, wait_for
from qlark.chat.quoted import (
    QuotedFetcher,
    render_forward,
    render_segments,
)

from test_m2_integration import _inject, _reset, ready  # noqa: F401

GROUP = "888888"


# --------------------------------------------------------------------------- #
# 渲染（纯函数）
# --------------------------------------------------------------------------- #
def test_render_text() -> None:
    content = render_segments([{"type": "text", "data": {"text": "今晚吃什么"}}])
    assert content.ok
    assert content.text == "今晚吃什么"


def test_render_image_keeps_a_ref_and_a_marker() -> None:
    content = render_segments([
        {"type": "text", "data": {"text": "看这个"}},
        {"type": "image", "data": {"file": "a.jpg", "url": "https://x/a.jpg"}},
    ])
    assert content.text == "看这个 [图片]"
    assert len(content.images) == 1
    assert content.images[0].url == "https://x/a.jpg"


def test_render_video_and_voice_and_card() -> None:
    content = render_segments([
        {"type": "video", "data": {"file": "v.mp4"}},
        {"type": "record", "data": {"file": "r.amr"}},
        {"type": "json", "data": {"data": "{}"}},
    ])
    assert "视频" in content.text
    assert "语音" in content.text
    assert "卡片" in content.text


def test_render_nested_reply_is_skipped() -> None:
    """引用里的引用不再展开，避免套娃。"""
    content = render_segments([
        {"type": "reply", "data": {"id": "999"}},
        {"type": "text", "data": {"text": "就这样"}},
    ])
    assert content.text == "就这样"


def test_render_caps_length() -> None:
    content = render_segments(
        [{"type": "text", "data": {"text": "长" * 2000}}], max_chars=50
    )
    assert len(content.text) <= 50
    assert content.text.endswith("…")


def test_forward_becomes_readable_lines() -> None:
    nodes = [
        {"sender": {"nickname": "小明"}, "message": [{"type": "text", "data": {"text": "在吗"}}]},
        {"sender": {"card": "老师"}, "message": [{"type": "text", "data": {"text": "作业交了吗"}}]},
    ]
    content = render_forward(nodes)
    assert content.ok
    assert "小明：在吗" in content.text
    assert "老师：作业交了吗" in content.text


def test_forward_truncates_and_says_so() -> None:
    nodes = [
        {"sender": {"nickname": f"u{i}"}, "message": [{"type": "text", "data": {"text": "x"}}]}
        for i in range(30)
    ]
    content = render_forward(nodes, max_nodes=5)
    assert "只展开了前 5 条" in content.text


# --------------------------------------------------------------------------- #
# 取回（假客户端）
# --------------------------------------------------------------------------- #
class _FakeClient:
    def __init__(self, responses: dict[str, dict]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def call(self, action: str, params: dict | None = None) -> dict:
        self.calls.append((action, dict(params or {})))
        key = action
        if key not in self.responses:
            raise RuntimeError(f"假服务没有这个动作：{action}")
        return self.responses[key]


def _payload(segments) -> dict:
    return {"status": "ok", "data": {"message": segments, "sender": {"nickname": "小明"}}}


def test_fetch_reads_get_msg() -> None:
    client = _FakeClient({
        "get_msg": _payload([{"type": "text", "data": {"text": "被引用的那句话"}}])
    })
    content = QuotedFetcher(lambda: client).fetch("12345")
    assert content.ok
    assert "被引用的那句话" in content.text
    assert client.calls[0] == ("get_msg", {"message_id": "12345"})


def test_fetch_expands_forward() -> None:
    client = _FakeClient({
        "get_msg": _payload([{"type": "forward", "data": {"id": "fw1"}}]),
        "get_forward_msg": {"status": "ok", "data": {"messages": [
            {"sender": {"nickname": "甲"}, "message": [{"type": "text", "data": {"text": "一"}}]},
            {"sender": {"nickname": "乙"}, "message": [{"type": "text", "data": {"text": "二"}}]},
        ]}},
    })
    content = QuotedFetcher(lambda: client).fetch("777")
    assert content.ok
    assert "甲：一" in content.text and "乙：二" in content.text
    assert ("get_forward_msg", {"message_id": "fw1"}) in client.calls


def test_fetch_does_not_raise_on_failure() -> None:
    client = _FakeClient({})          # 任何动作都抛
    content = QuotedFetcher(lambda: client).fetch("404")
    assert content.ok is False
    assert content.text == ""


def test_fetch_empty_id_short_circuits() -> None:
    client = _FakeClient({})
    content = QuotedFetcher(lambda: client).fetch("")
    assert content.ok is False
    assert client.calls == [], "空 ID 不该发请求"


# --------------------------------------------------------------------------- #
# 接进流水线
# --------------------------------------------------------------------------- #
QUOTE_ID = "70001"
#: 测试里机器人的 QQ 号（conftest 里设的 FAKE_NAPCAT_QQ）
BOT_QQ = "10000001"


def _inject_with_quote(env, text: str, *, message_id: int = 1) -> None:
    """注入一条"引用了某条消息 + @ 了机器人"的群消息。

    注意：假 NapCat 里 ``segments`` 和 ``at_bot`` 是**互斥**的 ——
    给了 segments 就不会自动补 @ 段，所以这里得自己带上。
    """
    _reset(env.home)
    _inject(
        env.home,
        message_type="group",
        group_id=GROUP,
        user_id="333333",
        message_id=message_id,
        text=text,
        nickname="毛糙蛋",
        segments=[
            {"type": "at", "data": {"qq": BOT_QQ}},
            {"type": "reply", "data": {"id": QUOTE_ID}},
            {"type": "text", "data": {"text": " " + text}},
        ],
    )


class _StubFetcher:
    def __init__(self, content) -> None:
        from qlark.chat.quoted import QuotedContent

        self._content = content if content is not None else QuotedContent()
        self.ids: list[str] = []

    def fetch(self, message_id: str):
        self.ids.append(message_id)
        return self._content


def _user_content(payload: dict) -> str:
    content = payload["messages"][-1]["content"]
    if isinstance(content, str):
        return content
    return "\n".join(
        str(part.get("text") or "") for part in content if isinstance(part, dict)
    )


def test_pipeline_puts_quoted_text_into_the_prompt(ready, fake_openai: FakeOpenAI) -> None:
    from qlark.chat.quoted import QuotedContent

    ready.settings.update(quote_context_enabled=True)
    ready.bot.pipeline._quoted_fetcher = _StubFetcher(
        QuotedContent(text="今晚吃白米饭", ok=True)
    )
    fake_openai.reset()

    _inject_with_quote(ready, "你看这句")
    assert wait_for(
        lambda: (ready.bot.pipeline._quoted_fetcher.ids), timeout=30
    ), "应该去取引用内容"

    content = _user_content(fake_openai.requests()[-1])
    assert "【引用了这条消息】今晚吃白米饭" in content
    assert "你看这句" in content
    assert ready.bot.pipeline.stats.quotes == 1


def test_pipeline_survives_a_missing_quoted_message(ready, fake_openai: FakeOpenAI) -> None:
    """原消息被删了 —— 取不到也要照常回复。"""
    from qlark.chat.quoted import QuotedContent

    ready.settings.update(quote_context_enabled=True)
    ready.bot.pipeline._quoted_fetcher = _StubFetcher(QuotedContent(ok=False))
    fake_openai.reset()

    _inject_with_quote(ready, "你看这句")
    assert wait_for(lambda: ready.bot.pipeline.stats.replied >= 1, timeout=30), "应该照常回复"

    content = _user_content(fake_openai.requests()[-1])
    assert "【引用了这条消息】" not in content
    assert ready.bot.pipeline.stats.quotes == 0


def test_quote_context_can_be_disabled(ready, fake_openai: FakeOpenAI) -> None:
    from qlark.chat.quoted import QuotedContent

    ready.settings.update(quote_context_enabled=False)
    stub = _StubFetcher(QuotedContent(text="不该出现", ok=True))
    ready.bot.pipeline._quoted_fetcher = stub
    fake_openai.reset()

    _inject_with_quote(ready, "你看这句")
    assert wait_for(lambda: ready.bot.pipeline.stats.replied >= 1, timeout=30)
    assert stub.ids == [], "关掉之后不该去取引用"


@pytest.mark.parametrize("kinds,expect", [
    (["text"], "只看文字"),
    (["image"], "[图片]"),
])
def test_render_kinds_are_recorded(kinds, expect) -> None:
    if kinds == ["text"]:
        segments = [{"type": "text", "data": {"text": "只看文字"}}]
    else:
        segments = [{"type": "image", "data": {"file": "a.jpg"}}]
    content = render_segments(segments)
    assert expect in content.text
    assert content.kinds == kinds
