"""M4 端到端：真流水线 + 假模型，验证合并式摘要真的按 W 轮触发并进入上下文。

验收标准（需求规格 §11 M4）：**满 W 轮自动合并摘要；失败不丢原文；
群 / 人设 / 私聊隔离正确；删人设连带删记忆并静默回落到人设1**。

这里跑的是完整链路：假 NapCat 推事件 → 过滤/触发 → 会话串行 → 组装上下文
（含长期记忆块）→ 假模型 → 回复 → 落库 → 触发压缩。只有 NapCat 和模型是假的。
"""

from __future__ import annotations

import pytest

from conftest import FakeOpenAI, _get, _post, wait_for
from qlark.chat.store import memory_key
from qlark.core.models import Phase

from test_m2_integration import _inject, _reply_texts, _reset, ready  # noqa: F401


def _requests(fake: FakeOpenAI) -> list[dict]:
    return fake.requests()


def _system_text(payload: dict) -> str:
    for message in payload.get("messages") or []:
        if message.get("role") == "system":
            return str(message.get("content") or "")
    return ""


def _say(env, text: str, *, user_id: str = "2658495997", message_id: int = 1) -> None:
    _reset(env.home)
    _inject(
        env.home,
        message_type="private",
        user_id=user_id,
        message_id=message_id,
        text=text,
    )
    assert wait_for(lambda: _reply_texts(env.home), timeout=30), f"没有回复：{text!r}"


# --------------------------------------------------------------------------- #
# 摘要按 W 轮触发
# --------------------------------------------------------------------------- #
def test_summary_triggers_at_w_rounds(ready, fake_openai: FakeOpenAI) -> None:
    """满 W 轮后自动合并摘要，并把摘要作为独立 system 块带给后续请求。"""
    ready.settings.update(memory_rounds=2, auto_summary=True)
    key = memory_key("private:2658495997", 1)
    store = ready.bot.pipeline.store

    _say(ready, "第一句", message_id=101)
    _say(ready, "第二句", message_id=102)

    assert wait_for(lambda: bool(store.summary_text(key)), timeout=30), (
        "满 W 轮后没有生成摘要"
    )
    assert store.unsummarized_rounds(key) == 0, "压缩过的轮次必须被标记"
    assert store.message_count(key) == 4, "压缩不该删除原文"

    # 再聊一句：这次请求的 system 块里必须带上长期记忆
    _say(ready, "第三句", message_id=103)
    system = _system_text(_requests(fake_openai)[-1])
    assert "[长期记忆]" in system, f"长期记忆没有进入上下文：{system[:200]!r}"


def test_summary_stats_are_counted(ready, fake_openai: FakeOpenAI) -> None:
    """总结调用的 token 也要计入今日统计（M6 的验收点之一）。"""
    ready.settings.update(memory_rounds=2, auto_summary=True)

    before = ready.db.stats_today().calls
    _say(ready, "甲", message_id=111)
    _say(ready, "乙", message_id=112)

    store = ready.bot.pipeline.store
    key = memory_key("private:2658495997", 1)
    assert wait_for(lambda: bool(store.summary_text(key)), timeout=30)

    # 2 次对话 + 至少 1 次总结
    assert ready.db.stats_today().calls >= before + 3


def test_summary_disabled_keeps_raw_history(ready, fake_openai: FakeOpenAI) -> None:
    ready.settings.update(memory_rounds=2, auto_summary=False)
    key = memory_key("private:2658495997", 1)
    store = ready.bot.pipeline.store

    _say(ready, "甲", message_id=201)
    _say(ready, "乙", message_id=202)

    assert store.summary_text(key) == ""
    assert store.unsummarized_rounds(key) == 2
    assert store.message_count(key) == 4


# --------------------------------------------------------------------------- #
# 失败不丢原文（端到端）
# --------------------------------------------------------------------------- #
def test_failed_reply_is_not_written_to_memory(ready, fake_openai: FakeOpenAI) -> None:
    """模型调用失败的那一轮不写记忆，避免留下"有问无答"的空洞。"""
    ready.settings.update(memory_rounds=20, auto_summary=False)
    key = memory_key("private:2658495997", 1)

    fake_openai.fail_next(times=99, status=500, message="boom")
    _reset(ready.home)
    _inject(
        ready.home,
        message_type="private",
        user_id="2658495997",
        message_id=301,
        text="这句话会失败",
    )
    assert wait_for(lambda: ready.bot.pipeline.stats.failed >= 1, timeout=30)
    assert ready.bot.pipeline.store.message_count(key) == 0


def test_summary_failure_keeps_all_messages(ready, fake_openai: FakeOpenAI) -> None:
    """总结调用失败时一条原文都不许丢，并且要标记 pending 待重试。

    做法：先正常聊满 W 轮，然后**只让总结调用失败**（对话请求放行）。
    假服务按"第 N 次请求"计数不好控，所以这里直接替换摘要器的 complete。
    """
    ready.settings.update(memory_rounds=2, auto_summary=True)
    summarizer = ready.bot.pipeline.summarizer

    from qlark.llm.client import LLMError, LLMErrorKind

    calls = {"n": 0}

    def always_fail(_messages):
        calls["n"] += 1
        raise LLMError(LLMErrorKind.SERVER, "总结服务 500")

    summarizer._complete = always_fail  # type: ignore[attr-defined]

    _say(ready, "第一句", message_id=311)
    _say(ready, "第二句", message_id=312)

    store = ready.bot.pipeline.store
    key = memory_key("private:2658495997", 1)
    assert wait_for(lambda: calls["n"] >= 1, timeout=30), "压缩没有被触发"

    assert store.summary_text(key) == ""
    assert store.summary_pending(key) is True, "失败必须标记 pending"
    assert store.message_count(key) == 4, "失败时一条原文都不许丢"
    assert store.unsummarized_rounds(key) == 2, "失败的轮次仍算未压缩，下轮重试"


# --------------------------------------------------------------------------- #
# 隔离性
# --------------------------------------------------------------------------- #
def test_memories_are_isolated_between_private_users(ready, fake_openai: FakeOpenAI) -> None:
    ready.settings.update(memory_rounds=20, auto_summary=False)

    _say(ready, "我是甲", user_id="111111", message_id=401)
    _say(ready, "我是乙", user_id="222222", message_id=402)

    store = ready.bot.pipeline.store
    assert store.message_count(memory_key("private:111111", 1)) == 2
    assert store.message_count(memory_key("private:222222", 1)) == 2

    memories = {item["memory_key"] for item in store.list_memories()}
    assert memory_key("private:111111", 1) in memories
    assert memory_key("private:222222", 1) in memories


def test_group_and_private_are_separate(ready, fake_openai: FakeOpenAI) -> None:
    ready.settings.update(memory_rounds=20, auto_summary=False)

    _say(ready, "私聊的话", user_id="333333", message_id=501)

    _reset(ready.home)
    _inject(
        ready.home,
        message_type="group",
        group_id="888888",
        user_id="333333",
        message_id=502,
        text="群里的话",
        at_bot=True,
    )
    assert wait_for(lambda: _reply_texts(ready.home), timeout=30)

    store = ready.bot.pipeline.store
    assert store.message_count(memory_key("private:333333", 1)) == 2
    assert store.message_count(memory_key("group:888888", 1)) == 2


# --------------------------------------------------------------------------- #
# 删除人设连带删记忆
# --------------------------------------------------------------------------- #
def test_deleting_persona_removes_its_memory(ready, fake_openai: FakeOpenAI) -> None:
    """删人设时：该人设的记忆一并删除，会话静默回落到人设1。"""
    ready.settings.update(memory_rounds=20, auto_summary=False)
    store = ready.bot.pipeline.store
    personas = ready.bot.pipeline.personas

    uid = int(personas.create("临时人设")["uid"])
    session = "private:2658495997"
    store.set_session_persona(session, uid, session_type="private", session_ref="2658495997")

    _say(ready, "用人设2说的话", message_id=601)
    key2 = memory_key(session, uid)
    assert store.message_count(key2) == 2

    result = personas.delete(uid)
    assert result.deleted_messages >= 2, "人设记忆没被连带删除"
    assert store.message_count(key2) == 0
    assert store.session_persona(
        session, session_type="private", session_ref="2658495997"
    ) == 1, "会话应当静默回落到人设1"


# --------------------------------------------------------------------------- #
# 手动压缩
# --------------------------------------------------------------------------- #
def test_manual_summarize_reports_not_enough(ready) -> None:
    """不到 W 轮时手动压缩要如实报告，而不是硬压。

    硬压会把还很新鲜、模型正需要逐字看到的对话提前揉成一团。
    """
    ready.settings.update(memory_rounds=50, auto_summary=True)
    outcome = ready.bot.pipeline.summarizer.maybe_summarize(
        memory_key("private:2658495997", 1)
    )
    assert outcome.status == "not_enough"


def test_manual_summarize_ignores_auto_switch(ready, fake_openai: FakeOpenAI) -> None:
    """「自动总结」关着时，手动点「立即压缩」仍然要能压。

    那个开关管的是**要不要自动压**，不是"能不能压"。
    """
    ready.settings.update(memory_rounds=2, auto_summary=False)
    store = ready.bot.pipeline.store
    key = memory_key("private:2658495997", 1)

    _say(ready, "甲", message_id=701)
    _say(ready, "乙", message_id=702)
    assert store.summary_text(key) == "", "自动总结关着，不该自己压"

    # 自动路径：应该被开关挡住
    assert ready.bot.pipeline.summarizer.maybe_summarize(key).status == "disabled"

    # 手动路径：应当真的压下去
    outcome = ready.bot.pipeline.summarizer.maybe_summarize(key, force=True)
    assert outcome.status == "ok"
    assert store.summary_text(key) != ""
    assert store.message_count(key) == 4, "压缩不该删除原文"
