"""消息去重。

NapCat 在重连、重试或进程重启的边界上**可能把同一条消息推两次**，
不去重就会出现"机器人对同一句话回两遍"。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict


class MessageDedup:
    """按消息 ID 去重，带时间窗与容量上限。"""

    def __init__(self, window_seconds: float = 600.0, max_size: int = 4000) -> None:
        self.window_seconds = float(window_seconds)
        self.max_size = int(max_size)
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._seen:
            key, stamp = next(iter(self._seen.items()))
            if stamp >= cutoff and len(self._seen) <= self.max_size:
                break
            self._seen.pop(key, None)

    def seen(self, key: str) -> bool:
        """记录 ``key``；若之前已经出现过则返回 ``True``（表示这是重复消息）。"""
        if not key:
            return False
        now = time.time()
        with self._lock:
            self._evict(now)
            duplicate = key in self._seen
            self._seen[key] = now
            self._seen.move_to_end(key)
            return duplicate

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)


__all__ = ["MessageDedup"]
