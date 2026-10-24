"""M4 记忆系统测试：合并式摘要 + 失败保护 + 上下文组装 + 记忆面板读写。

对应需求规格 v1.1 §6.2 / §6.3 / §3.6。

**最要紧的一组**是"压缩失败不丢原文"：宁可让上下文暂时变长，也不能因为一次
网络抖动就把用户的记忆弄丢。
"""

from __future__ import annotations

from typing import Any

import pytest

from qlark.chat.store import MessageStore, memory_key
from qlark.chat.summarizer import (
    SUMMARY_MATERIAL_MAX_CHARS,
    SUMMARY_MAX_CHARS,
    Summarizer,
    build_material,
    fit_material,
    truncate_summary,
)
from qlark.llm.client import ChatResult, LLMError, LLMErrorKind
from qlark.settings import Settings
from qlark.storage.db import Database

KEY = memory_key("group:123456", 1)


@pytest.fixture
def db(qlark_home) -> Database:
    database = Database(qlark_home.data / "qlark.db")
    yield database
    database.close()


@pytest.fixture
def store(db: Database) -> MessageStore:
    instance = MessageStore(db)
    instance.ensure_default_persona()
    return instance


class FakeSummarizer:
    """可控的"总结模型"。"""

    def __init__(self, reply: str = "用户养了一只叫豆豆的猫。", error: Exception | None = None):
        self.reply = reply
        self.error = error
        self.calls: list[list[dict[str, Any]]] = []

    def __call__(self, messages: list[dict[str, Any]]) -> ChatResult:
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return ChatResult(text=self.reply, prompt_tokens=100, completion_tokens=50)


def make_summarizer(store: MessageStore, settings: Settings, fake: FakeSummarizer) -> Summarizer:
    return Summarizer(settings, store, complete=fake)


def add_rounds(store: MessageStore, count: int, *, key: str = KEY, start: int = 1) -> None:
    for index in range(count):
        number = start + index
        store.append(key, "user", f"第{number}个问题")
        store.append(key, "assistant", f"第{number}个回答")


# --------------------------------------------------------------------------- #
# 轮数统计
# --------------------------------------------------------------------------- #
def test_unsummarized_rounds_counts_complete_rounds_only(store: MessageStore) -> None:
    """一轮 = user + assistant；孤立的半轮不算，否则会把残缺内容喂给总结模型。"""
    store.append(KEY, "user", "只有问")
    assert store.unsummarized_rounds(KEY) == 0
    store.append(KEY, "assistant", "有答了")
    assert store.unsummarized_rounds(KEY) == 1

    add_rounds(store, 3, start=2)
    assert store.unsummarized_rounds(KEY) == 4


def test_summarized_messages_are_not_counted(store: MessageStore) -> None:
    add_rounds(store, 2)
    rows = store.oldest_unsummarized(KEY, 2)
    store.apply_summary(KEY, "旧总结", [int(r["id"]) for r in rows])
    assert store.unsummarized_rounds(KEY) == 0


# --------------------------------------------------------------------------- #
# 触发条件
# --------------------------------------------------------------------------- #
def test_not_enough_rounds_does_nothing(store: MessageStore, db: Database) -> None:
    settings = Settings(db)
    settings.memory_rounds = 5
    fake = FakeSummarizer()
    add_rounds(store, 4)

    outcome = make_summarizer(store, settings, fake).maybe_summarize(KEY)
    assert outcome.status == "not_enough"
    assert fake.calls == [], "不到 W 轮不该调用模型"


def test_triggers_exactly_at_w_rounds(store: MessageStore, db: Database) -> None:
    settings = Settings(db)
    settings.memory_rounds = 3
    fake = FakeSummarizer()
    add_rounds(store, 3)

    outcome = make_summarizer(store, settings, fake).maybe_summarize(KEY)
    assert outcome.status == "ok"
    assert len(fake.calls) == 1
    assert store.summary_text(KEY) == fake.reply
    assert store.unsummarized_rounds(KEY) == 0, "压缩过的轮次必须被标记"


def test_disabled_auto_summary_does_nothing(store: MessageStore, db: Database) -> None:
    settings = Settings(db)
    settings.memory_rounds = 2
    settings.auto_summary = False
    fake = FakeSummarizer()
    add_rounds(store, 10)

    outcome = make_summarizer(store, settings, fake).maybe_summarize(KEY)
    assert outcome.status == "disabled"
    assert fake.calls == []
    assert store.unsummarized_rounds(KEY) == 10, "关掉自动总结后原文必须原样累积"


def test_only_w_rounds_are_consumed_each_time(store: MessageStore, db: Database) -> None:
    """一次只压缩最早的 W 轮，不能把全部历史一口吞掉。"""
    settings = Settings(db)
    settings.memory_rounds = 3
    fake = FakeSummarizer()
    add_rounds(store, 8)

    make_summarizer(store, settings, fake).maybe_summarize(KEY)
    assert store.unsummarized_rounds(KEY) == 5, "只应消耗 3 轮"

    make_summarizer(store, settings, fake).maybe_summarize(KEY)
    assert store.unsummarized_rounds(KEY) == 2


# --------------------------------------------------------------------------- #
# 失败保护（最重要）
# --------------------------------------------------------------------------- #
def test_failure_keeps_every_message_and_marks_pending(store: MessageStore, db: Database) -> None:
    """压缩失败：一条原文都不许删，并且标记 pending 待重试。"""
    settings = Settings(db)
    settings.memory_rounds = 3
    fake = FakeSummarizer(error=LLMError(LLMErrorKind.TIMEOUT, "请求超时"))
    add_rounds(store, 3)
    before = store.message_count(KEY)

    outcome = make_summarizer(store, settings, fake).maybe_summarize(KEY)

    assert outcome.status == "failed"
    assert store.message_count(KEY) == before, "失败时绝不能删除原文"
    assert store.unsummarized_rounds(KEY) == 3, "失败后这些轮次仍应算作未压缩"
    assert store.summary_pending(KEY) is True, "必须标记 pending 以便重试"
    assert store.summary_text(KEY) == ""


def test_retry_after_failure_succeeds(store: MessageStore, db: Database) -> None:
    settings = Settings(db)
    settings.memory_rounds = 3
    failing = FakeSummarizer(error=LLMError(LLMErrorKind.SERVER, "500"))
    add_rounds(store, 3)

    assert make_summarizer(store, settings, failing).maybe_summarize(KEY).status == "failed"
    assert store.summary_pending(KEY) is True

    good = FakeSummarizer(reply="重试成功了")
    assert make_summarizer(store, settings, good).maybe_summarize(KEY).status == "ok"
    assert store.summary_text(KEY) == "重试成功了"
    assert store.summary_pending(KEY) is False, "成功之后必须清掉 pending"


def test_unexpected_exception_is_contained(store: MessageStore, db: Database) -> None:
    """任何异常都不许冒泡出去 —— 压缩跑在回复之后，不能影响对话。"""
    settings = Settings(db)
    settings.memory_rounds = 2
    add_rounds(store, 2)

    def boom(_messages):
        raise RuntimeError("不是 LLMError 的意外错误")

    outcome = Summarizer(settings, store, complete=boom).maybe_summarize(KEY)
    assert outcome.status == "failed"
    assert store.message_count(KEY) == 4
    assert store.summary_pending(KEY) is True


def test_empty_reply_is_treated_as_failure(store: MessageStore, db: Database) -> None:
    settings = Settings(db)
    settings.memory_rounds = 2
    add_rounds(store, 2)

    outcome = make_summarizer(store, settings, FakeSummarizer(reply="   ")).maybe_summarize(KEY)
    assert outcome.status == "empty"
    assert store.message_count(KEY) == 4, "空摘要不能顶掉原文"
    assert store.summary_pending(KEY) is True


# --------------------------------------------------------------------------- #
# 递归合并
# --------------------------------------------------------------------------- #
def test_recursive_summary_includes_old_summary(store: MessageStore, db: Database) -> None:
    """新总结 = 总结(老总结 + 新 W 轮)：老总结必须出现在材料里。"""
    settings = Settings(db)
    settings.memory_rounds = 2
    fake = FakeSummarizer(reply="第一版总结")
    summarizer = make_summarizer(store, settings, fake)

    add_rounds(store, 2)
    summarizer.maybe_summarize(KEY)
    assert store.summary_text(KEY) == "第一版总结"

    add_rounds(store, 2, start=3)
    summarizer.maybe_summarize(KEY)

    material = fake.calls[-1][-1]["content"]
    assert "第一版总结" in material, "老总结没有并入新材料，记忆会断代"
    assert "第3个问题" in material
    assert "【此前摘要】" in material


def test_material_marks_roles(store: MessageStore) -> None:
    rows = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你也好"}]
    material = build_material("", rows)
    assert "用户：你好" in material
    assert "机器人：你也好" in material


def test_truncate_summary_enforces_limit() -> None:
    long_text = "字" * 500
    result = truncate_summary(long_text)
    assert len(result) <= SUMMARY_MAX_CHARS + 1
    assert result.endswith("…")


# --------------------------------------------------------------------------- #
# 材料长度上限（W 设得很大时不能把模型上下文撑爆）
# --------------------------------------------------------------------------- #
def test_fit_material_stops_at_the_limit() -> None:
    rows = []
    for index in range(40):
        rows.append({"id": index + 1, "role": "user", "content": "长" * 1000})
        rows.append({"id": index + 1, "role": "assistant", "content": "长" * 1000})

    kept = fit_material("", rows, limit=5000)
    assert 0 < len(kept) < len(rows), "超过上限时必须截断"
    total = sum(len(row["content"]) for row in kept)
    assert total <= 5000


def test_fit_material_keeps_whole_rounds() -> None:
    """绝不能把一轮拆开 —— 那会让总结模型把"问"当成"答"。"""
    rows = [{"id": 1, "role": "user", "content": "短"}]
    for index in range(2, 12):
        rows.append({"id": index, "role": "assistant" if index % 2 else "user", "content": "长" * 800})

    kept = fit_material("", rows, limit=3000)
    assert len(kept) % 2 == 0, f"留下的不是整数轮：{len(kept)} 条"


def test_fit_material_always_keeps_at_least_one_round() -> None:
    """单条消息本身就超长时也要让压缩有进展，否则这些内容永远卡在未压缩。"""
    rows = [
        {"id": 1, "role": "user", "content": "超" * 50000},
        {"id": 2, "role": "assistant", "content": "超" * 50000},
    ]
    kept = fit_material("", rows, limit=100)
    assert len(kept) == 2, "至少保留一轮"


def test_summarize_with_huge_material_still_succeeds(store: MessageStore, db: Database) -> None:
    """W 很大 + 消息很长时，压缩必须照常成功（不能因为材料超长就永远失败）。"""
    settings = Settings(db)
    settings.memory_rounds = 50
    fake = FakeSummarizer(reply="压缩成功")
    for index in range(50):
        store.append(KEY, "user", "问" * 3000)
        store.append(KEY, "assistant", "答" * 3000)

    outcome = make_summarizer(store, settings, fake).maybe_summarize(KEY)
    assert outcome.status == "ok", f"材料超长导致压缩失败：{outcome.error}"
    assert outcome.rounds < 50, "应当只压能塞进上下文的那几轮"

    material = fake.calls[-1][-1]["content"]
    assert len(material) < SUMMARY_MATERIAL_MAX_CHARS + 2000
    # 剩下的仍然算未压缩，下一轮继续
    assert store.unsummarized_rounds(KEY) > 0


# --------------------------------------------------------------------------- #
# 组装请求时的上下文
# --------------------------------------------------------------------------- #
def test_context_includes_summary_as_system_block(store: MessageStore) -> None:
    from qlark.chat.pipeline import SUMMARY_BLOCK_LABEL
    from qlark.chat.context import build_chat_messages

    store.apply_summary(KEY, "用户偏好喝美式", [])
    summary = store.summary_text(KEY)
    extra = [f"{SUMMARY_BLOCK_LABEL} {summary}"] if summary else []
    messages = build_chat_messages(
        system_prompt="你是助手", history=[("user", "在吗")], user_content="在吗", extra_system=extra
    )
    assert messages[0]["role"] == "system"
    assert "你是助手" in messages[0]["content"]
    assert "[长期记忆] 用户偏好喝美式" in messages[0]["content"]


def test_summary_survives_deletion_of_messages(store: MessageStore) -> None:
    store.apply_summary(KEY, "总结还在", [])
    store.clear(KEY)
    assert store.summary_text(KEY) == "", "clear 应当同时清掉总结"


# --------------------------------------------------------------------------- #
# 面板读写
# --------------------------------------------------------------------------- #
def test_delete_summary_resets_summarized_flag(store: MessageStore) -> None:
    """只删总结时必须复位 summarized，否则这些原文会永久消失。

    删掉总结后如果原文还被标记为"已压缩"，它们既不在总结里、也不会再被压缩，
    等于凭空蒸发 —— 这是最容易写错的一处。
    """
    add_rounds(store, 2)
    rows = store.oldest_unsummarized(KEY, 2)
    store.apply_summary(KEY, "总结", [int(r["id"]) for r in rows])
    assert store.unsummarized_rounds(KEY) == 0

    assert store.delete_summary(KEY) is True
    assert store.summary_text(KEY) == ""
    assert store.unsummarized_rounds(KEY) == 2, "原文必须重新变成未压缩状态"
    assert store.message_count(KEY) == 4


def test_memory_overview(store: MessageStore) -> None:
    add_rounds(store, 3)
    store.apply_summary(KEY, "一段总结", [])
    overview = store.memory_overview()
    assert overview["sessions"] == 1
    assert overview["messages"] == 6
    assert overview["summaries"] == 1
    assert overview["content_bytes"] > 0


def test_list_memories_parses_key_and_persona(store: MessageStore, db: Database) -> None:
    db.execute(
        "INSERT INTO session_state(session_id, session_type, session_ref, active_persona, updated_at) "
        "VALUES('group:123456', 'group', '123456', 1, 1)"
    )
    add_rounds(store, 2)
    items = store.list_memories()
    assert len(items) == 1
    item = items[0]
    assert item["session_id"] == "group:123456"
    assert item["session_type"] == "group"
    assert item["session_ref"] == "123456"
    assert item["persona_uid"] == 1
    assert item["messages"] == 4


def test_list_memories_reports_rounds_not_just_messages(store: MessageStore) -> None:
    """面板要同时给出「条数」和「轮数」。

    这是 v2.2.1 修的误判：卡片只写"54 条原文"，用户当成 54 轮，
    于是以为早过了 30 轮门槛、自动压缩坏了 —— 实际只有 27 轮，差 3 轮。
    """
    add_rounds(store, 3)
    item = store.list_memories()[0]
    assert item["messages"] == 6
    assert item["rounds"] == 3, "轮数必须是一问一答的对数"
    assert item["uncompressed_rounds"] == 3

    rows = store.oldest_unsummarized(KEY, 1)
    store.apply_summary(KEY, "旧总结", [int(r["id"]) for r in rows])
    item = store.list_memories()[0]
    assert item["messages"] == 6
    assert item["rounds"] == 3
    assert item["uncompressed_rounds"] == 2
    assert item["compressed"] == 2


def test_manual_summarize_ignores_window(store: MessageStore, db: Database) -> None:
    """手动压缩不受 W 轮门槛限制：只要还有完整轮次就压。

    以前未压缩 27 轮（门槛 30）时点「立即压缩」什么都不发生，
    用户只能得出"压缩坏了"这个结论（v2.2.1 修的）。
    """
    settings = Settings(db)
    settings.memory_rounds = 30
    add_rounds(store, 3)
    fake = FakeSummarizer()
    summarizer = make_summarizer(store, settings, fake)

    # 自动路径：不够 30 轮，不动
    assert summarizer.maybe_summarize(KEY).status == "not_enough"
    assert fake.calls == []

    # 手动路径：把这 3 轮直接压掉
    outcome = summarizer.maybe_summarize(KEY, force=True)
    assert outcome.status == "ok"
    assert outcome.rounds == 3
    assert store.summary_text(KEY) == fake.reply
    assert store.unsummarized_rounds(KEY) == 0


def test_manual_summarize_with_zero_rounds_is_a_noop(store: MessageStore, db: Database) -> None:
    settings = Settings(db)
    settings.memory_rounds = 30
    fake = FakeSummarizer()
    outcome = make_summarizer(store, settings, fake).maybe_summarize(KEY, force=True)
    assert outcome.status == "not_enough"
    assert outcome.rounds == 0
    assert fake.calls == [], "一轮都没有时不该调模型"


def test_memory_messages_edit_and_delete(store: MessageStore) -> None:
    add_rounds(store, 2)
    rows = store.memory_messages(KEY)
    assert len(rows) == 4

    target = int(rows[0]["id"])
    assert store.update_message(target, "改过的内容") is True
    assert store.memory_messages(KEY)[0]["content"] == "改过的内容"

    assert store.delete_message(target) is True
    assert store.message_count(KEY) == 3
    assert store.delete_message(999999) is False


def test_memory_keys_are_isolated(store: MessageStore) -> None:
    """群 / 人设 / 私聊之间记忆互不干扰（需求规格 §6.1）。"""
    other_group = memory_key("group:999", 1)
    other_persona = memory_key("group:123456", 2)
    private = memory_key("private:20000002", 1)

    add_rounds(store, 2, key=KEY)
    add_rounds(store, 5, key=other_group)
    add_rounds(store, 3, key=other_persona)
    add_rounds(store, 7, key=private)

    assert store.message_count(KEY) == 4
    assert store.message_count(other_group) == 10
    assert store.message_count(other_persona) == 6
    assert store.message_count(private) == 14


def test_apply_summary_is_atomic(store: MessageStore, db: Database) -> None:
    """总结与标记必须一起成功；中途出错要整体回滚。"""
    add_rounds(store, 2)
    rows = store.oldest_unsummarized(KEY, 2)
    ids = [int(r["id"]) for r in rows]

    # 让事务里的第二条语句失败
    original = db.executemany

    def failing(sql: str, seq=()):
        if "UPDATE messages SET summarized" in sql:
            raise RuntimeError("模拟写入失败")
        return original(sql, seq)

    db.executemany = failing  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError):
            store.apply_summary(KEY, "不该留下的总结", ids)
    finally:
        db.executemany = original  # type: ignore[method-assign]

    assert store.summary_text(KEY) == "", "事务失败后总结不该写进去"
    assert store.unsummarized_rounds(KEY) == 2, "事务失败后原文状态不该改变"
