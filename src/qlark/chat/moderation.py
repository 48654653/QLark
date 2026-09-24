"""风控：发送速率限制与敏感词过滤（需求规格 v1.1 §9.3）。

为什么要有这两样：

* **速率限制** —— QQ 对"短时间内高频回复"非常敏感，被判定为机器人行为的账号
  轻则被限流、重则被临时冻结。一个人自言自语刷屏时，机器人跟着刷是最危险的。
* **敏感词过滤** —— 需求里写的是"可选"。默认关闭（空列表），因为强行替用户
  决定哪些词不能说很容易误伤；打开了就按用户给的清单执行。

两者都**只做拦截与记录，不做任何"聪明"的改写** —— 改写会让人以为机器人认同
了某句话，比直接不理更糟。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Iterable

#: 速率窗口
WINDOW_SECONDS = 60.0


class RateLimiter:
    """每会话滑动窗口限流。

    "每会话"而不是"全局"：一个群刷屏不该把另一个群的正常对话一起饿死。
    """

    def __init__(self, *, limit: int = 12, window: float = WINDOW_SECONDS) -> None:
        self.limit = max(0, int(limit))
        self.window = float(window)
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def check(self, session_id: str, *, now: float | None = None) -> tuple[bool, int]:
        """记录一次发送。

        Returns:
            ``(是否放行, 该会话在这一分钟里已发出的条数)``。
        """
        if not self.enabled:
            return True, 0
        moment = time.monotonic() if now is None else float(now)
        with self._lock:
            bucket = self._hits.setdefault(session_id, deque())
            cutoff = moment - self.window
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False, len(bucket)
            bucket.append(moment)
            return True, len(bucket)

    def retry_after(self, session_id: str, *, now: float | None = None) -> float:
        """还要等多少秒才可能放行（用于日志与提示）。"""
        if not self.enabled:
            return 0.0
        moment = time.monotonic() if now is None else float(now)
        with self._lock:
            bucket = self._hits.get(session_id)
            if not bucket:
                return 0.0
            cutoff = moment - self.window
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) < self.limit:
                return 0.0
            return max(0.0, bucket[0] + self.window - moment)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {key: len(value) for key, value in self._hits.items() if value}


def find_blocked_word(text: str, words: Iterable[str]) -> str | None:
    """返回命中的第一个敏感词；没有命中返回 ``None``。

    大小写不敏感（英文词），中文本来就无所谓大小写。
    """
    haystack = str(text or "").lower()
    if not haystack:
        return None
    for word in words:
        needle = str(word or "").strip().lower()
        if needle and needle in haystack:
            return str(word)
    return None


def normalize_words(raw: object) -> list[str]:
    """把界面上传来的敏感词归一化成干净的列表。

    界面上是一行一个，但也容忍逗号 / 顿号分隔 —— 用户很自然会那么写。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = raw.replace("，", ",").replace("、", ",").replace("\n", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        parts = []
        for item in raw:
            parts.extend(str(item).replace("，", ",").replace("、", ",").split(","))
    else:
        parts = [str(raw)]

    seen: list[str] = []
    for part in parts:
        word = part.strip()
        if word and word not in seen:
            seen.append(word)
    return seen[:200]  # 上限：防止有人贴一整本词典进来


__all__ = [
    "WINDOW_SECONDS",
    "RateLimiter",
    "find_blocked_word",
    "normalize_words",
]
