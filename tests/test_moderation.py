"""M6 风控测试：速率限制、敏感词过滤、首启向导状态（需求规格 §9.3）。

这三样都是"用户看不见但会被 QQ 看见"的东西：机器人被限流甚至被封，
往往不是因为说了什么，而是因为**短时间回得太多**。
"""

from __future__ import annotations

import time

import pytest

from qlark.chat.moderation import RateLimiter, find_blocked_word, normalize_words
from qlark.settings import Settings
from qlark.storage.db import Database


@pytest.fixture
def db(qlark_home) -> Database:
    database = Database(qlark_home.data / "qlark.db")
    yield database
    database.close()


@pytest.fixture
def settings(db: Database) -> Settings:
    return Settings(db)


# --------------------------------------------------------------------------- #
# 速率限制
# --------------------------------------------------------------------------- #
def test_allows_up_to_limit_then_blocks() -> None:
    limiter = RateLimiter(limit=3)
    for index in range(3):
        allowed, count = limiter.check("group:1")
        assert allowed is True, f"第 {index + 1} 条不该被拦"
        assert count == index + 1

    allowed, count = limiter.check("group:1")
    assert allowed is False
    assert count == 3


def test_window_slides() -> None:
    """窗口滑过之后要恢复放行，不能永久拉黑。"""
    limiter = RateLimiter(limit=2, window=60.0)
    now = 1000.0
    assert limiter.check("g", now=now)[0] is True
    assert limiter.check("g", now=now)[0] is True
    assert limiter.check("g", now=now)[0] is False

    # 61 秒后第一条已经滑出窗口
    assert limiter.check("g", now=now + 61.0)[0] is True


def test_sessions_are_independent() -> None:
    """一个群刷屏不该把另一个群的正常对话一起饿死。"""
    limiter = RateLimiter(limit=1)
    assert limiter.check("group:1")[0] is True
    assert limiter.check("group:1")[0] is False
    assert limiter.check("group:2")[0] is True


def test_zero_means_unlimited() -> None:
    limiter = RateLimiter(limit=0)
    assert limiter.enabled is False
    for _ in range(500):
        assert limiter.check("g")[0] is True


def test_retry_after_reports_wait() -> None:
    limiter = RateLimiter(limit=1, window=60.0)
    now = 500.0
    limiter.check("g", now=now)
    assert limiter.retry_after("g", now=now) == pytest.approx(60.0, abs=0.1)
    assert limiter.retry_after("g", now=now + 59.0) == pytest.approx(1.0, abs=0.1)
    assert limiter.retry_after("g", now=now + 61.0) == 0.0
    assert limiter.retry_after("never-seen") == 0.0


def test_limiter_is_thread_safe() -> None:
    """限流器会被多个会话工作线程同时用，计数不能漏。"""
    import threading

    limiter = RateLimiter(limit=1000, window=60.0)
    results: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(50):
            allowed, _count = limiter.check("shared")
            with lock:
                results.append(allowed)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 400
    assert sum(results) == 400, "上限足够大时不该有任何一条被误拦"


# --------------------------------------------------------------------------- #
# 敏感词
# --------------------------------------------------------------------------- #
def test_find_blocked_word() -> None:
    words = ["广告", "spam"]
    assert find_blocked_word("这是广告内容", words) == "广告"
    assert find_blocked_word("buy SPAM now", words) == "spam", "英文不分大小写"
    assert find_blocked_word("正常聊天", words) is None
    assert find_blocked_word("", words) is None
    assert find_blocked_word("随便什么", []) is None


def test_find_blocked_word_ignores_blank_entries() -> None:
    assert find_blocked_word("abc", ["", "   ", "b"]) == "b"


def test_normalize_words_accepts_messy_input() -> None:
    """界面上是一行一个，但用户很自然会用逗号或顿号。"""
    assert normalize_words("傻, 笨、坏\n蠢") == ["傻", "笨", "坏", "蠢"]
    assert normalize_words(["a", "b"]) == ["a", "b"]
    assert normalize_words(" a , a , b ") == ["a", "b"], "要去重"
    assert normalize_words(None) == []
    assert normalize_words("") == []


def test_normalize_words_has_an_upper_bound() -> None:
    """防止有人贴一整本词典进来。"""
    words = normalize_words(",".join(f"w{index}" for index in range(1000)))
    assert len(words) <= 200


# --------------------------------------------------------------------------- #
# 配置读写
# --------------------------------------------------------------------------- #
def test_moderation_settings_roundtrip(settings: Settings) -> None:
    from qlark import model_config

    assert settings.get("rate_limit_per_minute") == 12
    assert settings.get("blocked_words") == []

    result = model_config.apply_model(
        settings, {"rate_limit_per_minute": 5, "blocked_words": "广告, 刷屏"}
    )
    assert result["ok"] is True

    snapshot = model_config.snapshot(settings)
    assert snapshot["rate_limit_per_minute"] == 5
    assert snapshot["blocked_words"] == ["广告", "刷屏"]


def test_moderation_settings_validation(settings: Settings) -> None:
    from qlark import model_config

    assert model_config.apply_model(settings, {"rate_limit_per_minute": 9999})["ok"] is False
    assert model_config.apply_model(settings, {"rate_limit_per_minute": None})["ok"] is False
    assert model_config.apply_model(settings, {"rate_limit_per_minute": 0})["ok"] is True, "0 是合法的（不限制）"
    assert model_config.apply_model(settings, {"blocked_words": {"不是": "列表"}})["ok"] is False


# --------------------------------------------------------------------------- #
# 首启向导状态
# --------------------------------------------------------------------------- #
def test_wizard_requires_risk_acknowledgement(qlark_home, monkeypatch) -> None:
    """没确认风控就不许结束向导 —— 那是这一步存在的唯一理由。"""
    from qlark.app import Application

    app = Application()
    try:
        state = app.get_wizard_state()
        assert state["first_run_done"] is False
        assert state["risk_acknowledged"] is False
        assert state["steps"]["risk"] is False
        assert state["steps"]["persona"] is True, "出厂人设1 本来就该算可用"

        denied = app.finish_wizard()
        assert denied["ok"] is False
        assert "风控" in denied["error"]
        assert app.get_wizard_state()["first_run_done"] is False, "被拒后不能偷偷标记完成"

        assert app.acknowledge_risk()["ok"] is True
        assert app.get_wizard_state()["steps"]["risk"] is True

        assert app.finish_wizard()["ok"] is True
        assert app.get_wizard_state()["first_run_done"] is True
    finally:
        app.shutdown()


def test_wizard_reflects_real_configuration(qlark_home) -> None:
    """步骤状态取自**真实配置**，而不是另存一份会和现实漂移的进度。"""
    from qlark.app import Application

    app = Application()
    try:
        assert app.get_wizard_state()["steps"]["model"] is False

        app.apply_model_config(
            {"base_url": "https://api.example.com", "api_key": "sk-test", "model": "m"}
        )
        assert app.get_wizard_state()["steps"]["model"] is True, "配好了就该显示已配好"

        assert app.get_wizard_state()["steps"]["login"] is False
        app.settings.qq_account = "2157649443"
        assert app.get_wizard_state()["steps"]["login"] is True
    finally:
        app.shutdown()


def test_restart_wizard(qlark_home) -> None:
    from qlark.app import Application

    app = Application()
    try:
        app.acknowledge_risk()
        app.finish_wizard()
        assert app.get_wizard_state()["first_run_done"] is True

        assert app.restart_wizard()["ok"] is True
        state = app.get_wizard_state()
        assert state["first_run_done"] is False
        assert state["risk_acknowledged"] is True, "重跑向导不该逼用户再确认一次风控"
    finally:
        app.shutdown()
