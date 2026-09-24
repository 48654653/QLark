"""SQLite 存储层测试。"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from qlark.settings import Settings
from qlark.storage.db import Database, local_midnight_ts, today_str


def make_db(tmp_path: Path) -> Database:
    return Database(tmp_path / "qlark.db")


def test_schema_creates_all_tables(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        rows = db.query("SELECT name FROM sqlite_master WHERE type = 'table'")
        names = {row["name"] for row in rows}
        for expected in (
            "personas",
            "session_state",
            "messages",
            "summaries",
            "stats_daily",
            "config",
            "persona_whitelist",
            "session_activity",
        ):
            assert expected in names, f"缺少表 {expected}"
    finally:
        db.close()


def test_wal_mode_enabled(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        rows = db.query("PRAGMA journal_mode")
        assert rows[0][0].lower() == "wal"
    finally:
        db.close()


def test_config_round_trip_preserves_types(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        db.set_config("number", 42)
        db.set_config("text", "中文内容")
        db.set_config("flag", True)
        db.set_config("nothing", None)
        db.set_config("mapping", {"a": [1, 2, 3]})

        assert db.get_config("number") == 42
        assert db.get_config("text") == "中文内容"
        assert db.get_config("flag") is True
        assert db.get_config("nothing") is None
        assert db.get_config("mapping") == {"a": [1, 2, 3]}
        assert db.get_config("missing", "fallback") == "fallback"
    finally:
        db.close()


def test_config_upsert_overwrites(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        db.set_config("k", 1)
        db.set_config("k", 2)
        assert db.get_config("k") == 2
        assert len(db.query("SELECT 1 FROM config WHERE key = 'k'")) == 1
    finally:
        db.close()


def test_stats_accumulate_for_today(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        db.bump_stats(prompt_tokens=100, completion_tokens=50, calls=1, msg_in=1, msg_out=1)
        db.bump_stats(prompt_tokens=200, completion_tokens=80, calls=2, errors=1, msg_out=1)

        stats = db.stats_today()
        assert stats.prompt_tokens == 300
        assert stats.completion_tokens == 130
        assert stats.total_tokens == 430
        assert stats.calls == 3
        assert stats.errors == 1
        assert stats.msg_in == 1
        assert stats.msg_out == 2
        assert stats.date == today_str()
    finally:
        db.close()


def test_active_sessions_counts_only_today(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        db.touch_session("group:1")
        db.touch_session("group:1")
        db.touch_session("private:2")
        assert db.active_sessions_today() == 2

        # 把一个会话挪到昨天，就不该再被计入
        yesterday = int(local_midnight_ts() - 3600)
        db.execute(
            "UPDATE session_activity SET last_active = ? WHERE session_id = ?",
            (yesterday, "private:2"),
        )
        assert db.active_sessions_today() == 1
        # 计数通过 stats_today 一并带出
        assert db.stats_today().active_sessions == 1
    finally:
        db.close()


def test_local_midnight_is_today() -> None:
    midnight = local_midnight_ts()
    moment = datetime.fromtimestamp(midnight)
    assert (moment.hour, moment.minute, moment.second) == (0, 0, 0)
    assert midnight <= time.time()


def test_settings_defaults_and_types(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        settings = Settings(db)
        assert settings.theme == "dark"
        assert settings.onebot_host == "127.0.0.1"
        assert settings.onebot_port == 3000
        assert settings.qq_account is None
        assert settings.get("memory_rounds") == 20
        assert settings.get("auto_summary") is True

        settings.theme = "light"
        assert settings.theme == "light"
        assert db.get_config("theme") == "light"

        # 非法主题值要回落到 dark
        settings.set("theme", "rainbow")
        assert settings.theme == "dark"

        settings.qq_account = 123456
        assert settings.qq_account == "123456"

        settings.onebot_port = "4567"
        assert settings.onebot_port == 4567
        settings.set("onebot_port", "not-a-number")
        assert settings.onebot_port == 3000
    finally:
        db.close()


def test_settings_base_url(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        settings = Settings(db)
        settings.update(onebot_host="127.0.0.1", onebot_port=3999)
        assert settings.onebot_base_url == "http://127.0.0.1:3999"
    finally:
        db.close()
