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
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..core.models import Stats

log = logging.getLogger(__name__)

#: 建库之后要补齐的列（老库升级用）：表名 → [(列名, 列定义)]
#:
#: 只增不删、只补不改 —— 迁移一旦开始改类型或删列，就需要真正意义上的版本号了，
#: 而现在这个规模还不值得。
COLUMN_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "personas": [
        ("split_enabled", "INTEGER DEFAULT 0"),
        ("meme_enabled", "INTEGER DEFAULT 0"),
        # 下面这几个留空（NULL）表示"跟随全局设置"，见 settings.DEFAULTS
        ("split_chance", "REAL DEFAULT NULL"),
        ("split_delay_ms", "INTEGER DEFAULT NULL"),
        ("meme_chance", "REAL DEFAULT NULL"),
        # 自定义提示词；留空表示用内置的那套
        ("split_prompt", "TEXT DEFAULT NULL"),
        ("meme_prompt", "TEXT DEFAULT NULL"),
        # v2：语音回复。voice_prompt 是这个角色的音色描述，留空用全局默认
        ("voice_enabled", "INTEGER DEFAULT 0"),
        ("voice_prompt", "TEXT DEFAULT NULL"),
    ],
    "messages": [
        # v2：这一条附带的图片文件名（JSON 数组）。图片本身在 <数据目录>/media/，
        # 数据库里只记名字 —— base64 绝不入库。
        ("media", "TEXT DEFAULT NULL"),
    ],
}


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
        self._migrate_columns()

    def _migrate_columns(self) -> None:
        """给老库补上后加的列。

        ``schema.sql`` 全是 ``CREATE TABLE IF NOT EXISTS`` —— 表已经存在时它
        **什么都不会做**，新加的列也就永远不会出现。所以每次启动都对着
        ``PRAGMA table_info`` 检查一遍，缺哪个补哪个。``ALTER TABLE ADD COLUMN``
        是幂等安全的（缺了才加），已有数据也不会动。
        """
        for table, columns in COLUMN_MIGRATIONS.items():
            try:
                existing = {row["name"] for row in self.query(f"PRAGMA table_info({table})")}
            except sqlite3.Error:  # pragma: no cover - 表都读不到就没什么能做的
                continue
            if not existing:
                continue
            for name, ddl in columns:
                if name in existing:
                    continue
                try:
                    self.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                    log.info("已为老库补列：%s.%s", table, name)
                except sqlite3.Error:
                    log.exception("补列失败：%s.%s", table, name)

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

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """显式事务：块内所有写入要么全成、要么全不成。

        摘要压缩必须用它（需求规格 §6.3）："写新总结"和"把这 W 轮标记为已压缩"
        是一个原子操作。中途崩了如果只写了一半，就会出现"总结覆盖了这些轮次、
        但原文仍被当作未压缩"的错乱状态，或者更糟——原文被标记了却没进总结，
        那部分记忆就永久丢了。
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:  # pragma: no cover - 回滚都失败就没什么能做的了
                    log.exception("事务回滚失败")
                raise
            else:
                self._conn.execute("COMMIT")

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
