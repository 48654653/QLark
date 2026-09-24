"""应用配置：默认值 + 类型化读写。

M1 阶段实际用到的只有账号、主题与 OneBot 连接参数；其余键先定义好，
避免后面改动读取代码。
"""

from __future__ import annotations

from typing import Any

from .storage.db import Database
from .version import (
    DEFAULT_EVENT_PORT,
    DEFAULT_ONEBOT_PORT,
    DEFAULT_ONEBOT_TOKEN,
)

#: 配置键 → 默认值
DEFAULTS: dict[str, Any] = {
    # ---- 账号 ----
    "qq_account": None,          # 上次成功登录的 QQ 号（客户端自己持久化，不依赖 NapCat 日志）
    "qq_nickname": None,
    # ---- 界面 ----
    "theme": "dark",
    # ---- OneBot 网络（写入 onebot11_<QQ>.json 时使用）----
    "onebot_host": "127.0.0.1",
    "onebot_port": DEFAULT_ONEBOT_PORT,
    "onebot_token": DEFAULT_ONEBOT_TOKEN,
    # ---- 事件通道（NapCat → QLark 的消息推送）----
    "event_host": "127.0.0.1",
    "event_port": DEFAULT_EVENT_PORT,
    # ---- 机器人行为（M2）----
    "bot_paused": False,
    "max_concurrent": 4,
    "dedup_window": 600,
    "reply_quote_group": True,
    "max_reply_chars": 900,
    "notify_error_private": True,
    "ignored_users": [],
    "ignored_groups": [],
    # ---- 记忆 / 人设（M4 使用，先占位）----
    "memory_rounds": 20,
    "auto_summary": True,
    "persona_whitelist_only": True,
    # ---- 模型（M3 使用，先占位）----
    "base_url": "",
    "api_key": "",
    "model": "",
    "vision_model": "",
    "summary_model": "",
    "max_tokens": 2048,
    "temperature": 0.8,
    # ---- 运行态标记 ----
    "restart_required": False,   # 有 NapCat 侧配置待重启生效
}


class Settings:
    """基于 :class:`Database` 的类型化配置门面。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------ 通用
    def get(self, key: str, default: Any = None) -> Any:
        if key in DEFAULTS:
            fallback = DEFAULTS[key]
        else:
            fallback = default
        value = self._db.get_config(key, fallback)
        if value is None and fallback is not None:
            return fallback
        return value

    def set(self, key: str, value: Any) -> None:
        self._db.set_config(key, value)

    def update(self, **kwargs: Any) -> None:
        if kwargs:
            self._db.update_config(kwargs)

    def all(self) -> dict[str, Any]:
        merged = dict(DEFAULTS)
        merged.update(self._db.all_config())
        return merged

    # ------------------------------------------------------------------ 快捷属性
    @property
    def qq_account(self) -> str | None:
        value = self.get("qq_account")
        return str(value) if value else None

    @qq_account.setter
    def qq_account(self, value: str | None) -> None:
        self.set("qq_account", str(value) if value else None)

    @property
    def qq_nickname(self) -> str | None:
        value = self.get("qq_nickname")
        return str(value) if value else None

    @qq_nickname.setter
    def qq_nickname(self, value: str | None) -> None:
        self.set("qq_nickname", value or None)

    @property
    def theme(self) -> str:
        value = str(self.get("theme") or "dark")
        return value if value in ("dark", "light") else "dark"

    @theme.setter
    def theme(self, value: str) -> None:
        self.set("theme", "light" if value == "light" else "dark")

    @property
    def onebot_host(self) -> str:
        return str(self.get("onebot_host") or "127.0.0.1")

    @onebot_host.setter
    def onebot_host(self, value: str) -> None:
        self.set("onebot_host", str(value))

    @property
    def onebot_port(self) -> int:
        try:
            return int(self.get("onebot_port"))
        except (TypeError, ValueError):
            return DEFAULT_ONEBOT_PORT

    @onebot_port.setter
    def onebot_port(self, value: int | str) -> None:
        try:
            self.set("onebot_port", int(value))
        except (TypeError, ValueError):
            self.set("onebot_port", DEFAULT_ONEBOT_PORT)

    @property
    def onebot_token(self) -> str:
        return str(self.get("onebot_token") or DEFAULT_ONEBOT_TOKEN)

    @onebot_token.setter
    def onebot_token(self, value: str) -> None:
        self.set("onebot_token", str(value))

    @property
    def onebot_base_url(self) -> str:
        return f"http://{self.onebot_host}:{self.onebot_port}"

    # ------------------------------------------------------------------ 事件通道
    @property
    def event_host(self) -> str:
        return str(self.get("event_host") or "127.0.0.1")

    @event_host.setter
    def event_host(self, value: str) -> None:
        self.set("event_host", str(value))

    @property
    def event_port(self) -> int:
        try:
            return int(self.get("event_port"))
        except (TypeError, ValueError):
            return DEFAULT_EVENT_PORT

    @event_port.setter
    def event_port(self, value: int | str) -> None:
        try:
            self.set("event_port", int(value))
        except (TypeError, ValueError):
            self.set("event_port", DEFAULT_EVENT_PORT)

    @property
    def event_base_url(self) -> str:
        return f"http://{self.event_host}:{self.event_port}"

    # ------------------------------------------------------------------ 机器人行为
    @property
    def bot_paused(self) -> bool:
        return bool(self.get("bot_paused"))

    @bot_paused.setter
    def bot_paused(self, value: bool) -> None:
        self.set("bot_paused", bool(value))

    @property
    def max_concurrent(self) -> int:
        try:
            return max(1, min(16, int(self.get("max_concurrent"))))
        except (TypeError, ValueError):
            return 4

    @property
    def dedup_window(self) -> int:
        try:
            return max(30, int(self.get("dedup_window")))
        except (TypeError, ValueError):
            return 600

    @property
    def reply_quote_group(self) -> bool:
        return bool(self.get("reply_quote_group", True))

    @property
    def max_reply_chars(self) -> int:
        try:
            return max(100, int(self.get("max_reply_chars")))
        except (TypeError, ValueError):
            return 900

    @property
    def notify_error_private(self) -> bool:
        return bool(self.get("notify_error_private", True))

    def id_set(self, key: str) -> set[str]:
        """把配置里的 QQ / 群号列表读成字符串集合（兼容数字与字符串两种写法）。"""
        raw = self.get(key)
        if not isinstance(raw, (list, tuple, set)):
            return set()
        return {str(item).strip() for item in raw if str(item).strip()}

    @property
    def restart_required(self) -> bool:
        return bool(self.get("restart_required"))

    @restart_required.setter
    def restart_required(self, value: bool) -> None:
        self.set("restart_required", bool(value))
