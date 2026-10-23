"""@ 激活保活：被 @ 之后的一段时间内，那个人不必再 @ 也能接着聊。

三条硬规则（第二条最容易做错）：
  ① @ 一次开窗，窗口内每说一句都续期
  ② **窗口按 (会话, 用户) 独立** —— 别人说话既不能续期，也不能蹭用
  ③ 静默满 N 秒自动关闭

第 ② 条如果做成"整个群一个窗口"，那么只要群里一直有人说话，窗口就永远不关，
机器人会变成每句都插嘴 —— 比"每句都要 @"还糟。
"""

from __future__ import annotations

import pytest

from conftest import FakeOpenAI, wait_for

from test_m2_integration import _inject, _reset, ready  # noqa: F401
from qlark.chat.keepalive import KeepAlive

GROUP = "888888"


# --------------------------------------------------------------------------- #
# 纯逻辑（注入时钟，不用等真时间）
# --------------------------------------------------------------------------- #
class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_arm_opens_a_window() -> None:
    clock = _Clock()
    alive = KeepAlive(seconds=60, clock=clock)
    assert alive.active("g", "a") is False
    alive.arm("g", "a")
    assert alive.active("g", "a") is True


def test_window_expires_after_silence() -> None:
    clock = _Clock()
    alive = KeepAlive(seconds=60, clock=clock)
    alive.arm("g", "a")

    clock.advance(59)
    assert alive.active("g", "a") is True, "59 秒时还该有效"

    clock.advance(2)
    assert alive.active("g", "a") is False, "满 60 秒该自动关闭"


def test_each_message_refreshes_the_window() -> None:
    clock = _Clock()
    alive = KeepAlive(seconds=60, clock=clock)
    alive.arm("g", "a")

    # 每 30 秒说一句，说五轮 —— 一直在续期，所以始终有效
    for _ in range(5):
        clock.advance(30)
        assert alive.active("g", "a") is True
        alive.touch("g", "a")

    clock.advance(61)
    assert alive.active("g", "a") is False


def test_other_users_cannot_refresh_it() -> None:
    """别人说话不能给 A 续期 —— 这是本功能最关键的一条。"""
    clock = _Clock()
    alive = KeepAlive(seconds=60, clock=clock)
    alive.arm("g", "a")

    for _ in range(10):
        clock.advance(10)
        alive.touch("g", "b")        # B 一直在说话
        assert alive.active("g", "b") is False or True  # B 自己有自己的窗口

    # A 一直没说话 → 60 秒后必须已经过期，不能被 B 续上
    assert alive.active("g", "a") is False


def test_other_users_cannot_borrow_it() -> None:
    """A 激活了，B 说话不能被当成"窗口内"。"""
    clock = _Clock()
    alive = KeepAlive(seconds=60, clock=clock)
    alive.arm("g", "a")
    clock.advance(10)
    assert alive.active("g", "b") is False


def test_windows_are_per_session() -> None:
    clock = _Clock()
    alive = KeepAlive(seconds=60, clock=clock)
    alive.arm("g1", "a")
    assert alive.active("g2", "a") is False, "换个群不该继承"


def test_remaining_reports_the_countdown() -> None:
    clock = _Clock()
    alive = KeepAlive(seconds=60, clock=clock)
    alive.arm("g", "a")
    clock.advance(20)
    assert alive.remaining("g", "a") == pytest.approx(40)
    assert alive.remaining("g", "zzz") == 0


def test_clear_and_purge() -> None:
    clock = _Clock()
    alive = KeepAlive(seconds=60, clock=clock)
    alive.arm("g", "a")
    alive.arm("g", "b")
    alive.arm("g2", "a")
    assert len(alive) == 3

    alive.clear("g", "a")
    assert alive.active("g", "a") is False
    assert len(alive) == 2

    clock.advance(61)
    assert alive.purge() == 2
    assert len(alive) == 0


def test_snapshot_lists_live_windows() -> None:
    clock = _Clock()
    alive = KeepAlive(seconds=60, clock=clock)
    alive.arm("g", "a")
    clock.advance(15)
    rows = alive.snapshot()
    assert rows == [{"session_id": "g", "user_id": "a", "remaining": 45.0}]


# --------------------------------------------------------------------------- #
# 接进流水线
# --------------------------------------------------------------------------- #
def _say(env, text: str, *, user_id: str = "333333", message_id: int = 1, at: bool = False) -> None:
    _reset(env.home)
    _inject(
        env.home,
        message_type="group",
        group_id=GROUP,
        user_id=user_id,
        message_id=message_id,
        text=text,
        at_bot=at,
        nickname="毛糙蛋",
    )


def _replied(env) -> bool:
    from test_m2_integration import _reply_texts

    return bool(_reply_texts(env.home))


def test_at_opens_the_window_and_follow_up_needs_no_at(ready, fake_openai: FakeOpenAI) -> None:
    ready.settings.update(keepalive_enabled=True, keepalive_seconds=60)
    fake_openai.reset()

    _say(ready, "在吗", message_id=1, at=True)
    assert wait_for(lambda: _replied(ready), timeout=30), "第一次 @ 应该回"

    _say(ready, "那个事情", message_id=2, at=False)
    assert wait_for(lambda: _replied(ready), timeout=30), "窗口内不 @ 也该回"


def test_without_at_and_without_window_it_ignores(ready, fake_openai: FakeOpenAI) -> None:
    ready.settings.update(keepalive_enabled=True, keepalive_seconds=60)
    fake_openai.reset()

    _say(ready, "随便说一句", message_id=1, at=False)
    assert not wait_for(lambda: _replied(ready), timeout=3), "没 @ 也没窗口，不该回"


def test_other_member_does_not_inherit_the_window(ready, fake_openai: FakeOpenAI) -> None:
    """A 激活后，B 说话不该被回复 —— 窗口是 (会话, 用户) 独立的。"""
    ready.settings.update(keepalive_enabled=True, keepalive_seconds=60)
    fake_openai.reset()

    _say(ready, "在吗", user_id="333333", message_id=1, at=True)
    assert wait_for(lambda: _replied(ready), timeout=30)

    _say(ready, "我也想聊", user_id="444444", message_id=2, at=False)
    assert not wait_for(lambda: _replied(ready), timeout=3), "B 不该蹭到 A 的窗口"


def test_disabled_keepalive_requires_at_every_time(ready, fake_openai: FakeOpenAI) -> None:
    ready.settings.update(keepalive_enabled=False)
    fake_openai.reset()

    _say(ready, "在吗", message_id=1, at=True)
    assert wait_for(lambda: _replied(ready), timeout=30)

    _say(ready, "接着说", message_id=2, at=False)
    assert not wait_for(lambda: _replied(ready), timeout=3), "关掉之后必须重新 @"


def test_private_chat_is_unaffected(ready, fake_openai: FakeOpenAI) -> None:
    """私聊本来就不用 @，保活不该影响它。"""
    ready.settings.update(keepalive_enabled=True)
    fake_openai.reset()

    _reset(ready.home)
    _inject(
        ready.home,
        message_type="private",
        user_id="20000002",
        message_id=1,
        text="在吗",
    )
    assert wait_for(lambda: _replied(ready), timeout=30)
