"""聊天指令测试：解析、权限、反馈文案。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from qlark.chat.commands import HELP_TEXT, CommandRouter, normalize_command_text, parse_command
from qlark.chat.personas import PersonaStore
from qlark.chat.store import MessageStore, memory_key
from qlark.napcat.logbus import LogBus
from qlark.onebot.events import MessageEvent
from qlark.onebot.segments import ParsedMessage
from qlark.settings import Settings
from qlark.storage.db import Database


def make_event(
    *,
    group: bool = True,
    user: str = "100001",
    group_id: str = "123456",
    message_id: str = "1",
) -> MessageEvent:
    return MessageEvent(
        message_id=message_id,
        message_type="group" if group else "private",
        sub_type="normal",
        user_id=user,
        self_id="10000001",
        group_id=group_id if group else None,
        sender={"user_id": user, "nickname": "测试用户"},
    )


def make_parsed(text: str) -> ParsedMessage:
    return ParsedMessage(text=text, mentions_bot=True)


@pytest.fixture
def ctx(tmp_path: Path):
    db = Database(tmp_path / "q.db")
    logbus = LogBus(capacity=500, log_dir=tmp_path, persist=False)
    settings = Settings(db)
    store = MessageStore(db)
    store.ensure_default_persona()
    personas = PersonaStore(db)
    router = CommandRouter(settings, store, personas, logbus)
    try:
        yield SimpleNamespace(
            db=db,
            settings=settings,
            store=store,
            personas=personas,
            router=router,
            logbus=logbus,
        )
    finally:
        logbus.close()
        db.close()


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/person 3", ("person", ["3"])),
        ("/PERSON 3", ("person", ["3"])),
        ("  /person   3  ", ("person", ["3"])),
        ("／person 3", ("person", ["3"])),          # 全角斜杠
        ("/person #3", ("person", ["#3"])),
        ("/help", ("help", [])),
        ("/help 额外参数", ("help", ["额外参数"])),
    ],
)
def test_parse_command(text: str, expected: tuple) -> None:
    assert parse_command(text) == expected


@pytest.mark.parametrize("text", ["你好", "", "  ", "/", "/123", "person 3", "[图片]"])
def test_parse_command_rejects_non_commands(text: str) -> None:
    assert parse_command(text) is None


def test_normalize_command_text() -> None:
    assert normalize_command_text("／person 3") == "/person 3"
    assert normalize_command_text("  /x  ") == "/x"


# --------------------------------------------------------------------------- #
# /help /status /未知
# --------------------------------------------------------------------------- #
def test_help(ctx) -> None:
    outcome = ctx.router.handle(make_event(), make_parsed("/help"), 1)
    assert outcome.handled is True
    assert outcome.reply == HELP_TEXT
    assert "/person" in outcome.reply


def test_unknown_command(ctx) -> None:
    outcome = ctx.router.handle(make_event(), make_parsed("/nope"), 1)
    assert outcome.handled is True
    assert "未知指令" in outcome.reply


def test_status(ctx) -> None:
    ctx.personas.update(2 if ctx.personas.create()["uid"] == 2 else 2, {})  # 确保有第二个人设
    uid = ctx.personas.create("傲娇女仆")["uid"]
    key = memory_key("group:123456", uid)
    ctx.store.append(key, "user", "x")
    outcome = ctx.router.handle(make_event(), make_parsed("/status"), uid)
    assert outcome.handled is True
    assert "group:123456" in outcome.reply
    assert "傲娇女仆" in outcome.reply
    assert "记忆条数：1" in outcome.reply


def test_non_command_is_not_handled(ctx) -> None:
    outcome = ctx.router.handle(make_event(), make_parsed("你好呀"), 1)
    assert outcome.handled is False


def test_image_only_message_does_not_crash_router(ctx) -> None:
    outcome = ctx.router.handle(make_event(), make_parsed("[图片]"), 1)
    assert outcome.handled is False


# --------------------------------------------------------------------------- #
# /person 权限
# --------------------------------------------------------------------------- #
def test_private_switch_is_always_allowed(ctx) -> None:
    uid = ctx.personas.create("傲娇女仆")["uid"]
    ctx.settings.set("persona_whitelist_only", True)

    outcome = ctx.router.handle(make_event(group=False), make_parsed("/person 2"), 1)
    assert outcome.handled is True
    assert "已切换为人设 2" in outcome.reply
    assert "傲娇女仆" in outcome.reply
    assert outcome.changed is True
    assert ctx.store.session_persona("private:100001", session_type="private", session_ref="100001") == uid


def test_group_switch_requires_whitelist_by_default(ctx) -> None:
    ctx.personas.create("傲娇女仆")
    ctx.settings.set("persona_whitelist_only", True)

    outcome = ctx.router.handle(make_event(user="100001"), make_parsed("/person 2"), 1)
    assert outcome.handled is True
    assert "没有" in outcome.reply and "权限" in outcome.reply
    # 人设没有被切换
    assert ctx.store.session_persona("group:123456", session_type="group", session_ref="123456") == 1


def test_group_switch_allowed_for_whitelisted_user(ctx) -> None:
    uid = ctx.personas.create("傲娇女仆")["uid"]
    ctx.personas.add_whitelist("100001")
    ctx.settings.set("persona_whitelist_only", True)

    outcome = ctx.router.handle(make_event(user="100001"), make_parsed("/person 2"), 1)
    assert outcome.changed is True
    assert ctx.store.session_persona("group:123456", session_type="group", session_ref="123456") == uid


def test_group_switch_allowed_for_everyone_when_toggle_off(ctx) -> None:
    uid = ctx.personas.create("傲娇女仆")["uid"]
    ctx.settings.set("persona_whitelist_only", False)

    outcome = ctx.router.handle(make_event(user="200002"), make_parsed("/person 2"), 1)
    assert outcome.changed is True
    assert ctx.store.session_persona("group:123456", session_type="group", session_ref="123456") == uid


# --------------------------------------------------------------------------- #
# /person 参数
# --------------------------------------------------------------------------- #
def test_person_switches_for_this_session_only(ctx) -> None:
    """人设作用域是会话独立的：甲群切换不影响乙群。"""
    ctx.personas.create("傲娇女仆")
    ctx.personas.add_whitelist("100001")
    ctx.router.handle(make_event(group_id="111"), make_parsed("/person 2"), 1)

    assert ctx.store.session_persona("group:111", session_type="group", session_ref="111") == 2
    assert ctx.store.session_persona("group:222", session_type="group", session_ref="222") == 1


def test_person_without_argument_lists_personas(ctx) -> None:
    ctx.personas.create("傲娇女仆")
    outcome = ctx.router.handle(make_event(), make_parsed("/person"), 1)
    assert outcome.handled is True
    assert "可用人设" in outcome.reply
    assert "傲娇女仆" in outcome.reply
    assert "当前" in outcome.reply


def test_person_with_bad_argument(ctx) -> None:
    outcome = ctx.router.handle(make_event(), make_parsed("/person abc"), 1)
    assert outcome.handled is True
    assert "用法" in outcome.reply


def test_person_with_missing_slot(ctx) -> None:
    ctx.personas.add_whitelist("100001")
    outcome = ctx.router.handle(make_event(), make_parsed("/person 99"), 1)
    assert outcome.handled is True
    assert "不存在" in outcome.reply
    assert "可用编号：1" in outcome.reply


def test_person_without_permission_does_not_leak_slot_existence(ctx) -> None:
    """无权限时统一回"没有权限"，不透露某个编号存不存在。"""
    ctx.settings.set("persona_whitelist_only", True)
    outcome = ctx.router.handle(make_event(user="200002"), make_parsed("/person 99"), 1)
    assert "权限" in outcome.reply
    assert "不存在" not in outcome.reply


def test_person_refuses_disabled_persona(ctx) -> None:
    uid = ctx.personas.create("停用的人设")["uid"]
    ctx.personas.update(uid, {"enabled": False})
    ctx.personas.add_whitelist("100001")

    outcome = ctx.router.handle(make_event(), make_parsed("/person 2"), 1)
    assert outcome.handled is True
    assert "停用" in outcome.reply


def test_person_slot_maps_to_immutable_uid(ctx) -> None:
    """界面编号会重排，但记忆绑的是 uid —— 切换后记忆键必须跟着 uid 走。"""
    second = ctx.personas.create("人设2")["uid"]
    ctx.personas.create("人设3")
    ctx.personas.delete(second)  # 删除后原"人设3"变成 slot 2

    ctx.personas.add_whitelist("100001")
    ctx.router.handle(make_event(), make_parsed("/person 2"), 1)
    active = ctx.store.session_persona("group:123456", session_type="group", session_ref="123456")
    assert active != second
    assert ctx.personas.get(active) is not None
    assert int(ctx.personas.get(active)["slot"]) == 2


# --------------------------------------------------------------------------- #
# /reset
# --------------------------------------------------------------------------- #
def test_reset_clears_current_persona_memory(ctx) -> None:
    ctx.personas.add_whitelist("100001")
    key = memory_key("group:123456", 1)
    ctx.store.append(key, "user", "a")
    ctx.store.append(key, "assistant", "b")

    outcome = ctx.router.handle(make_event(), make_parsed("/reset"), 1)
    assert outcome.handled is True
    assert "2" in outcome.reply
    assert ctx.store.message_count(key) == 0


def test_reset_requires_permission_in_group(ctx) -> None:
    key = memory_key("group:123456", 1)
    ctx.store.append(key, "user", "a")
    ctx.settings.set("persona_whitelist_only", True)

    outcome = ctx.router.handle(make_event(user="200002"), make_parsed("/reset"), 1)
    assert "权限" in outcome.reply
    assert ctx.store.message_count(key) == 1, "无权限时不该清掉记忆"


def test_reset_allowed_in_private(ctx) -> None:
    key = memory_key("private:100001", 1)
    ctx.store.append(key, "user", "a")
    outcome = ctx.router.handle(make_event(group=False), make_parsed("/reset"), 1)
    assert "已清空" in outcome.reply
    assert ctx.store.message_count(key) == 0
