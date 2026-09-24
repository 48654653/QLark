"""SQLite 存储层。

设计要点：

* WAL 模式 + ``busy_timeout``，并发读写安全、断电不坏库。
* 单连接 + 线程锁。QLark 的写入量很低（每轮对话几条），单连接足够且避免
  多连接带来的 WAL 竞争。
* 所有 SQL 都走参数绑定，不做字符串拼接。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from ..core.models import Stats

log = logging.getLogger(__name__)


def local_midnight_ts(when: float | None = None) -> float:
    """本地时区当日 00:00 的 Unix 时间戳（统计跨天重置的依据）。"""
    moment = datetime.fromtimestamp(when) if when is not None else datetime.now()
    return datetime(moment.year, moment.month, moment.day).timestamp()


def today_str(when: float | None = None) -> str:
    moment = datetime.fromtimestamp(when) if when is not None else datetime.now()
    return moment.strftime("%Y-%m-%d")


def _load_schema_sql() -> str:
    try:  # 打包后走 importlib.resources 更可靠
        from importlib.resources import files

        return files("qlark.storage").joinpath("schema.sql").read_text(encoding="utf-8")
    except Exception:  # pragma: no cover - 开发期兜底
        return Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")


class Database:
    """QLark 的 SQLite 门面。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            isolation_level=None,  # 自己管事务
            timeout=10.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._apply_pragmas()
        self.init_schema()

    # ------------------------------------------------------------------ 基础
    def _apply_pragmas(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.close()

    def init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_load_schema_sql())

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:  # pragma: no cover
                pass

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ 查询
    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            try:
                return cur.fetchall()
            finally:
                cur.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            cur.close()

    def executemany(self, sql: str, seq: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            cur = self._conn.executemany(sql, [tuple(p) for p in seq])
            cur.close()

    # ------------------------------------------------------------------ 配置
    def get_config(self, key: str, default: Any = None) -> Any:
        rows = self.query("SELECT value FROM config WHERE key = ?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except (TypeError, ValueError):  # 数据损坏时退回默认值，不让界面崩
            log.warning("配置项 %s 解码失败，使用默认值", key)
            return default

    def set_config(self, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        self.execute(
            "INSERT INTO config(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, payload),
        )

    def update_config(self, mapping: dict[str, Any]) -> None:
        self.executemany(
            "INSERT INTO config(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ((k, json.dumps(v, ensure_ascii=False)) for k, v in mapping.items()),
        )

    def all_config(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for row in self.query("SELECT key, value FROM config"):
            try:
                result[row["key"]] = json.loads(row["value"])
            except (TypeError, ValueError):
                continue
        return result

    # ------------------------------------------------------------------ 统计
    def stats_today(self) -> Stats:
        day = today_str()
        rows = self.query("SELECT * FROM stats_daily WHERE date = ?", (day,))
        if not rows:
            return Stats(date=day, active_sessions=self.active_sessions_today())
        row = rows[0]
        return Stats(
            date=day,
            prompt_tokens=row["prompt_tokens"],
            completion_tokens=row["completion_tokens"],
            calls=row["calls"],
            errors=row["errors"],
            msg_in=row["msg_in"],
            msg_out=row["msg_out"],
            active_sessions=self.active_sessions_today(),
        )

    def bump_stats(
        self,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        calls: int = 0,
        errors: int = 0,
        msg_in: int = 0,
        msg_out: int = 0,
    ) -> None:
        """累加今日统计（跨天自动新建当日行）。"""
        day = today_str()
        self.execute(
            "INSERT INTO stats_daily(date, prompt_tokens, completion_tokens, calls, errors, msg_in, msg_out) "
            "VALUES(?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET "
            "  prompt_tokens     = prompt_tokens     + excluded.prompt_tokens, "
            "  completion_tokens = completion_tokens + excluded.completion_tokens, "
            "  calls             = calls             + excluded.calls, "
            "  errors            = errors            + excluded.errors, "
            "  msg_in            = msg_in            + excluded.msg_in, "
            "  msg_out           = msg_out           + excluded.msg_out",
            (day, prompt_tokens, completion_tokens, calls, errors, msg_in, msg_out),
        )

    # ------------------------------------------------------------------ 会话
    def touch_session(self, session_id: str, msg_count: int = 1) -> None:
        self.execute(
            "INSERT INTO session_activity(session_id, last_active, msg_count) VALUES(?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "  last_active = excluded.last_active, "
            "  msg_count   = msg_count + excluded.msg_count",
            (session_id, int(time.time()), msg_count),
        )

    def active_sessions_today(self) -> int:
        rows = self.query(
            "SELECT COUNT(*) AS n FROM session_activity WHERE last_active >= ?",
            (int(local_midnight_ts()),),
        )
        return int(rows[0]["n"]) if rows else 0
