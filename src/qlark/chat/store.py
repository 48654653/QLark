"""对话记忆的落库与人设读取。

记忆键 = ``<session_id>:p<persona_uid>``，满足需求规格 v1.1 §6.1：

* 群聊内所有人共用一份（同一 ``group:<群号>``）
* 不同人设不互通（键里带 ``p<uid>``）
* 不同群互不干扰（键里带群号）
* 私聊按用户隔离
"""

from __future__ import annotations

import json
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
        media: Iterable[str] | None = None,
        video: str | None = None,
        tokens: int = 0,
        msg_id: str | None = None,
        created_at: int | None = None,
    ) -> int:
        """写一条记忆。

        Args:
            media: 这一条附带的图片**文件名**（不是完整路径，也不是 base64）。
                图片本身落在 ``<用户数据目录>/media/``，这里只记名字，
                上下文组装时按需读回来 —— **绝不能把 base64 写进数据库**。
            video: 这一条附带的**视频直链**（NapCat 给的 http(s) 地址）。
                只存一条、只回放最近那一条 —— 视频比图片贵得多
                （实测 1MB 短视频约 1100 token）。
        """
        names = [str(name) for name in (media or []) if name]
        url = str(video or "").strip() or None
        self._db.execute(
            "INSERT INTO messages(memory_key, role, content, has_image, media, video, "
            "tokens, msg_id, created_at, summarized) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (
                key,
                role,
                content,
                1 if has_image else 0,
                json.dumps(names, ensure_ascii=False) if names else None,
                url,
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

    def recent_rounds_with_media(
        self, key: str, rounds: int, media_rounds: int = 0
    ) -> list[tuple[str, str, list[str]]]:
        """同 :meth:`recent_rounds`，但**带上最近 ``media_rounds`` 轮的图片文件名**。

        v2 的做法：记忆里不再靠 ``[图片]`` 占位符表示"这里有过一张图"，
        而是把**真实的图**放回上下文。但历史全都带上图会让每轮请求都重发一遍
        （一张 512px 约 258 token），所以只回溯最近几轮 —— 更早的变成纯文本
        （占位符会被调用方去掉，不会留下"这里曾经有图"的空壳）。

        Returns:
            ``[(role, content, [文件名, ...])]``，按时间正序；没有图的条目是空列表。
        """
        rows = self.recent_rounds_with_media_and_video(key, rounds, media_rounds)
        return [(role, content, media) for role, content, media, _video, _at in rows]

    def recent_rounds_with_media_and_video(
        self, key: str, rounds: int, media_rounds: int = 0
    ) -> list[tuple[str, str, list[str], str | None, int]]:
        """在 :meth:`recent_rounds_with_media` 基础上再带上视频直链与时间。

        视频**不按轮数回溯**：每一条带视频的消息都把直链原样返回，由调用方
        决定只回放最近那一条（``[视频]`` 很贵：实测 1MB 短视频约 1100 token）。
        这里不做截断，是为了让"最近一次"的判定留在流水线里，逻辑只有一处；
        ``created_at`` 一起带出去，是因为直链带 ``rkey``、约 2 小时就过期。

        Returns:
            ``[(role, content, [图片文件名, ...], 视频直链或 None, created_at)]``，按时间正序。
        """
        if rounds <= 0:
            return []
        rows = self._db.query(
            "SELECT role, content, media, "
            "  CASE WHEN video IS NULL THEN '' ELSE video END AS video, created_at "
            "FROM messages WHERE memory_key = ? ORDER BY id DESC LIMIT ?",
            (key, int(rounds) * 2),
        )
        ordered = list(reversed(rows))
        # 最近 media_rounds 轮 = 最后 media_rounds*2 条
        keep_from = max(0, len(ordered) - max(0, int(media_rounds)) * 2)
        out: list[tuple[str, str, list[str], str | None, int]] = []
        for index, row in enumerate(ordered):
            media: list[str] = []
            if index >= keep_from:
                # sqlite3.Row 没有 .get()，用 keys() 判断列是否存在
                raw = row["media"] if "media" in row.keys() else None
                if raw:
                    try:
                        loaded = json.loads(str(raw))
                        if isinstance(loaded, list):
                            media = [str(name) for name in loaded if name]
                    except ValueError:
                        media = []
            video = str(row["video"] or "").strip() or None
            out.append(
                (
                    str(row["role"]),
                    str(row["content"]),
                    media,
                    video,
                    int(row["created_at"] or 0),
                )
            )
        return out

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

    # ------------------------------------------------------------------ 摘要
    def unsummarized_rounds(self, key: str) -> int:
        """未压缩的**完整轮数**。

        一轮 = 一条 user + 一条 assistant。取两者较小值，这样"有问无答"的半个
        轮次不会被算进去，也就不会把残缺内容喂给总结模型。
        """
        rows = self._db.query(
            "SELECT role, COUNT(*) AS n FROM messages "
            "WHERE memory_key = ? AND summarized = 0 GROUP BY role",
            (key,),
        )
        counts = {str(row["role"]): int(row["n"]) for row in rows}
        return min(counts.get("user", 0), counts.get("assistant", 0))

    def oldest_unsummarized(self, key: str, rounds: int) -> list[dict[str, Any]]:
        """最早的 ``rounds`` 轮未压缩原文（升序），供压缩使用。"""
        if rounds <= 0:
            return []
        rows = self._db.query(
            "SELECT id, role, content FROM messages "
            "WHERE memory_key = ? AND summarized = 0 ORDER BY id ASC LIMIT ?",
            (key, int(rounds) * 2),
        )
        return [dict(row) for row in rows]

    def summary(self, key: str) -> dict[str, Any] | None:
        rows = self._db.query("SELECT * FROM summaries WHERE memory_key = ?", (key,))
        return dict(rows[0]) if rows else None

    def summary_text(self, key: str) -> str:
        row = self.summary(key)
        return str(row.get("content") or "").strip() if row else ""

    def apply_summary(
        self,
        key: str,
        content: str,
        covered_ids: list[int],
        *,
        pending: bool = False,
    ) -> None:
        """把新总结与"这 W 轮已压缩"**原子写入**（需求规格 §6.3 的事务块）。

        两件事必须一起成功：只写总结会让原文被重复计入下一轮压缩；只标记原文
        会让内容永久丢失（老总结被顶掉了，却没有任何地方记着它）。
        """
        covered_to = int(covered_ids[-1]) if covered_ids else 0
        with self._db.transaction():
            self._db.execute(
                "INSERT INTO summaries(memory_key, content, covered_to, pending, updated_at) "
                "VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(memory_key) DO UPDATE SET "
                "  content    = excluded.content, "
                "  covered_to = MAX(covered_to, excluded.covered_to), "
                "  pending    = excluded.pending, "
                "  updated_at = excluded.updated_at",
                (key, content, covered_to, 1 if pending else 0, int(time.time())),
            )
            if covered_ids:
                self._db.executemany(
                    "UPDATE messages SET summarized = 1 WHERE id = ?",
                    [(int(i),) for i in covered_ids],
                )

    def set_summary_pending(self, key: str, pending: bool) -> None:
        """标记"压缩失败待重试"。**不删除任何原文**（需求规格 §6.3）。"""
        self._db.execute(
            "INSERT INTO summaries(memory_key, content, covered_to, pending, updated_at) "
            "VALUES(?, '', 0, ?, ?) "
            "ON CONFLICT(memory_key) DO UPDATE SET pending = excluded.pending",
            (key, 1 if pending else 0, int(time.time())),
        )

    def summary_pending(self, key: str) -> bool:
        row = self.summary(key)
        return bool(row and row.get("pending"))

    # ------------------------------------------------------------------ 面板
    def memory_overview(self) -> dict[str, Any]:
        """容量提示用：会话数、已压缩次数、占用体积（需求规格 §3.6）。"""
        rows = self._db.query(
            "SELECT "
            " (SELECT COUNT(*) FROM messages) AS messages, "
            " (SELECT COUNT(*) FROM summaries WHERE content <> '') AS summaries, "
            " (SELECT COUNT(*) FROM summaries WHERE pending = 1) AS pending, "
            " (SELECT COUNT(DISTINCT memory_key) FROM messages) AS keys, "
            " (SELECT COALESCE(SUM(LENGTH(content)), 0) FROM messages) AS msg_bytes, "
            " (SELECT COALESCE(SUM(LENGTH(content)), 0) FROM summaries) AS sum_bytes"
        )
        row = rows[0] if rows else {}
        db_bytes = 0
        try:
            db_bytes = self._db.path.stat().st_size
        except OSError:
            pass
        return {
            "sessions": int(row["keys"] or 0),
            "messages": int(row["messages"] or 0),
            "summaries": int(row["summaries"] or 0),
            "pending": int(row["pending"] or 0),
            "content_bytes": int(row["msg_bytes"] or 0) + int(row["sum_bytes"] or 0),
            "db_bytes": int(db_bytes),
        }

    def list_memories(self) -> list[dict[str, Any]]:
        """按会话分组列出记忆概况，供面板左栏展示。"""
        rows = self._db.query(
            "SELECT memory_key, "
            "       COUNT(*) AS total, "
            "       SUM(CASE WHEN summarized = 1 THEN 1 ELSE 0 END) AS compressed, "
            "       MAX(created_at) AS updated_at "
            "FROM messages GROUP BY memory_key ORDER BY updated_at DESC"
        )
        # 有总结但没有原文的键（极端情况：原文被删但总结还在）也要列出来
        extra = self._db.query(
            "SELECT s.memory_key, s.updated_at FROM summaries s "
            "WHERE s.content <> '' AND s.memory_key NOT IN (SELECT DISTINCT memory_key FROM messages)"
        )
        by_key = {str(row["memory_key"]): row for row in rows}
        for row in extra:
            by_key[str(row["memory_key"])] = None  # type: ignore[assignment]

        persona_names = {
            int(p["uid"]): str(p["name"] or "") for p in self.list_personas()
        }
        sessions = {
            str(s["session_id"]): s
            for s in self._db.query("SELECT session_id, session_type, session_ref FROM session_state")
        }

        result: list[dict[str, Any]] = []
        for key, row in by_key.items():
            session_id, _, persona_part = key.rpartition(":p")
            try:
                uid = int(persona_part)
            except ValueError:
                uid = DEFAULT_PERSONA_UID
            summary_row = self.summary(key)
            info = sessions.get(session_id)
            kind = str(info["session_type"]) if info else (
                "group" if session_id.startswith("group:") else "private"
            )
            ref = str(info["session_ref"]) if info else session_id.split(":", 1)[-1]
            result.append(
                {
                    "memory_key": key,
                    "session_id": session_id,
                    "session_type": kind,
                    "session_ref": ref,
                    "persona_uid": uid,
                    "persona_name": persona_names.get(uid, f"人设{uid}"),
                    "messages": int(row["total"]) if row else 0,
                    "compressed": int(row["compressed"] or 0) if row else 0,
                    "summary_chars": len(str(summary_row.get("content") or "")) if summary_row else 0,
                    "summary_pending": bool(summary_row and summary_row.get("pending")),
                    "updated_at": int(row["updated_at"] or 0) if row else int(
                        (summary_row or {}).get("updated_at") or 0
                    ),
                }
            )
        result.sort(key=lambda item: item["updated_at"], reverse=True)
        return result

    def memory_messages(self, key: str, *, limit: int = 500) -> list[dict[str, Any]]:
        """某个记忆键的原文（时间正序），供面板查看/编辑/删除。"""
        rows = self._db.query(
            "SELECT id, role, content, has_image, media, video, tokens, created_at, summarized "
            "FROM messages WHERE memory_key = ? ORDER BY id ASC LIMIT ?",
            (key, int(limit)),
        )
        return [dict(row) for row in rows]

    def update_message(self, message_id: int, content: str) -> bool:
        rows = self._db.query("SELECT id FROM messages WHERE id = ?", (int(message_id),))
        if not rows:
            return False
        self._db.execute(
            "UPDATE messages SET content = ? WHERE id = ?", (str(content), int(message_id))
        )
        return True

    def delete_message(self, message_id: int) -> bool:
        rows = self._db.query("SELECT id FROM messages WHERE id = ?", (int(message_id),))
        if not rows:
            return False
        self._db.execute("DELETE FROM messages WHERE id = ?", (int(message_id),))
        return True

    def delete_summary(self, key: str) -> bool:
        """只删总结、保留原文。

        删完要把原文的 ``summarized`` 复位，否则这些内容既不在总结里、又被当作
        已压缩，等于**永久消失**。
        """
        row = self.summary(key)
        if not row:
            return False
        with self._db.transaction():
            self._db.execute("DELETE FROM summaries WHERE memory_key = ?", (key,))
            self._db.execute(
                "UPDATE messages SET summarized = 0 WHERE memory_key = ?", (key,)
            )
        return True


__all__ = ["DEFAULT_PERSONA_UID", "MessageStore", "memory_key"]
