"""每会话串行、全局并发受限的任务调度。

为什么必须按会话串行：一轮回复要"读上下文 → 调模型 → 写回记忆"。
如果同一个群里两条消息并发处理，两边都会在对方写回之前读到旧历史，
结果是**记忆错乱**（两条回复各自看不到对方）。

全局并发上限则防止几十个群同时触发时把 API 打爆或被限流。
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Callable

log = logging.getLogger(__name__)

#: 单个会话最多积压多少条待处理消息，超出就丢弃并回调通知
DEFAULT_MAX_PENDING = 8


class SessionQueue:
    """把任务按 ``session_id`` 串行化，同时限制全局并发。"""

    def __init__(
        self,
        max_workers: int = 4,
        *,
        max_pending_per_session: int = DEFAULT_MAX_PENDING,
        on_drop: Callable[[str, int], None] | None = None,
    ) -> None:
        self.max_workers = max(1, int(max_workers))
        self.max_pending_per_session = max(1, int(max_pending_per_session))
        self._on_drop = on_drop

        self._pending: dict[str, deque[Callable[[], None]]] = {}
        self._active: set[str] = set()
        self._running = 0
        self._closed = False
        self._cond = threading.Condition(threading.RLock())
        self._workers: list[threading.Thread] = []
        self._started = False

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        with self._cond:
            if self._started:
                return
            self._started = True
            self._closed = False
            for index in range(self.max_workers):
                thread = threading.Thread(
                    target=self._worker_loop,
                    name=f"qlark-worker-{index}",
                    daemon=True,
                )
                thread.start()
                self._workers.append(thread)

    def shutdown(self, *, wait: bool = True) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        if not wait:
            return
        for thread in list(self._workers):
            if thread.is_alive():
                thread.join(timeout=5)
        self._workers.clear()
        with self._cond:
            self._started = False
            self._pending.clear()
            self._active.clear()

    # ------------------------------------------------------------------ 提交
    def submit(self, session_id: str, task: Callable[[], None]) -> bool:
        """投递任务。返回 ``False`` 表示该会话积压过多、本次被丢弃。"""
        with self._cond:
            if self._closed:
                return False
            if not self._started:
                self.start()
            bucket = self._pending.setdefault(session_id, deque())
            if len(bucket) >= self.max_pending_per_session:
                dropped = len(bucket)
                if self._on_drop is not None:
                    try:
                        self._on_drop(session_id, dropped)
                    except Exception:
                        log.exception("on_drop 回调异常")
                return False
            bucket.append(task)
            self._cond.notify_all()
            return True

    # ------------------------------------------------------------------ 观测
    @property
    def pending_count(self) -> int:
        with self._cond:
            return sum(len(bucket) for bucket in self._pending.values())

    @property
    def running_count(self) -> int:
        with self._cond:
            return self._running

    # ------------------------------------------------------------------ 工作线程
    def _pickable(self) -> bool:
        if self._running >= self.max_workers:
            return False
        return any(session not in self._active for session in self._pending)

    def _pick_session(self) -> str | None:
        for session in list(self._pending):
            if session not in self._active:
                return session
        return None

    def _worker_loop(self) -> None:
        while True:
            with self._cond:
                while not self._closed and not self._pickable():
                    self._cond.wait()
                session = self._pick_session()
                if session is None:
                    if self._closed:
                        return
                    # 所有待处理会话都在运行中，等其他线程结束
                    self._cond.wait(timeout=0.5)
                    continue
                bucket = self._pending[session]
                task = bucket.popleft()
                if not bucket:
                    del self._pending[session]
                self._active.add(session)
                self._running += 1

            try:
                task()
            except Exception:
                log.exception("会话任务执行异常 session=%s", session)
            finally:
                with self._cond:
                    self._active.discard(session)
                    self._running -= 1
                    self._cond.notify_all()


__all__ = ["DEFAULT_MAX_PENDING", "SessionQueue"]
