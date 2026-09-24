"""机器人运行时：把「事件通道」与「消息流水线」绑成一体。

.. code-block:: text

    NapCat ──POST 事件──> EventServer ──> MessagePipeline ──> 模型 ──> send_msg ──> NapCat

登录就绪后启动，NapCat 停止时一起停掉。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .chat.pipeline import MessagePipeline
from .core.models import LogSource
from .events.server import EventServer
from .napcat.logbus import LogBus
from .settings import Settings
from .storage.db import Database

log = logging.getLogger(__name__)


class BotService:
    """消息收发总开关。"""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        logbus: LogBus,
        *,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self.logbus = logbus
        self._on_change = on_change or (lambda: None)

        self.pipeline = MessagePipeline(settings, db, logbus, on_change=self._on_change)
        self.event_server = EventServer(
            host=settings.event_host,
            port=settings.event_port,
            token=settings.onebot_token,
            on_event=self.pipeline.submit,
            logbus=logbus,
        )
        self.last_error: str | None = None

    # ================================================================== 生命周期
    def start(self) -> bool:
        """启动事件通道与流水线。成功返回 ``True``。"""
        if self.event_server.running:
            return True

        try:
            self.event_server.start()
        except OSError as exc:
            self.last_error = str(exc)
            self._log(
                f"事件服务启动失败：{exc}。改端口后需要重启 NapCat 才能生效，"
                "请检查「事件端口」设置是否被占用。",
                level="error",
            )
            self._on_change()
            return False

        self.last_error = None
        self.pipeline.start()
        self._on_change()
        return True

    def stop(self, *, wait: bool = True) -> None:
        self.pipeline.stop()
        self.event_server.stop()
        self._on_change()

    @property
    def running(self) -> bool:
        return self.event_server.running and self.pipeline.running

    # ================================================================== 暂停
    def set_paused(self, paused: bool) -> bool:
        return self.pipeline.set_paused(paused)

    @property
    def paused(self) -> bool:
        return self.pipeline.paused

    # ================================================================== 状态
    def snapshot(self) -> dict[str, Any]:
        return {
            "bot": self.pipeline.snapshot(),
            "event_server": self.event_server.snapshot(),
        }

    def _log(self, message: str, *, level: str = "info") -> None:
        self.logbus.add(message, level=level, source=LogSource.QLARK.value)


__all__ = ["BotService"]
