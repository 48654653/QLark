"""领域模型：状态机阶段、日志行、环境自检报告、应用状态快照。

``AppState.to_dict()`` 的输出结构就是前端 JS 的契约，修改时必须同步
``src/qlark/web/app.js``。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# --------------------------------------------------------------------------- #
# 状态机阶段
# --------------------------------------------------------------------------- #
class Phase(str, Enum):
    """启动 / 登录状态机阶段（与前端 ``phase`` 取值一一对应）。"""

    IDLE = "idle"
    ENV_CHECK = "env_check"
    ENV_ERROR = "env_error"
    STARTING = "starting"
    QUICK_LOGIN = "quick_login"
    WAITING_SCAN = "waiting_scan"
    LOGGED_IN = "logged_in"
    NETWORK_SETUP = "network_setup"
    RESTARTING = "restarting"
    READY = "ready"
    STOPPED = "stopped"
    ERROR = "error"


PHASE_TEXT: dict[str, str] = {
    Phase.IDLE: "未启动",
    Phase.ENV_CHECK: "正在自检运行环境…",
    Phase.ENV_ERROR: "环境自检未通过",
    Phase.STARTING: "正在启动 NapCat…",
    Phase.QUICK_LOGIN: "正在快速登录…",
    Phase.WAITING_SCAN: "等待扫码登录",
    Phase.LOGGED_IN: "已登录，正在初始化…",
    Phase.NETWORK_SETUP: "正在写入 OneBot 网络配置…",
    Phase.RESTARTING: "正在重启 NapCat 使配置生效…",
    Phase.READY: "运行中",
    Phase.STOPPED: "已停止",
    Phase.ERROR: "发生错误",
}

#: 这些阶段下 NapCat 进程应当存在
RUNNING_PHASES = {
    Phase.STARTING,
    Phase.QUICK_LOGIN,
    Phase.WAITING_SCAN,
    Phase.LOGGED_IN,
    Phase.NETWORK_SETUP,
    Phase.RESTARTING,
    Phase.READY,
}


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
class Level(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class LogSource(str, Enum):
    QLARK = "QLark"
    NAPCAT = "NapCat"


@dataclass(frozen=True)
class LogLine:
    seq: int
    ts: float
    level: str
    source: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "level": self.level,
            "source": self.source,
            "text": self.text,
        }


# --------------------------------------------------------------------------- #
# 环境自检
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EnvItem:
    key: str
    label: str
    ok: bool
    detail: str = ""
    fixable: bool = False
    #: 关键项为 False 时，不允许启动（例如 NapCat 运行时缺失）
    critical: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "ok": self.ok,
            "detail": self.detail,
            "fixable": self.fixable,
            "critical": self.critical,
        }


@dataclass
class EnvReport:
    items: list[EnvItem] = field(default_factory=list)
    checked_at: float = 0.0

    @property
    def ok(self) -> bool:
        """所有关键项通过才算通过。"""
        return all(item.ok for item in self.items if item.critical)

    def failures(self) -> list[EnvItem]:
        return [item for item in self.items if not item.ok]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_at": self.checked_at,
            "items": [item.to_dict() for item in self.items],
        }


# --------------------------------------------------------------------------- #
# 统计与二维码
# --------------------------------------------------------------------------- #
@dataclass
class Stats:
    date: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    errors: int = 0
    msg_in: int = 0
    msg_out: int = 0
    active_sessions: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.calls,
            "errors": self.errors,
            "msg_in": self.msg_in,
            "msg_out": self.msg_out,
            "active_sessions": self.active_sessions,
        }


@dataclass
class QrSnapshot:
    """当前二维码快照。``data_url`` 直接交给前端 ``<img>``。"""

    data_url: str
    mtime: float

    def to_dict(self) -> dict[str, Any]:
        return {"data_url": self.data_url, "mtime": self.mtime}


# --------------------------------------------------------------------------- #
# 应用状态快照
# --------------------------------------------------------------------------- #
@dataclass
class AppState:
    phase: str = Phase.IDLE.value
    qq: str | None = None
    nickname: str | None = None
    online: bool = False
    onebot_ready: bool = False
    napcat_running: bool = False
    pid: int | None = None
    uptime_sec: int = 0
    qrcode: QrSnapshot | None = None
    last_error: str | None = None
    theme: str = "dark"
    restart_required: bool = False
    stats: Stats = field(default_factory=Stats)
    env: EnvReport = field(default_factory=EnvReport)

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "phase_text": PHASE_TEXT.get(self.phase, self.phase),
            "qq": self.qq,
            "nickname": self.nickname,
            "online": self.online,
            "onebot_ready": self.onebot_ready,
            "napcat_running": self.napcat_running,
            "pid": self.pid,
            "uptime_sec": self.uptime_sec,
            "qrcode": self.qrcode.to_dict() if self.qrcode else None,
            "last_error": self.last_error,
            "theme": self.theme,
            "restart_required": self.restart_required,
            "stats": self.stats.to_dict(),
            "env": self.env.to_dict(),
            "server_time": time.time(),
        }
