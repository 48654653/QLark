"""「回复判断表」：把"该回多长、该不该认真"交回模型判断。

**为什么不是掷骰子**：长度和语气完全取决于对方说了什么。真实翻车场景——
对方说「求你了，不@他我就会死」，掷到极短档回一个「嗯」，比说错话更伤人。

根本原则：**骰子只能决定"要不要加一个可选的东西"（表情、连发），
不能决定"这条回复好不好"（长度、语气）。**
表情配错了无伤大雅；敷衍一个正在情绪里的人是真伤害。

判断表放在**消息末尾**（离生成点最近）—— 位置决定效果，前面验证过。
"""

from __future__ import annotations

import json

from conftest import FakeOpenAI, wait_for, wait_memory
from qlark.chat.reply_format import REPLY_RUBRIC
from qlark.chat.store import memory_key

from test_m2_integration import _inject, _reply_texts, _reset, ready  # noqa: F401

GROUP = "888888"


def _say(env, text: str = "你好", message_id: int = 1) -> None:
    _reset(env.home)
    _inject(
        env.home,
        message_type="group",
        group_id=GROUP,
        user_id="333333",
        message_id=message_id,
        text=text,
        at_bot=True,
        nickname="毛糙蛋",
    )
    assert wait_for(lambda: _reply_texts(env.home), timeout=30), "没有回复"


def test_rubric_content_covers_both_directions() -> None:
    """判断表必须**同时**管住"别太长"和"情绪时别敷衍"。

    只写"简洁"是错的 —— 那正是原来长度骰子的思路。
    """
    assert "打招呼" in REPLY_RUBRIC
    assert "在情绪里" in REPLY_RUBRIC
    assert "不许敷衍" in REPLY_RUBRIC
    assert "超过三句" in REPLY_RUBRIC


def test_rubric_mentions_performance_density() -> None:
    """真正的毛病不是"太长"，是每条都塞满人设梗 —— 判断表要管这个。"""
    assert "调料" in REPLY_RUBRIC or "主菜" in REPLY_RUBRIC
    assert "偶尔" in REPLY_RUBRIC


def test_rubric_is_injected_by_default(ready, fake_openai: FakeOpenAI) -> None:
    # 先给一个人设提示词，否则消息里只有「判断表 + 用户消息」两条，
    # messages[0] 和 messages[-2] 会是同一条，"开头没有它"就断言不出来了
    ready.bot.pipeline.personas.update(1, {"system_prompt": "你是鲸鱼娘"})
    ready.settings.update(reply_rubric_enabled=True)
    fake_openai.reset()

    _say(ready)

    payload = fake_openai.requests()[-1]
    messages = payload["messages"]
    # 必须是**末尾**的 system，而不是并进开头那坨
    assert messages[-2]["role"] == "system", [m["role"] for m in messages]
    assert REPLY_RUBRIC in str(messages[-2]["content"])
    assert "你是鲸鱼娘" in str(messages[0]["content"])
    assert REPLY_RUBRIC not in str(messages[0]["content"]), "判断表被并进开头了"


def test_rubric_can_be_disabled(ready, fake_openai: FakeOpenAI) -> None:
    ready.bot.pipeline.personas.update(1, {"system_prompt": "你是鲸鱼娘"})
    ready.settings.update(reply_rubric_enabled=False)
    fake_openai.reset()

    _say(ready)

    payload = json.dumps(fake_openai.requests()[-1], ensure_ascii=False)
    assert REPLY_RUBRIC not in payload


def test_rubric_never_reaches_memory(ready, fake_openai: FakeOpenAI) -> None:
    ready.settings.update(reply_rubric_enabled=True)
    fake_openai.reset()

    _say(ready)

    key = memory_key(f"group:{GROUP}", 1)
    store = ready.bot.pipeline.store
    assert wait_memory(store, key, min_rows=2), "记忆没落库"
    for row in store.memory_messages(key):
        assert "打招呼" not in row["content"] or "昵称" in row["content"]
        assert "这条消息该怎么回" not in row["content"], "判断表进了记忆"


def test_rubric_coexists_with_format_reminder(ready, fake_openai: FakeOpenAI) -> None:
    """掷中分段时，判断表和格式提醒同时在末尾 system 里，且格式提醒在后。"""
    ready.bot.pipeline.personas.update(1, {"system_prompt": "你是鲸鱼娘", "split_enabled": True})
    ready.settings.update(
        split_chance=1.0, meme_chance=0.0, split_delay_ms=0, reply_rubric_enabled=True
    )
    fake_openai.replies('{"messages": ["一"]}', repeat_last=True)

    _say(ready)

    trailing = str(fake_openai.requests()[-1]["messages"][-2]["content"])
    assert REPLY_RUBRIC in trailing
    assert "只输出那个 JSON 对象" in trailing
    # 格式提醒必须在**后半段**（更靠近生成点）
    assert trailing.index("只输出那个 JSON 对象") > trailing.index("这条消息该怎么回")
