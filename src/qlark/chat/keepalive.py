"""@ 激活保活：被 @ 之后的一段时间内，那个人不必再 @ 也能继续对话。

为什么需要它：群聊里"每说一句都得 @ 一次"非常烦人，真人聊天不会这样。
@ 一次之后给 60 秒的窗口，期间他正常说话机器人就接着聊；他停下来满 60 秒，
窗口自动关闭，之后又要 @ 才会理他。

.. important::
   **窗口是按 (会话, 用户) 独立的。**

   这一点是硬要求：如果做成"整个群一个窗口"，那么只要群里一直有人说话，
   窗口就永远不关，机器人会变成每句都插嘴 —— 那比"每句都要 @"更糟。

   所以别人说话**既不能续期，也不能蹭用**你的窗口。

实现上只是内存里的一个字典（``键 → 到期时刻``），不落库：
重启之后窗口消失，最多是"要求再 @ 一次"，不影响正确性。
"""

from __future__ import annotations

import threading
import time
from typing import Callable

#: 窗口默认时长（秒）
DEFAULT_SECONDS = 60


class KeepAlive:
    """记录"谁在什么时候激活过"，并判断窗口是否还有效。"""

    def __init__(
        self,
        *,
        seconds: float = DEFAULT_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._seconds = max(1.0, float(seconds))
        #: 注入时钟便于测试（默认用单调时钟，不受系统时间调整影响）
        self._clock = clock or time.monotonic
        self._deadlines: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 配置
    @property
    def seconds(self) -> float:
        return self._seconds

    def set_seconds(self, seconds: float) -> None:
        self._seconds = max(1.0, float(seconds))

    # ------------------------------------------------------------------ 状态
    def arm(self, session_id: str, user_id: str) -> None:
        """开窗 / 续期：被 @ 的时候调。"""
        self.touch(session_id, user_id)

    def touch(self, session_id: str, user_id: str) -> None:
        """把到期时刻推到 now + 时长。**窗口内每说一句话都要调它。**"""
        key = (str(session_id), str(user_id))
        now = self._clock()
        with self._lock:
            self._deadlines[key] = now + self._seconds

    def active(self, session_id: str, user_id: str) -> bool:
        """这个人的窗口还有效吗？过期的条目顺手清掉。"""
        key = (str(session_id), str(user_id))
        now = self._clock()
        with self._lock:
            deadline = self._deadlines.get(key)
            if deadline is None:
                return False
            if now >= deadline:
                # 静默期满 = 自动关闭
                self._deadlines.pop(key, None)
                return False
            return True

    def remaining(self, session_id: str, user_id: str) -> float:
        """窗口还剩多少秒（没用或已过期返回 0）。"""
        key = (str(session_id), str(user_id))
        now = self._clock()
        with self._lock:
            deadline = self._deadlines.get(key)
            if deadline is None:
                return 0.0
            return max(0.0, deadline - now)

    def clear(self, session_id: str | None = None, user_id: str | None = None) -> None:
        """清窗口。两个参数都给 = 清某个人；只给会话 = 清整个会话；都不给 = 全清。"""
        with self._lock:
            if session_id is None:
                self._deadlines.clear()
                return
            if user_id is None:
                for key in [k for k in self._deadlines if k[0] == session_id]:
                    self._deadlines.pop(key, None)
                return
            self._deadlines.pop((str(session_id), str(user_id)), None)

    def purge(self) -> int:
        """清掉所有已过期的条目，返回清掉的个数（定期调用防止字典无限增长）。"""
        now = self._clock()
        with self._lock:
            stale = [key for key, deadline in self._deadlines.items() if now >= deadline]
            for key in stale:
                self._deadlines.pop(key, None)
            return len(stale)

    def __len__(self) -> int:
        with self._lock:
            return len(self._deadlines)

    def snapshot(self) -> list[dict[str, object]]:
        """给界面 / 调试看的当前窗口列表。"""
        now = self._clock()
        with self._lock:
            return [
                {
                    "session_id": key[0],
                    "user_id": key[1],
                    "remaining": round(max(0.0, deadline - now), 1),
                }
                for key, deadline in self._deadlines.items()
                if deadline > now
            ]


__all__ = ["DEFAULT_SECONDS", "KeepAlive"]
