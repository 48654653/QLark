"""M6 端到端：风控真的拦得住消息（需求规格 §9.3）。

单元测试保证了限流器算法对；这里保证它**真的接在了消息链路上** ——
一个算得对但没接上的限流器，等于没有。
"""

from __future__ import annotations

import pytest

from conftest import FakeOpenAI, _post, wait_for

from test_m2_integration import _inject, _reply_texts, _reset, ready  # noqa: F401


def _reset_sent(home) -> None:
    _reset(home)


def _send(env, text: str, *, message_id: int) -> None:
    _inject(
        env.home,
        message_type="private",
        user_id="20000002",
        message_id=message_id,
        text=text,
    )


# --------------------------------------------------------------------------- #
# 敏感词
# --------------------------------------------------------------------------- #
def test_blocked_word_is_not_replied(ready, fake_openai: FakeOpenAI) -> None:
    """命中敏感词的消息**直接忽略**，连模型都不该调。

    先拦再调，是因为"既然要丢，就别先花掉一次 token"。
    """
    ready.settings.update(blocked_words=["刷屏广告"], rate_limit_per_minute=0)
    fake_openai.reset()
    _reset_sent(ready.home)

    _send(ready, "这是一条刷屏广告消息", message_id=1101)

    assert wait_for(lambda: ready.bot.pipeline.stats.blocked >= 1, timeout=20), "没有被拦下"
    assert _reply_texts(ready.home) == [], "被拦的消息不该有回复"
    assert fake_openai.requests() == [], "被拦的消息不该调用模型"


def test_normal_message_still_works_with_blocklist(ready, fake_openai: FakeOpenAI) -> None:
    ready.settings.update(blocked_words=["刷屏广告"], rate_limit_per_minute=0)
    fake_openai.reset()
    _reset_sent(ready.home)

    _send(ready, "一句正常的话", message_id=1102)
    assert wait_for(lambda: _reply_texts(ready.home), timeout=30), "正常消息被误拦了"
    assert ready.bot.pipeline.stats.blocked == 0


def test_blocked_message_is_not_written_to_memory(ready, fake_openai: FakeOpenAI) -> None:
    from qlark.chat.store import memory_key

    ready.settings.update(blocked_words=["刷屏广告"], rate_limit_per_minute=0)
    fake_openai.reset()
    _reset_sent(ready.home)

    _send(ready, "刷屏广告内容", message_id=1103)
    assert wait_for(lambda: ready.bot.pipeline.stats.blocked >= 1, timeout=20)

    key = memory_key("private:20000002", 1)
    assert ready.bot.pipeline.store.message_count(key) == 0, "被拦的消息不该进记忆"


# --------------------------------------------------------------------------- #
# 速率限制
# --------------------------------------------------------------------------- #
def test_rate_limit_blocks_after_threshold(ready, fake_openai: FakeOpenAI) -> None:
    """超过每分钟上限之后不再回复（QQ 对高频回复非常敏感）。"""
    ready.settings.update(rate_limit_per_minute=2, blocked_words=[])
    fake_openai.reset()
    _reset_sent(ready.home)

    for index in range(2):
        _send(ready, f"第{index + 1}句", message_id=1200 + index)
        assert wait_for(
            lambda: len(_reply_texts(ready.home)) >= index + 1, timeout=30
        ), f"第 {index + 1} 句应当被回复"

    _send(ready, "第三句", message_id=1202)
    assert wait_for(lambda: ready.bot.pipeline.stats.rate_limited >= 1, timeout=20), "没有触发限流"

    # 前两句的回复还在，第三句不该再增加
    assert len(_reply_texts(ready.home)) == 2, "超限的那句不该有回复"


def test_rate_limit_logs_a_clear_reason(ready, fake_openai: FakeOpenAI) -> None:
    ready.settings.update(rate_limit_per_minute=1, blocked_words=[])
    fake_openai.reset()
    _reset_sent(ready.home)

    _send(ready, "第一句", message_id=1210)
    assert wait_for(lambda: _reply_texts(ready.home), timeout=30)

    _send(ready, "第二句", message_id=1211)
    assert wait_for(lambda: ready.bot.pipeline.stats.rate_limited >= 1, timeout=20)

    # 统计和日志不是原子的：pipeline 里先 `stats.rate_limited += 1` 再 `_log(...)`，
    # 所以看到统计变化就立刻读日志会有空窗。等日志本身出现，而不是等统计。
    def logged(keyword: str) -> bool:
        return any(keyword in line.text for line in ready.logbus.tail(4000))

    assert wait_for(lambda: logged("达到上限"), timeout=10), "限流必须留下可读的原因"
    assert wait_for(lambda: logged("秒后恢复"), timeout=10), "要告诉用户多久恢复"


def test_zero_disables_rate_limit(ready, fake_openai: FakeOpenAI) -> None:
    ready.settings.update(rate_limit_per_minute=0, blocked_words=[])
    fake_openai.reset()
    _reset_sent(ready.home)

    for index in range(4):
        _send(ready, f"第{index + 1}句", message_id=1220 + index)
        assert wait_for(
            lambda: len(_reply_texts(ready.home)) >= index + 1, timeout=30
        ), f"不限制时第 {index + 1} 句也该被回复"

    assert ready.bot.pipeline.stats.rate_limited == 0


def test_rate_limit_is_per_session(ready, fake_openai: FakeOpenAI) -> None:
    """一个会话刷屏不该把另一个会话饿死。"""
    ready.settings.update(rate_limit_per_minute=1, blocked_words=[])
    fake_openai.reset()

    _reset_sent(ready.home)
    _send(ready, "甲的第一句", message_id=1230)
    assert wait_for(lambda: _reply_texts(ready.home), timeout=30)

    _send(ready, "甲的第二句", message_id=1231)
    assert wait_for(lambda: ready.bot.pipeline.stats.rate_limited >= 1, timeout=20)

    # 换个会话，应当照常回复
    _reset_sent(ready.home)
    _inject(
        ready.home,
        message_type="private",
        user_id="999999",
        message_id=1232,
        text="乙的第一句",
    )
    assert wait_for(lambda: _reply_texts(ready.home), timeout=30), "另一个会话被误伤"


# --------------------------------------------------------------------------- #
# 默认值
# --------------------------------------------------------------------------- #
def test_default_rate_limit_is_on(ready) -> None:
    """出厂就该有限流 —— 让用户自己去发现"需要限流"是不负责任的。"""
    from qlark.settings import DEFAULTS

    assert DEFAULTS["rate_limit_per_minute"] > 0
    assert DEFAULTS["blocked_words"] == [], "敏感词默认关闭（强行替用户决定容易误伤）"
    assert ready.bot.pipeline._limiter().limit == ready.settings.get("rate_limit_per_minute")
