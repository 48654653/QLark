"""QQ 里的聊天指令。

需求规格 §4.5 定义的指令集：

===============  ==========================================================
``/person N``    切换本会话的人设（群聊默认仅白名单 QQ 可用）
``/help``        指令列表
``/reset``       清空本会话（当前人设）的记忆，二次确认由用户自己负责
``/status``      当前会话身份 / 人设 / 记忆轮数 / 消息条数
===============  ==========================================================

两条硬规则：

1. **指令不写入记忆** —— 否则模型会看到一堆 ``/person 3`` 污染上下文。
2. **判定基于解析后的文本**，并兼容全角 ``／``（中文输入法下很容易打出来）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..core.models import LogSource
from ..napcat.logbus import LogBus
from ..onebot.events import MessageEvent
from ..onebot.segments import ParsedMessage
from ..settings import Settings
from .personas import DEFAULT_PERSONA_UID, PersonaError, PersonaStore
from .store import MessageStore, memory_key

log = logging.getLogger(__name__)

HELP_TEXT = (
    "可用指令：\n"
    "/person N — 切换人设（N 是人设编号，不带参数则列出全部）\n"
    "/status — 查看当前会话状态\n"
    "/reset — 清空本会话的记忆\n"
    "/help — 显示这条帮助"
)

NO_PERMISSION_TEXT = "你没有切换人设的权限。"


@dataclass
class CommandOutcome:
    """指令处理结果。"""

    handled: bool = False
    reply: str = ""
    #: 是否改动了会影响界面显示的状态（人设切换等）
    changed: bool = False


def normalize_command_text(text: str) -> str:
    """统一全角斜杠、去掉首尾空白。"""
    return (text or "").replace("／", "/").replace("＼", "/").strip()


def parse_command(text: str) -> tuple[str, list[str]] | None:
    """把 ``/person 3`` 解析成 ``("person", ["3"])``；不是指令则返回 ``None``。"""
    normalized = normalize_command_text(text)
    if not normalized.startswith("/"):
        return None
    parts = normalized.split()
    if not parts:
        return None
    name = parts[0][1:].lower()
    if not name or not name.isalpha():
        return None
    return name, parts[1:]


class CommandRouter:
    """把指令变成一条回复，不经过模型。"""

    def __init__(
        self,
        settings: Settings,
        store: MessageStore,
        personas: PersonaStore,
        logbus: LogBus,
    ) -> None:
        self.settings = settings
        self.store = store
        self.personas = personas
        self.logbus = logbus

    # ------------------------------------------------------------------ 权限
    def can_switch_persona(self, event: MessageEvent) -> bool:
        """私聊始终允许本人；群聊默认仅白名单 QQ。"""
        if not event.is_group:
            return True
        if not bool(self.settings.get("persona_whitelist_only", True)):
            return True
        return self.personas.is_whitelisted(event.user_id)

    # ------------------------------------------------------------------ 入口
    def handle(
        self,
        event: MessageEvent,
        parsed: ParsedMessage,
        persona_uid: int,
    ) -> CommandOutcome:
        parsed_command = parse_command(parsed.text)
        if parsed_command is None:
            return CommandOutcome(handled=False)

        name, args = parsed_command
        if name in ("person", "renshe"):
            return self._cmd_person(event, args, persona_uid)
        if name == "help":
            return CommandOutcome(handled=True, reply=HELP_TEXT)
        if name == "status":
            return self._cmd_status(event, persona_uid)
        if name == "reset":
            return self._cmd_reset(event, persona_uid)
        return CommandOutcome(handled=True, reply=f"未知指令：/{name}\n\n{HELP_TEXT}")

    # ------------------------------------------------------------------ /person
    def _cmd_person(
        self, event: MessageEvent, args: list[str], persona_uid: int
    ) -> CommandOutcome:
        personas = self.personas.list_public()

        if not args:
            return CommandOutcome(handled=True, reply=self._describe_personas(personas, persona_uid))

        raw = args[0].lstrip("#")
        if not raw.isdigit():
            return CommandOutcome(handled=True, reply="用法：/person <编号>，例如 /person 2")

        if not self.can_switch_persona(event):
            self._log(f"{event.describe()} 尝试切换人设但没有权限", level="warn")
            return CommandOutcome(handled=True, reply=NO_PERMISSION_TEXT)

        slot = int(raw)
        target = next((item for item in personas if item["slot"] == slot), None)
        if target is None:
            available = "、".join(str(item["slot"]) for item in personas) or "（无）"
            return CommandOutcome(handled=True, reply=f"人设 {slot} 不存在。可用编号：{available}")

        if not target["enabled"]:
            return CommandOutcome(
                handled=True, reply=f"人设 {slot} 已被停用，无法切换。"
            )

        self.store.set_session_persona(
            event.session_id,
            int(target["uid"]),
            session_type="group" if event.is_group else "private",
            session_ref=str(event.group_id if event.is_group else event.user_id),
        )
        label = target["name"] or "未命名"
        self._log(
            f"{event.describe()} 切换人设为 {slot}（{label}）", level="info"
        )
        return CommandOutcome(
            handled=True, reply=f"已切换为人设 {slot}：{label}", changed=True
        )

    def _describe_personas(self, personas: list[dict[str, Any]], current_uid: int) -> str:
        lines = ["可用人设："]
        for item in personas:
            marks = []
            if int(item["uid"]) == int(current_uid):
                marks.append("当前")
            if not item["enabled"]:
                marks.append("已停用")
            suffix = f"（{'、'.join(marks)}）" if marks else ""
            lines.append(f"{item['slot']}. {item['name'] or '未命名'}{suffix}")
        lines.append("发送 /person <编号> 切换")
        return "\n".join(lines)

    # ------------------------------------------------------------------ /status
    def _cmd_status(self, event: MessageEvent, persona_uid: int) -> CommandOutcome:
        row = self.personas.get(persona_uid) or {}
        key = memory_key(event.session_id, persona_uid)
        lines = [
            f"会话：{event.session_id}",
            f"身份：{'群聊' if event.is_group else '私聊'}",
            f"当前人设：{row.get('slot', '?')}. {row.get('name') or '未命名'}",
            f"记忆轮数上限：{self.settings.get('memory_rounds')}",
            f"本会话记忆条数：{self.store.message_count(key)}",
        ]
        return CommandOutcome(handled=True, reply="\n".join(lines))

    # ------------------------------------------------------------------ /reset
    def _cmd_reset(self, event: MessageEvent, persona_uid: int) -> CommandOutcome:
        if event.is_group and not self.can_switch_persona(event):
            return CommandOutcome(handled=True, reply="你没有清空记忆的权限。")

        key = memory_key(event.session_id, persona_uid)
        removed = self.store.clear(key)
        self._log(f"{event.describe()} 清空了会话记忆（{removed} 条）", level="warn")
        return CommandOutcome(
            handled=True, reply=f"已清空本会话的记忆（{removed} 条）。", changed=True
        )

    # ------------------------------------------------------------------ 日志
    def _log(self, message: str, *, level: str = "info") -> None:
        self.logbus.add(message, level=level, source=LogSource.QLARK.value)


__all__ = [
    "CommandOutcome",
    "CommandRouter",
    "HELP_TEXT",
    "NO_PERMISSION_TEXT",
    "normalize_command_text",
    "parse_command",
]
