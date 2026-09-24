"""去重与每会话串行队列的测试。"""

from __future__ import annotations

import threading
import time

from conftest import wait_for
from qlark.chat.dedup import MessageDedup
from qlark.chat.queue import SessionQueue


# --------------------------------------------------------------------------- #
# 去重
# --------------------------------------------------------------------------- #
def test_dedup_flags_repeats() -> None:
    dedup = MessageDedup(window_seconds=60)
    assert dedup.seen("a") is False
    assert dedup.seen("a") is True
    assert dedup.seen("b") is False


def test_dedup_empty_key_is_never_a_duplicate() -> None:
    dedup = MessageDedup(window_seconds=60)
    assert dedup.seen("") is False
    assert dedup.seen("") is False


def test_dedup_expires_old_entries() -> None:
    dedup = MessageDedup(window_seconds=0.2)
    assert dedup.seen("a") is False
    time.sleep(0.3)
    assert dedup.seen("a") is False, "超出时间窗后应视为新消息"


def test_dedup_respects_capacity() -> None:
    dedup = MessageDedup(window_seconds=3600, max_size=3)
    for index in range(6):
        dedup.seen(f"k{index}")
    assert len(dedup) <= 4


# --------------------------------------------------------------------------- #
# 会话队列
# --------------------------------------------------------------------------- #
def test_same_session_runs_serially() -> None:
    """同一会话的任务绝不能并发，否则记忆会错乱。"""
    queue = SessionQueue(max_workers=4)
    try:
        active = 0
        max_active = 0
        lock = threading.Lock()
        done: list[int] = []

        def task(index: int) -> None:
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.08)
            with lock:
                active -= 1
                done.append(index)

        for index in range(5):
            assert queue.submit("group:1", lambda i=index: task(i)) is True

        assert wait_for(lambda: len(done) == 5, timeout=10)
        assert max_active == 1, "同一会话出现了并发执行"
        assert done == [0, 1, 2, 3, 4], "同一会话必须按提交顺序执行"
    finally:
        queue.shutdown()


def test_different_sessions_run_in_parallel() -> None:
    queue = SessionQueue(max_workers=4)
    try:
        active = 0
        max_active = 0
        lock = threading.Lock()
        started = threading.Event()

        def task() -> None:
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
                if active >= 3:
                    started.set()
            time.sleep(0.25)
            with lock:
                active -= 1

        for index in range(3):
            queue.submit(f"group:{index}", task)

        assert started.wait(timeout=5), "不同会话应当能并发"
        assert max_active >= 2
    finally:
        queue.shutdown()


def test_global_concurrency_is_capped() -> None:
    queue = SessionQueue(max_workers=2)
    try:
        active = 0
        max_active = 0
        lock = threading.Lock()

        def task() -> None:
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.15)
            with lock:
                active -= 1

        for index in range(6):
            queue.submit(f"group:{index}", task)

        assert wait_for(lambda: queue.pending_count == 0 and queue.running_count == 0, timeout=15)
        assert max_active <= 2, f"并发上限被突破：{max_active}"
    finally:
        queue.shutdown()


def test_queue_drops_when_session_backs_up() -> None:
    """同一会话积压过多时应当丢弃并回调，而不是无限增长。"""
    dropped: list[tuple[str, int]] = []
    queue = SessionQueue(max_workers=1, max_pending_per_session=2, on_drop=lambda s, n: dropped.append((s, n)))
    try:
        started = threading.Event()
        gate = threading.Event()

        def slow() -> None:
            started.set()
            gate.wait(timeout=5)

        assert queue.submit("group:1", slow) is True
        # 必须等第一个任务真的被取走再往下提交。
        # 工作线程的启动是有延迟的：如果它还没取走 slow，积压计数会把这它也算进去，
        # 于是第 3 次提交就会因为"已满"被丢弃 —— 那样测试就变成了看运气。
        assert started.wait(timeout=5), "第一个任务应当已经开始执行"

        assert queue.submit("group:1", lambda: None) is True
        assert queue.submit("group:1", lambda: None) is True
        assert queue.submit("group:1", lambda: None) is False, "超额应当被丢弃"
        assert dropped and dropped[0][0] == "group:1"
        gate.set()
    finally:
        queue.shutdown()


def test_task_exception_does_not_kill_the_worker() -> None:
    queue = SessionQueue(max_workers=2)
    try:
        done = threading.Event()

        def boom() -> None:
            raise RuntimeError("故意炸一下")

        queue.submit("group:1", boom)
        queue.submit("group:1", done.set)
        assert done.wait(timeout=5), "一个任务抛异常后，后续任务仍应执行"
    finally:
        queue.shutdown()


def test_shutdown_is_idempotent() -> None:
    queue = SessionQueue(max_workers=2)
    queue.start()
    queue.shutdown()
    queue.shutdown()
