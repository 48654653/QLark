"""把连续发来的碎片消息合并成一条。

为什么需要它：有人习惯一句话拆成好几条发 —— "在" "吗" "问个事" 一条一条来。
如果每条都回，就会变成「咋了 / 嗯？ / 说」三连，既费 token 又一眼是机器人。

做法是**静默合并**（debounce）：
  · 收到第一条就开始收集，同时开始计时
  · 期间每来一条，全部并进来，**计时重置**
  · 对方停下 ``quiet_seconds`` 秒 → 把这一批合并成一条送出去

关键是有个**硬上限** ``max_seconds``：从第一条算起最多等这么久。
否则遇到一直打字不停的人，机器人会永远不吭声。

.. note::
   为什么不用"每 15 秒轮询一次"：那样单条消息的延迟取决于它恰好落在哪个
   格子里（0～15 秒随机），同样的话忽快忽慢，比稳定偏慢更假。静默合并的
   延迟是固定的 —— 对方一停就发，通常 3 秒左右，正好是"看一眼、想一想、
   打字"的时间。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)


@dataclass
class _Entry:
    items: list[Any] = field(default_factory=list)
    #: 这一批里**第一条**到达的时刻，硬上限从这里算
    first_at: float = 0.0
    timer: threading.Timer | None = None


class Coalescer:
    """按 ``(会话, 用户)`` 收集消息，静默后回调。

    Args:
        quiet_seconds: 静默多久算"说完了"。
        max_seconds: 从第一条算起的硬上限，防止对方一直打字导致永远不回。
        on_flush: ``on_flush(key, items)``，在**定时器线程**里被调用，
            实现方必须自己保证线程安全。
    """

    def __init__(
        self,
        *,
        quiet_seconds: float = 3.0,
        max_seconds: float = 15.0,
        on_flush: Callable[[str, list[Any]], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.quiet_seconds = max(0.05, float(quiet_seconds))
        self.max_seconds = max(self.quiet_seconds, float(max_seconds))
        self._on_flush = on_flush
        self._clock = clock or time.monotonic
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 配置
    def configure(self, *, quiet_seconds: float, max_seconds: float) -> None:
        """就地改参数（配置改了不用重启）。已经在收集的批次按新参数重排。"""
        with self._lock:
            self.quiet_seconds = max(0.05, float(quiet_seconds))
            self.max_seconds = max(self.quiet_seconds, float(max_seconds))
            for key, entry in self._entries.items():
                self._reschedule(key, entry)

    # ------------------------------------------------------------------ 收集
    def add(self, key: str, item: Any) -> int:
        """往这个人的缓冲里加一条，返回**这一批现在有几条**。"""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _Entry(first_at=self._clock())
                self._entries[key] = entry
            entry.items.append(item)
            count = len(entry.items)
            self._reschedule(key, entry)
            return count

    def count(self, key: str) -> int:
        with self._lock:
            entry = self._entries.get(key)
            return len(entry.items) if entry else 0

    def take(self, key: str) -> list[Any]:
        """取走这个人的缓冲并停掉计时（不触发回调）。

        用在"对方突然 @ 了机器人"的时候：把攒着的碎片一起吸收进这一轮，
        免得先回一个"嗯？"再回正式答案。
        """
        with self._lock:
            entry = self._entries.pop(key, None)
            if entry is None:
                return []
            if entry.timer is not None:
                entry.timer.cancel()
                entry.timer = None
            return entry.items

    def discard(self, key: str) -> int:
        """丢掉一个人的缓冲，返回丢掉几条。暂停 / 退出时用。"""
        return len(self.take(key))

    def discard_all(self) -> int:
        """丢掉所有缓冲并停掉所有计时。返回总共丢掉几条。"""
        with self._lock:
            keys = list(self._entries)
        total = 0
        for key in keys:
            total += len(self.take(key))
        return total

    @property
    def keys(self) -> list[str]:
        with self._lock:
            return list(self._entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    # ------------------------------------------------------------------ 内部
    def _reschedule(self, key: str, entry: _Entry) -> None:
        """重排计时器：取"静默到期"和"硬上限到期"里更早的那个。"""
        now = self._clock()
        deadline = min(now + self.quiet_seconds, entry.first_at + self.max_seconds)
        delay = max(0.01, deadline - now)
        if entry.timer is not None:
            entry.timer.cancel()
        timer = threading.Timer(delay, self._fire, args=(key,))
        timer.daemon = True
        entry.timer = timer
        timer.start()

    def _fire(self, key: str) -> None:
        """定时器到期：把整批交出去。**在定时器线程里跑。**"""
        items = self.take(key)
        if not items:
            return
        if self._on_flush is None:
            return
        try:
            self._on_flush(key, items)
        except Exception:  # noqa: BLE001 定时器线程里绝不能把异常抛出去
            log.exception("合并批次回调失败（key=%s）", key)


__all__ = ["Coalescer"]
