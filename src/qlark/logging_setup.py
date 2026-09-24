"""把 Python 标准库 ``logging`` 的日志接进 :class:`LogBus`。

这样界面上的日志面板既能看到 QLark 自身的日志，也能看到 NapCat 的原始输出，
且来源字段可区分。
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from .core.models import Level, LogSource
from .napcat.logbus import LogBus

_LEVEL_MAP: dict[int, str] = {
    logging.DEBUG: Level.DEBUG.value,
    logging.INFO: Level.INFO.value,
    logging.WARNING: Level.WARN.value,
    logging.ERROR: Level.ERROR.value,
    logging.CRITICAL: Level.ERROR.value,
}


class LogBusHandler(logging.Handler):
    """把日志记录转发到 LogBus（来源标记为 QLark）。"""

    def __init__(self, logbus: LogBus, level: int = logging.INFO) -> None:
        super().__init__(level)
        self.logbus = logbus
        self.setFormatter(logging.Formatter("%(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()
        try:
            self.logbus.add(
                message,
                level=_LEVEL_MAP.get(record.levelno, Level.INFO.value),
                source=LogSource.QLARK.value,
            )
        except Exception:  # 日志本身不能抛异常
            pass


def setup_logging(logbus: LogBus, *, debug: bool = False) -> None:
    """安装全局日志配置。"""
    root = logging.getLogger()
    level = logging.DEBUG if debug else logging.INFO
    root.setLevel(level)

    # 清掉可能存在的重复 handler（例如测试里重复调用）
    for handler in list(root.handlers):
        root.removeHandler(handler)

    bus_handler = LogBusHandler(logbus, level=level)
    bus_handler.setLevel(level)
    root.addHandler(bus_handler)

    # 开发期同时在控制台输出，方便 headless 调试
    if sys.stderr is not None and sys.stderr.isatty():
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(level)
        console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(console)

    # requests / urllib3 的连接日志太吵，压到 WARNING
    for noisy in ("urllib3", "requests", "webview", "psutil"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_config_summary(logger: logging.Logger, settings: Any) -> None:
    """启动时打印一份脱敏配置摘要，便于排障。"""
    logger.info(
        "配置摘要：base_url=%s model=%s onebot=%s memory_rounds=%s auto_summary=%s",
        getattr(settings, "onebot_base_url", "-"),
        "(未设置)" if not settings.get("model") else settings.get("model"),
        settings.onebot_base_url,
        settings.get("memory_rounds"),
        settings.get("auto_summary"),
    )
