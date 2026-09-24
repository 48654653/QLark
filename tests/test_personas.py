"""人设管理测试：增删改查、编号重排、删除级联、白名单。"""

from __future__ import annotations

from pathlib import Path

import pytest

from qlark.chat.personas import (
    DEFAULT_PERSONA_UID,
    MAX_PERSONAS,
    PersonaError,
    PersonaStore,
)
from qlark.chat.store import MessageStore, memory_key
from qlark.storage.db import Database


@pytest.fixture
def stores(tmp_path: Path):
    db = Database(tmp_path / "q.db")
    try:
        messages = MessageStore(db)
        messages.ensure_default_persona()
        yield db, messages, PersonaStore(db)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 基础
# --------------------------------------------------------------------------- #
def test_default_persona_is_created(stores) -> None:
    _db, _messages, personas = stores
    rows = personas.rows()
    assert len(rows) == 1
    assert int(rows[0]["uid"]) == DEFAULT_PERSONA_UID
    assert int(rows[0]["slot"]) == 1


def test_public_list_marks_default_undeletable(stores) -> None:
    _db, _messages, personas = stores
    items = personas.list_public()
    assert items[0]["can_delete"] is False
    created = personas.create()
    items = personas.list_public()
    assert items[1]["can_delete"] is True
    assert items[1]["uid"] == created["uid"]


def test_create_assigns_increasing_slots(stores) -> None:
    _db, _messages, personas = stores
    first = personas.create("人设2")
    second = personas.create("人设3")
    assert first["slot"] == 2
    assert second["slot"] == 3
    assert [item["slot"] for item in personas.list_public()] == [1, 2, 3]


def test_create_respects_limit(stores) -> None:
    _db, _messages, personas = stores
    for _ in range(MAX_PERSONAS - 1):
        personas.create()
    with pytest.raises(PersonaError):
        personas.create()


def test_update_fields(stores) -> None:
    _db, _messages, personas = stores
    uid = personas.create()["uid"]
    result = personas.update(
        uid,
        {
            "name": "傲娇女仆",
            "system_prompt": "你是傲娇女仆。",
            "enabled": True,
            "model_override": "gpt-4o-mini",
            "temperature_override": 1.2,
            "max_tokens_override": 512,
        },
    )
    assert result == {"ok": True}
    row = personas.get(uid)
    assert row is not None
    assert row["name"] == "傲娇女仆"
    assert row["system_prompt"] == "你是傲娇女仆。"
    assert row["model_override"] == "gpt-4o-mini"
    assert abs(float(row["temperature_override"]) - 1.2) < 1e-9
    assert int(row["max_tokens_override"]) == 512


def test_update_can_clear_overrides(stores) -> None:
    _db, _messages, personas = stores
    uid = personas.create()["uid"]
    personas.update(uid, {"model_override": "x", "temperature_override": 1.0})
    personas.update(uid, {"model_override": "", "temperature_override": None})
    row = personas.get(uid)
    assert row is not None
    assert row["model_override"] is None
    assert row["temperature_override"] is None


@pytest.mark.parametrize(
    ("fields", "field"),
    [
        ({"temperature_override": 3.0}, "temperature_override"),
        ({"temperature_override": "abc"}, "temperature_override"),
        ({"max_tokens_override": 0}, "max_tokens_override"),
        ({"max_tokens_override": 99999}, "max_tokens_override"),
        ({"name": "x" * 100}, "name"),
    ],
)
def test_update_validation(stores, fields: dict, field: str) -> None:
    _db, _messages, personas = stores
    uid = personas.create()["uid"]
    result = personas.update(uid, fields)
    assert result["ok"] is False
    assert field in result["errors"]


# --------------------------------------------------------------------------- #
# 删除级联
# --------------------------------------------------------------------------- #
def test_delete_default_is_refused(stores) -> None:
    _db, _messages, personas = stores
    with pytest.raises(PersonaError):
        personas.delete(DEFAULT_PERSONA_UID)


def test_delete_renumbers_slots(stores) -> None:
    _db, _messages, personas = stores
    second = personas.create("人设2")["uid"]
    personas.create("人设3")
    personas.delete(second)
    items = personas.list_public()
    assert [item["slot"] for item in items] == [1, 2], "删除后编号必须连续"


def test_delete_reassigns_sessions_and_wipes_memory(stores) -> None:
    """需求规格 §5.4：会话静默切回人设1 + 该人设全部记忆一并删除。"""
    _db, messages, personas = stores
    uid = personas.create("临时人设")["uid"]

    # 两个会话正在用这个人设
    messages.session_persona("group:111", session_type="group", session_ref="111")
    messages.set_session_persona("group:111", uid)
    messages.session_persona("private:222", session_type="private", session_ref="222")
    messages.set_session_persona("private:222", uid)

    # 各写几条记忆；同时给人设1留一条，确认没被误删
    for session in ("group:111", "private:222"):
        key = memory_key(session, uid)
        messages.append(key, "user", "你好")
        messages.append(key, "assistant", "收到")
    keep_key = memory_key("group:111", DEFAULT_PERSONA_UID)
    messages.append(keep_key, "user", "人设1的记忆")

    result = personas.delete(uid)
    assert result.affected_sessions == 2
    assert result.deleted_keys == 2
    assert result.deleted_messages == 4

    # 会话回落到人设1
    assert messages.session_persona("group:111", session_type="group", session_ref="111") == 1
    assert messages.session_persona("private:222", session_type="private", session_ref="222") == 1
    # 该人设的记忆没了，人设1的还在
    assert messages.message_count(memory_key("group:111", uid)) == 0
    assert messages.message_count(memory_key("private:222", uid)) == 0
    assert messages.message_count(keep_key) == 1


def test_delete_does_not_touch_similar_uid(stores) -> None:
    """``:p1`` 与 ``:p10`` 不能互相误伤。"""
    _db, messages, personas = stores
    uid = personas.create()["uid"]  # uid=2
    for _ in range(8):
        personas.create()  # 直到出现 uid=10

    key_2 = memory_key("group:1", 2)
    key_10 = memory_key("group:1", 10)
    messages.append(key_2, "user", "属于 p2")
    messages.append(key_10, "user", "属于 p10")

    personas.delete(2)
    assert messages.message_count(key_2) == 0
    assert messages.message_count(key_10) == 1, "p10 的记忆被误删了"


def test_clear_memory_keeps_persona(stores) -> None:
    _db, messages, personas = stores
    uid = personas.create()["uid"]
    key = memory_key("group:1", uid)
    messages.append(key, "user", "x")
    result = personas.clear_memory(uid)
    assert result["deleted_messages"] == 1
    assert personas.get(uid) is not None
    assert messages.message_count(key) == 0


def test_message_counts_are_reported(stores) -> None:
    _db, messages, personas = stores
    uid = personas.create()["uid"]
    messages.append(memory_key("group:1", uid), "user", "a")
    messages.append(memory_key("group:1", uid), "assistant", "b")
    messages.append(memory_key("group:2", uid), "user", "c")
    counts = {item["uid"]: item["message_count"] for item in personas.list_public()}
    assert counts[uid] == 3


# --------------------------------------------------------------------------- #
# 白名单
# --------------------------------------------------------------------------- #
def test_whitelist_add_remove(stores) -> None:
    _db, _messages, personas = stores
    assert personas.whitelist() == []
    assert personas.add_whitelist("2157649443") == ["2157649443"]
    assert personas.is_whitelisted("2157649443") is True
    assert personas.add_whitelist("2157649443") == ["2157649443"], "重复添加不应报错"
    assert personas.whitelist() == ["2157649443"]
    assert personas.remove_whitelist("2157649443") == []
    assert personas.is_whitelisted("2157649443") is False


@pytest.mark.parametrize("bad", ["abc", "", "123", "1" * 20])
def test_whitelist_rejects_bad_qq(stores, bad: str) -> None:
    _db, _messages, personas = stores
    with pytest.raises(PersonaError):
        personas.add_whitelist(bad)
