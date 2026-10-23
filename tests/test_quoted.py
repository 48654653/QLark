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

    # 「取到引用」不等于「请求已发出」—— 取回之后才组装消息、才调模型。
    # 直接读 requests()[-1] 会偶发 IndexError，必须等请求真的到。
    assert wait_for(lambda: fake_openai.requests(), timeout=30), "应该调用模型"

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


# --------------------------------------------------------------------------- #
# 单独发过来的聊天记录（不是引用）
#
# 以前 forward 段只被渲染成字面量 "[合并转发]"，内容从来没去取过 ——
# 用户单独发一段聊天记录，机器人眼里只有那四个字。
# --------------------------------------------------------------------------- #
def test_forward_segment_is_recognised_when_parsing() -> None:
    from qlark.onebot.segments import parse_segments

    parsed = parse_segments(
        [{"type": "forward", "data": {"id": "FW123"}}], self_id="10000001"
    )
    assert parsed.text == "[合并转发]", "记忆里存的仍然只是占位符"
    assert parsed.forwards == [{"id": "FW123"}], "原始 data 要留下来给流水线展开"
    assert not parsed.is_empty, "只有一段转发也算有内容，不能被当成空消息丢掉"


def test_forward_is_not_recognised_without_the_segment() -> None:
    from qlark.onebot.segments import parse_segments

    parsed = parse_segments(
        [{"type": "text", "data": {"text": "你好"}}], self_id="10000001"
    )
    assert parsed.forwards == []


def test_fetch_forward_handles_inline_content() -> None:
    """NapCat 常常把内容内联在 data.content 里，不用再调接口。"""
    fetcher = QuotedFetcher(lambda: None)
    content = fetcher.fetch_forward({
        "content": [
            {"sender": {"nickname": "甲"}, "message": [{"type": "text", "data": {"text": "看图"}}]},
            {"sender": {"nickname": "乙"}, "message": [{"type": "image", "data": {"url": "https://x/a.jpg"}}]},
        ]
    })
    assert content.ok
    assert "甲：看图" in content.text
    assert len(content.images) == 1, "聊天记录里的图片要能被视觉模型看到"


def test_fetch_forward_falls_back_to_get_forward_msg() -> None:
    client = _FakeClient({
        "get_forward_msg": {"data": {"messages": [
            {"sender": {"nickname": "丙"}, "message": [{"type": "text", "data": {"text": "在吗"}}]},
        ]}},
    })
    content = QuotedFetcher(lambda: client).fetch_forward({"id": "FW1"})
    assert content.ok
    assert "丙：在吗" in content.text
    assert client.calls[0][0] == "get_forward_msg"


def test_fetch_forward_does_not_raise() -> None:
    content = QuotedFetcher(lambda: _FakeClient({})).fetch_forward({"id": "BAD"})
    assert content.ok is False
    assert content.text == ""


class _StubForwardFetcher:
    """替换掉 pipeline 里的取回器，直接给一段预设内容。"""

    def __init__(self, content=None) -> None:
        from qlark.chat.quoted import QuotedContent

        self._content = content if content is not None else QuotedContent()
        self.forward_calls: list[dict] = []

    def fetch(self, message_id: str):
        from qlark.chat.quoted import QuotedContent

        return QuotedContent()

    def fetch_forward(self, data):
        self.forward_calls.append(dict(data or {}))
        return self._content


def _inject_forward(env, text: str = "", *, message_id: int = 1, forward_id: str = "FW77") -> None:
    segments = [
        {"type": "at", "data": {"qq": BOT_QQ}},
        {"type": "forward", "data": {"id": forward_id}},
    ]
    if text:
        segments.append({"type": "text", "data": {"text": " " + text}})
    _reset(env.home)
    _inject(
        env.home,
        message_type="group",
        group_id=GROUP,
        user_id="333333",
        message_id=message_id,
        text=text,
        nickname="毛糙蛋",
        segments=segments,
    )


def test_pipeline_expands_a_standalone_forward(ready, fake_openai: FakeOpenAI) -> None:
    """单独发一段聊天记录：内容要真的进提示词。"""
    from qlark.chat.quoted import QuotedContent

    ready.settings.update(quote_context_enabled=True)
    stub = _StubForwardFetcher(
        QuotedContent(text="甲：今晚吃饭吗 / 乙：吃", ok=True)
    )
    ready.bot.pipeline._quoted_fetcher = stub
    fake_openai.reset()

    _inject_forward(ready, "这段你看")
    assert wait_for(lambda: stub.forward_calls, timeout=30), "应该去展开聊天记录"
    assert wait_for(lambda: fake_openai.requests(), timeout=30), "应该调用模型"

    content = _user_content(fake_openai.requests()[-1])
    assert "【转发的聊天记录】甲：今晚吃饭吗 / 乙：吃" in content
    assert "这段你看" in content
    assert stub.forward_calls[0] == {"id": "FW77"}


def test_pipeline_forward_with_only_a_record_still_replies(ready, fake_openai: FakeOpenAI) -> None:
    """只发一段聊天记录、一个字都不打，也要照常回复。"""
    from qlark.chat.quoted import QuotedContent

    ready.settings.update(quote_context_enabled=True)
    ready.bot.pipeline._quoted_fetcher = _StubForwardFetcher(
        QuotedContent(text="甲：在吗", ok=True)
    )
    fake_openai.reset()

    _inject_forward(ready)
    assert wait_for(lambda: ready.bot.pipeline.stats.replied >= 1, timeout=30)

    content = _user_content(fake_openai.requests()[-1])
    assert "【转发的聊天记录】甲：在吗" in content


def test_forward_failure_falls_back_to_placeholder(ready, fake_openai: FakeOpenAI) -> None:
    """展开失败也要照常回复，只留 [合并转发] 占位符。"""
    from qlark.chat.quoted import QuotedContent

    ready.settings.update(quote_context_enabled=True)
    ready.bot.pipeline._quoted_fetcher = _StubForwardFetcher(QuotedContent(ok=False))
    fake_openai.reset()

    _inject_forward(ready, "看看")
    assert wait_for(lambda: ready.bot.pipeline.stats.replied >= 1, timeout=30)

    content = _user_content(fake_openai.requests()[-1])
    assert "【转发的聊天记录】" not in content
    assert "[合并转发]" in content


def test_forward_can_be_disabled(ready, fake_openai: FakeOpenAI) -> None:
    from qlark.chat.quoted import QuotedContent

    ready.settings.update(quote_context_enabled=False)
    stub = _StubForwardFetcher(QuotedContent(text="不该出现", ok=True))
    ready.bot.pipeline._quoted_fetcher = stub
    fake_openai.reset()

    _inject_forward(ready, "看看")
    assert wait_for(lambda: ready.bot.pipeline.stats.replied >= 1, timeout=30)
    assert stub.forward_calls == [], "关掉之后不该去展开"
