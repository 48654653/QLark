"""对话记忆的落库与人设读取。

记忆键 = ``<session_id>:p<persona_uid>``，满足需求规格 v1.1 §6.1：

* 群聊内所有人共用一份（同一 ``group:<群号>``）
* 不同人设不互通（键里带 ``p<uid>``）
* 不同群互不干扰（键里带群号）
* 私聊按用户隔离
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable

from ..storage.db import Database

log = logging.getLogger(__name__)

#: 出厂默认人设的内部 uid（不可删除，见需求规格 §5.1）
DEFAULT_PERSONA_UID = 1


def memory_key(session_id: str, persona_uid: int) -> str:
    return f"{session_id}:p{int(persona_uid)}"


class MessageStore:
    """对 ``messages`` / ``summaries`` / ``personas`` / ``session_state`` 的读写门面。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------ 人设
    def ensure_default_persona(self) -> int:
        """保证"人设1"存在（出厂默认、不可删除）。返回其 uid。"""
        rows = self._db.query(
            "SELECT uid FROM personas WHERE uid = ?", (DEFAULT_PERSONA_UID,)
        )
        if rows:
            return DEFAULT_PERSONA_UID
        self._db.execute(
            "INSERT INTO personas(uid, slot, name, enabled, system_prompt, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (DEFAULT_PERSONA_UID, 1, "人设1", 1, "", int(time.time())),
        )
        log.info("已创建出厂默认人设（uid=%s）", DEFAULT_PERSONA_UID)
        return DEFAULT_PERSONA_UID

    def persona(self, uid: int) -> dict[str, Any] | None:
        rows = self._db.query("SELECT * FROM personas WHERE uid = ?", (int(uid),))
        return dict(rows[0]) if rows else None

    def persona_prompt(self, uid: int) -> str:
        """人设的系统提示词。**未配置时返回空串**（此时不注入 system 消息，
        而不是发一条空的 system —— 部分 OpenAI 兼容服务会因此报错）。"""
        row = self.persona(uid)
        if not row:
            return ""
        return str(row.get("system_prompt") or "").strip()

    def list_personas(self) -> list[dict[str, Any]]:
        rows = self._db.query("SELECT * FROM personas ORDER BY slot, uid")
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ 会话
    def session_persona(
        self,
        session_id: str,
        *,
        session_type: str,
        session_ref: str,
    ) -> int:
        """取该会话当前生效的人设 uid；没有就按默认人设建一行。

        需求规格 §5.2：**人设作用域是会话独立的** —— 每个群、每个私聊用户
        各自记住自己当前用的人设。
        """
        rows = self._db.query(
            "SELECT active_persona FROM session_state WHERE session_id = ?", (session_id,)
        )
        if rows:
            return int(rows[0]["active_persona"])
        uid = self.ensure_default_persona()
        self._db.execute(
            "INSERT INTO session_state(session_id, session_type, session_ref, active_persona, updated_at) "
            "VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO NOTHING",
            (session_id, session_type, session_ref, uid, int(time.time())),
        )
        return uid

    def set_session_persona(
        self,
        session_id: str,
        persona_uid: int,
        *,
        session_type: str | None = None,
        session_ref: str | None = None,
    ) -> None:
        """设置会话当前人设。

        传了 ``session_type`` / ``session_ref`` 时是 **UPSERT** —— 否则对于一个还
        没有 ``session_state`` 行的会话（例如从没说过话的私聊），UPDATE 会**静默
        匹配 0 行**，用户看到"已切换"但实际没生效。
        """
        now = int(time.time())
        if session_type is None or session_ref is None:
            self._db.execute(
                "UPDATE session_state SET active_persona = ?, updated_at = ? WHERE session_id = ?",
                (int(persona_uid), now, session_id),
            )
            return
        self._db.execute(
            "INSERT INTO session_state(session_id, session_type, session_ref, active_persona, updated_at) "
            "VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "  active_persona = excluded.active_persona, "
            "  updated_at     = excluded.updated_at",
            (session_id, session_type, str(session_ref), int(persona_uid), now),
        )

    # ------------------------------------------------------------------ 消息
    def append(
        self,
        key: str,
        role: str,
        content: str,
        *,
        has_image: bool = False,
        tokens: int = 0,
        msg_id: str | None = None,
        created_at: int | None = None,
    ) -> int:
        self._db.execute(
            "INSERT INTO messages(memory_key, role, content, has_image, tokens, msg_id, created_at, summarized) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, 0)",
            (
                key,
                role,
                content,
                1 if has_image else 0,
                int(tokens),
                msg_id,
                int(created_at if created_at is not None else time.time()),
            ),
        )
        rows = self._db.query("SELECT last_insert_rowid() AS id")
        return int(rows[0]["id"]) if rows else 0

    def recent_rounds(self, key: str, rounds: int) -> list[tuple[str, str]]:
        """取最近 ``rounds`` 轮原文，按时间正序返回 ``(role, content)`` 列表。"""
        if rounds <= 0:
            return []
        rows = self._db.query(
            "SELECT role, content FROM messages WHERE memory_key = ? ORDER BY id DESC LIMIT ?",
            (key, int(rounds) * 2),
        )
        return [(str(row["role"]), str(row["content"])) for row in reversed(rows)]

    def message_count(self, key: str) -> int:
        rows = self._db.query(
            "SELECT COUNT(*) AS n FROM messages WHERE memory_key = ?", (key,)
        )
        return int(rows[0]["n"]) if rows else 0

    def clear(self, key: str) -> int:
        """清空某个记忆键的全部原文与摘要，返回删除的原文条数。"""
        rows = self._db.query(
            "SELECT COUNT(*) AS n FROM messages WHERE memory_key = ?", (key,)
        )
        count = int(rows[0]["n"]) if rows else 0
        self._db.execute("DELETE FROM messages WHERE memory_key = ?", (key,))
        self._db.execute("DELETE FROM summaries WHERE memory_key = ?", (key,))
        return count

    def known_memory_keys(self) -> Iterable[str]:
        rows = self._db.query("SELECT DISTINCT memory_key FROM messages")
        return [str(row["memory_key"]) for row in rows]


__all__ = ["DEFAULT_PERSONA_UID", "MessageStore", "memory_key"]
