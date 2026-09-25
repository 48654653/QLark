"""人设的增删改查。

规则来自需求规格 v1.1 §5：

* 出厂默认人设 **uid=1，不可删除**，新会话初始使用它
* 界面编号（``slot``）连续 ``1..N``，删除后**重排**
* 记忆用**内部不可变 uid** 绑定（``<session>:p<uid>``），所以重排不会导致记忆错配
* 删除人设时：① 正在使用它的会话**静默切回人设1**；② **它的全部记忆一并删除**
* 上限 **99** 个
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..storage.db import Database

log = logging.getLogger(__name__)

#: 出厂默认人设的 uid（不可删除）
DEFAULT_PERSONA_UID = 1
#: 人设数量上限（需求规格 §3.5）
MAX_PERSONAS = 99
#: 字段长度上限
MAX_NAME = 64
MAX_MODEL = 128
MAX_PROMPT = 8000


def memory_suffix(uid: int) -> str:
    """记忆键的尾部标记，用于按人设筛出记忆。"""
    return f":p{int(uid)}"


@dataclass
class DeleteResult:
    uid: int
    affected_sessions: int = 0
    deleted_keys: int = 0
    deleted_messages: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "affected_sessions": self.affected_sessions,
            "affected_keys": self.deleted_keys,
            "deleted_messages": self.deleted_messages,
        }


class PersonaError(ValueError):
    """人设操作失败（会被包装成 ``{ok: false, error: ...}`` 返回给界面）。"""


class PersonaStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------ 读取
    def ensure_default(self) -> int:
        rows = self._db.query("SELECT uid FROM personas WHERE uid = ?", (DEFAULT_PERSONA_UID,))
        if rows:
            return DEFAULT_PERSONA_UID
        self._db.execute(
            "INSERT INTO personas(uid, slot, name, enabled, system_prompt, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (DEFAULT_PERSONA_UID, 1, "人设1", 1, "", int(time.time())),
        )
        log.info("已创建出厂默认人设（uid=%s）", DEFAULT_PERSONA_UID)
        return DEFAULT_PERSONA_UID

    def rows(self) -> list[dict[str, Any]]:
        self.ensure_default()
        return [
            dict(row)
            for row in self._db.query("SELECT * FROM personas ORDER BY slot, uid")
        ]

    def get(self, uid: int) -> dict[str, Any] | None:
        rows = self._db.query("SELECT * FROM personas WHERE uid = ?", (int(uid),))
        return dict(rows[0]) if rows else None

    def count_by_uid(self) -> dict[int, int]:
        """每个 uid 名下的记忆条数（按记忆键尾部 ``:p<uid>`` 归属）。"""
        counts: dict[int, int] = {}
        rows = self._db.query(
            "SELECT memory_key, COUNT(*) AS n FROM messages GROUP BY memory_key"
        )
        for row in rows:
            uid = _uid_from_key(str(row["memory_key"]))
            if uid is not None:
                counts[uid] = counts.get(uid, 0) + int(row["n"])
        return counts

    def list_public(self) -> list[dict[str, Any]]:
        counts = self.count_by_uid()
        result: list[dict[str, Any]] = []
        for row in self.rows():
            uid = int(row["uid"])
            result.append(
                {
                    "uid": uid,
                    "slot": int(row.get("slot") or uid),
                    "name": str(row.get("name") or ""),
                    "avatar_path": str(row.get("avatar_path") or ""),
                    "system_prompt": str(row.get("system_prompt") or ""),
                    "enabled": bool(row.get("enabled")),
                    "model_override": row.get("model_override"),
                    "temperature_override": row.get("temperature_override"),
                    "max_tokens_override": row.get("max_tokens_override"),
                    # 随机分段 / 表情包：人设级开关（"这个角色会不会这么做"）
                    "split_enabled": bool(row.get("split_enabled")),
                    "meme_enabled": bool(row.get("meme_enabled")),
                    "can_delete": uid != DEFAULT_PERSONA_UID,
                    "message_count": counts.get(uid, 0),
                }
            )
        return result

    def next_slot(self) -> int:
        rows = self._db.query("SELECT MAX(slot) AS m FROM personas")
        current = int(rows[0]["m"] or 0) if rows else 0
        return current + 1

    # ------------------------------------------------------------------ 校验
    @staticmethod
    def validate(fields: dict[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}

        if "name" in fields:
            name = str(fields["name"] or "")
            if len(name) > MAX_NAME:
                errors["name"] = f"名称最长 {MAX_NAME} 个字符"

        if "system_prompt" in fields:
            prompt = str(fields["system_prompt"] or "")
            if len(prompt) > MAX_PROMPT:
                errors["system_prompt"] = f"提示词最长 {MAX_PROMPT} 个字符"

        if fields.get("model_override"):
            if len(str(fields["model_override"])) > MAX_MODEL:
                errors["model_override"] = f"模型名最长 {MAX_MODEL} 个字符"

        temperature = fields.get("temperature_override")
        if temperature not in (None, ""):
            try:
                value = float(temperature)
            except (TypeError, ValueError):
                errors["temperature_override"] = "温度必须是数字"
            else:
                if not 0.0 <= value <= 2.0:
                    errors["temperature_override"] = "温度必须在 0~2 之间"

        max_tokens = fields.get("max_tokens_override")
        if max_tokens not in (None, ""):
            try:
                value = int(max_tokens)
            except (TypeError, ValueError):
                errors["max_tokens_override"] = "最大 Token 必须是整数"
            else:
                if not 1 <= value <= 32000:
                    errors["max_tokens_override"] = "最大 Token 必须在 1~32000 之间"

        return errors

    # ------------------------------------------------------------------ 写入
    def create(self, name: str = "") -> dict[str, Any]:
        self.ensure_default()
        if len(self.rows()) >= MAX_PERSONAS:
            raise PersonaError(f"人设数量已达上限（{MAX_PERSONAS} 个）")

        slot = self.next_slot()
        created = int(time.time())
        self._db.execute(
            "INSERT INTO personas(slot, name, enabled, system_prompt, created_at) "
            "VALUES(?, ?, ?, ?, ?)",
            (slot, str(name or f"人设{slot}"), 1, "", created),
        )
        rows = self._db.query("SELECT last_insert_rowid() AS uid")
        uid = int(rows[0]["uid"])
        log.info("已新建人设 uid=%s slot=%s", uid, slot)
        return {"ok": True, "uid": uid, "slot": slot}

    def update(self, uid: int, fields: dict[str, Any]) -> dict[str, Any]:
        row = self.get(uid)
        if row is None:
            raise PersonaError(f"人设 {uid} 不存在")

        errors = self.validate(fields)
        if errors:
            return {"ok": False, "errors": errors}

        updates: dict[str, Any] = {}
        if "name" in fields:
            updates["name"] = str(fields["name"] or "").strip()
        if "system_prompt" in fields:
            updates["system_prompt"] = str(fields["system_prompt"] or "")
        if "enabled" in fields:
            updates["enabled"] = 1 if fields["enabled"] else 0
        if "split_enabled" in fields:
            updates["split_enabled"] = 1 if fields["split_enabled"] else 0
        if "meme_enabled" in fields:
            updates["meme_enabled"] = 1 if fields["meme_enabled"] else 0
        if "avatar_path" in fields:
            updates["avatar_path"] = str(fields["avatar_path"] or "")

        for key in ("model_override", "temperature_override", "max_tokens_override"):
            if key not in fields:
                continue
            value = fields[key]
            if value in (None, "", "null"):
                updates[key] = None
            elif key == "temperature_override":
                updates[key] = float(value)
            elif key == "max_tokens_override":
                updates[key] = int(value)
            else:
                updates[key] = str(value).strip() or None

        if not updates:
            return {"ok": True}

        assignments = ", ".join(f"{key} = ?" for key in updates)
        self._db.execute(
            f"UPDATE personas SET {assignments} WHERE uid = ?",
            (*updates.values(), int(uid)),
        )
        return {"ok": True}

    def delete(self, uid: int) -> DeleteResult:
        """删除人设：会话静默回落到人设1，该人设的全部记忆一并删除，编号重排。"""
        uid = int(uid)
        if uid == DEFAULT_PERSONA_UID:
            raise PersonaError("出厂默认人设不可删除")
        if self.get(uid) is None:
            raise PersonaError(f"人设 {uid} 不存在")

        self.ensure_default()

        # ① 正在使用它的会话 → 静默切回人设1
        affected_rows = self._db.query(
            "SELECT COUNT(*) AS n FROM session_state WHERE active_persona = ?", (uid,)
        )
        affected = int(affected_rows[0]["n"]) if affected_rows else 0
        self._db.execute(
            "UPDATE session_state SET active_persona = ?, updated_at = ? WHERE active_persona = ?",
            (DEFAULT_PERSONA_UID, int(time.time()), uid),
        )

        # ② 该人设的全部记忆一并删除
        keys = self._keys_for(uid)
        deleted_messages = 0
        for key in keys:
            rows = self._db.query(
                "SELECT COUNT(*) AS n FROM messages WHERE memory_key = ?", (key,)
            )
            deleted_messages += int(rows[0]["n"]) if rows else 0
            self._db.execute("DELETE FROM messages WHERE memory_key = ?", (key,))
            self._db.execute("DELETE FROM summaries WHERE memory_key = ?", (key,))

        self._db.execute("DELETE FROM personas WHERE uid = ?", (uid,))
        self._renumber()

        result = DeleteResult(
            uid=uid,
            affected_sessions=affected,
            deleted_keys=len(keys),
            deleted_messages=deleted_messages,
        )
        log.info(
            "已删除人设 uid=%s：回落 %s 个会话，删除 %s 条记忆",
            uid,
            affected,
            deleted_messages,
        )
        return result

    def clear_memory(self, uid: int) -> dict[str, Any]:
        """清空某个人设的全部记忆（人设本身保留）。"""
        if self.get(uid) is None:
            raise PersonaError(f"人设 {uid} 不存在")
        keys = self._keys_for(uid)
        deleted = 0
        for key in keys:
            rows = self._db.query(
                "SELECT COUNT(*) AS n FROM messages WHERE memory_key = ?", (key,)
            )
            deleted += int(rows[0]["n"]) if rows else 0
            self._db.execute("DELETE FROM messages WHERE memory_key = ?", (key,))
            self._db.execute("DELETE FROM summaries WHERE memory_key = ?", (key,))
        return {"ok": True, "deleted_messages": deleted, "affected_keys": len(keys)}

    # ------------------------------------------------------------------ 白名单
    def whitelist(self) -> list[str]:
        rows = self._db.query("SELECT qq FROM persona_whitelist ORDER BY qq")
        return [str(row["qq"]) for row in rows]

    def add_whitelist(self, qq: str) -> list[str]:
        value = str(qq).strip()
        if not value.isdigit():
            raise PersonaError("QQ 号必须是纯数字")
        if len(value) < 5 or len(value) > 12:
            raise PersonaError("QQ 号长度不合法")
        self._db.execute(
            "INSERT INTO persona_whitelist(qq) VALUES(?) ON CONFLICT(qq) DO NOTHING",
            (value,),
        )
        return self.whitelist()

    def remove_whitelist(self, qq: str) -> list[str]:
        self._db.execute("DELETE FROM persona_whitelist WHERE qq = ?", (str(qq).strip(),))
        return self.whitelist()

    def is_whitelisted(self, qq: str) -> bool:
        rows = self._db.query("SELECT 1 FROM persona_whitelist WHERE qq = ?", (str(qq),))
        return bool(rows)

    # ------------------------------------------------------------------ 内部
    def _keys_for(self, uid: int) -> set[str]:
        suffix = memory_suffix(uid)
        keys: set[str] = set()
        for table in ("messages", "summaries"):
            for row in self._db.query(f"SELECT DISTINCT memory_key FROM {table}"):
                key = str(row["memory_key"])
                if key.endswith(suffix):
                    keys.add(key)
        return keys

    def _renumber(self) -> None:
        """按当前顺序把界面编号重排成连续的 1..N。"""
        rows = self._db.query("SELECT uid FROM personas ORDER BY slot, uid")
        self._db.executemany(
            "UPDATE personas SET slot = ? WHERE uid = ?",
            ((index, int(row["uid"])) for index, row in enumerate(rows, start=1)),
        )


def _uid_from_key(key: str) -> int | None:
    marker = key.rfind(":p")
    if marker < 0:
        return None
    tail = key[marker + 2 :]
    return int(tail) if tail.isdigit() else None


__all__ = [
    "DEFAULT_PERSONA_UID",
    "DeleteResult",
    "MAX_PERSONAS",
    "PersonaError",
    "PersonaStore",
    "memory_suffix",
]
