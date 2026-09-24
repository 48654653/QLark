"""日志总线：内存环形缓冲 + 落盘。

为什么需要它：
NapCat 的启动输出包含大幅二维码字符画，**必须被持续消费**——管道缓冲区写满后
进程会直接阻塞（技术文档坑 23）。所以所有 stdout 都先进这里，再由界面按需拉取。

前端不是逐行推送的：界面通过 :meth:`LogBus.since` 按 ``seq`` 增量拉取，
避免每来一行就调一次 ``evaluate_js``。
"""

from __future__ import annotations

import base64
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from ..core.models import Level, LogLine, LogSource
from . import patterns

_MAX_BYTES_DEFAULT = 8 * 1024 * 1024
_KEEP_DAYS_DEFAULT = 7


class _FileSink:
    """按天分文件、按大小轮转的简易日志落盘器。"""

    def __init__(
        self,
        directory: Path,
        *,
        prefix: str = "qlark",
        max_bytes: int = _MAX_BYTES_DEFAULT,
        keep_days: int = _KEEP_DAYS_DEFAULT,
    ) -> None:
        self.directory = directory
        self.prefix = prefix
        self.max_bytes = max_bytes
        self.keep_days = keep_days
        self._lock = threading.Lock()
        self._handle = None
        self._day = ""
        self._size = 0
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.directory = Path(".")  # 极端情况下退化为当前目录，不阻塞主流程

    # ------------------------------------------------------------------ 内部
    def _path_for(self, day: str) -> Path:
        return self.directory / f"{self.prefix}-{day}.log"

    def _open(self, day: str) -> None:
        self._close()
        self._day = day
        path = self._path_for(day)
        try:
            self._handle = open(path, "a", encoding="utf-8", errors="replace")
            self._size = path.stat().st_size if path.exists() else 0
        except OSError:
            self._handle = None

    def _close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None

    def _rotate(self) -> None:
        self._close()
        path = self._path_for(self._day)
        try:
            backup = path.with_suffix(path.suffix + ".1")
            if backup.exists():
                backup.unlink()
            if path.exists():
                path.rename(backup)
        except OSError:
            pass

    def _cleanup(self) -> None:
        cutoff = time.time() - self.keep_days * 86400
        try:
            for item in self.directory.glob(f"{self.prefix}-*.log*"):
                try:
                    if item.stat().st_mtime < cutoff:
                        item.unlink()
                except OSError:
                    continue
        except OSError:
            pass

    # ------------------------------------------------------------------ 对外
    def write(self, ts: float, level: str, source: str, text: str) -> None:
        day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        stamp = datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]
        line = f"{stamp}\t{level.upper()}\t{source}\t{text}\n"
        encoded_len = len(line.encode("utf-8", errors="replace"))

        with self._lock:
            if self._handle is None or day != self._day:
                self._open(day)
                self._cleanup()
            if self._handle is None:
                return
            if self._size + encoded_len > self.max_bytes:
                self._rotate()
                self._open(day)
                if self._handle is None:
                    return
            try:
                self._handle.write(line)
                self._handle.flush()
                self._size += encoded_len
            except OSError:
                self._close()

    def close(self) -> None:
        with self._lock:
            self._close()


class LogBus:
    """线程安全的日志环形缓冲。"""

    def __init__(
        self,
        capacity: int = 5000,
        *,
        log_dir: Path | None = None,
        persist: bool = True,
    ) -> None:
        self.capacity = capacity
        self._lines: deque[LogLine] = deque(maxlen=capacity)
        self._seq = 0
        self._lock = threading.RLock()
        self._sink = _FileSink(log_dir) if (persist and log_dir is not None) else None
        self._listeners: list[Callable[[list[LogLine]], None]] = []

    # ------------------------------------------------------------------ 写入
    def add(
        self,
        text: str,
        *,
        level: str | None = None,
        source: str = LogSource.QLARK.value,
        ts: float | None = None,
    ) -> LogLine:
        """追加一行日志。``text`` 可以包含换行，会被拆成多行。"""
        first: LogLine | None = None
        for piece in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = self._add_one(piece, level=level, source=source, ts=ts)
            first = first or line
        assert first is not None
        return first

    def _add_one(self, text: str, *, level: str | None, source: str, ts: float | None) -> LogLine:
        moment = time.time() if ts is None else ts
        resolved_level = level or (
            patterns.parse_level(text) if source == LogSource.NAPCAT.value else Level.INFO.value
        )
        with self._lock:
            self._seq += 1
            line = LogLine(
                seq=self._seq,
                ts=moment,
                level=resolved_level,
                source=source,
                text=text,
            )
            self._lines.append(line)
        if self._sink is not None:
            self._sink.write(moment, resolved_level, source, text)
        return line

    def add_napcat(self, text: str) -> LogLine:
        return self.add(text, source=LogSource.NAPCAT.value)

    # ------------------------------------------------------------------ 读取
    def since(self, seq: int = 0, limit: int | None = None) -> list[LogLine]:
        """返回 ``seq`` 之后的日志行（``seq`` 是上一批的最后一个序号）。"""
        with self._lock:
            items = [line for line in self._lines if line.seq > seq]
        if limit is not None and len(items) > limit:
            items = items[-limit:]
        return items

    def tail(self, n: int = 200) -> list[LogLine]:
        with self._lock:
            return list(self._lines)[-n:]

    @property
    def latest_seq(self) -> int:
        with self._lock:
            return self._seq

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()

    def count(self) -> int:
        with self._lock:
            return len(self._lines)

    # ------------------------------------------------------------------ 订阅
    def subscribe(self, callback: Callable[[list[LogLine]], None]) -> Callable[[], None]:
        """注册批量回调，返回取消订阅函数。"""
        self._listeners.append(callback)

        def unsubscribe() -> None:
            try:
                self._listeners.remove(callback)
            except ValueError:
                pass

        return unsubscribe

    def flush_to_listeners(self, since_seq: int) -> int:
        """把 ``since_seq`` 之后的行推给订阅者，返回新的序号。"""
        lines = self.since(since_seq)
        if not lines:
            return since_seq
        for callback in list(self._listeners):
            try:
                callback(lines)
            except Exception:  # 订阅者异常不能影响日志主流程
                continue
        return lines[-1].seq

    # ------------------------------------------------------------------ 导出
    def export_text(self, lines: Iterable[LogLine] | None = None) -> str:
        items = list(lines) if lines is not None else self.tail(self.capacity)
        out: list[str] = []
        for line in items:
            stamp = datetime.fromtimestamp(line.ts).strftime("%Y-%m-%d %H:%M:%S")
            out.append(f"{stamp}\t{line.level.upper()}\t{line.source}\t{line.text}")
        return "\n".join(out)

    def close(self) -> None:
        if self._sink is not None:
            self._sink.close()


# --------------------------------------------------------------------------- #
# 二维码图片转 data URL（前端直接塞进 <img src>）
# --------------------------------------------------------------------------- #
def png_to_data_url(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


__all__ = ["LogBus", "png_to_data_url"]
