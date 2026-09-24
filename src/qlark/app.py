"""应用装配层：把存储、日志、NapCat 管理、界面桥接到一起。"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import paths
from .bot import BotService
from .chat.personas import PersonaError
from .core.models import AppState, Phase
from .logging_setup import setup_logging
from .napcat import netconfig
from .napcat.envcheck import EnvChecker, detect_credential_accounts
from .napcat.logbus import LogBus
from .napcat.login import LoginError, LoginManager
from .settings import Settings
from .storage.db import Database
from .version import WINDOW_TITLE
from . import model_config

log = logging.getLogger(__name__)

#: 每次推送最多带多少行日志（超出的留到下一拍，避免 evaluate_js 卡顿）
LOG_PUSH_BATCH = 300
#: 打开界面时预填的历史日志行数
LOG_BACKFILL = 300
#: 无变化时状态推送的心跳间隔（用于刷新运行时长）
STATE_HEARTBEAT = 3.0


class Application:
    """QLark 主应用。"""

    def __init__(self, *, debug: bool = False) -> None:
        self.debug = debug
        paths.ensure_data_dirs()

        self.db = Database(paths.db_path())
        self.settings = Settings(self.db)
        self.logbus = LogBus(capacity=5000, log_dir=paths.log_dir())
        setup_logging(self.logbus, debug=debug)

        log.info("QLark 启动，数据目录：%s", paths.data_dir())
        log.info("程序目录：%s", paths.app_dir())

        self.login = LoginManager(
            self.settings,
            self.logbus,
            on_change=self.mark_dirty,
            on_ready=self._on_login_ready,
            on_stopped=self._on_login_stopped,
        )
        self.bot = BotService(
            self.settings, self.db, self.logbus, on_change=self.mark_dirty
        )

        self._window: Any = None
        self._dirty = threading.Event()
        self._shutdown_event = threading.Event()
        self._pump_thread: threading.Thread | None = None
        self._last_log_seq = 0
        self._last_state_push = 0.0
        self._auto_started = False

    # ================================================================== 状态
    def state(self) -> dict[str, Any]:
        snap = self.login.snapshot()
        stats = self.db.stats_today()
        app_state = AppState(
            phase=snap["phase"],
            qq=snap["qq"] or self.settings.qq_account,
            nickname=snap["nickname"] or self.settings.qq_nickname,
            online=bool(snap["online"]),
            onebot_ready=bool(snap["onebot_ready"]),
            napcat_running=bool(snap["napcat_running"]),
            pid=snap["pid"],
            uptime_sec=int(snap["uptime_sec"]),
            qrcode=snap["qrcode"],
            last_error=snap["last_error"],
            theme=self.settings.theme,
            restart_required=self.settings.restart_required,
            stats=stats,
            env=snap["env"],
        )
        payload = app_state.to_dict()
        payload.update(self.bot.snapshot())
        return payload

    # ================================================================== 登录联动
    def _on_login_ready(self) -> None:
        """登录就绪 → 打开事件通道，开始收发消息。"""
        self.bot.start()
        self.mark_dirty()

    def _on_login_stopped(self) -> None:
        """NapCat 停了 → 机器人也停，避免残留的事件服务占着端口。"""
        if self.bot.running:
            self.bot.stop(wait=False)
        self.mark_dirty()

    def set_paused(self, paused: bool) -> None:
        self.bot.set_paused(paused)
        self.mark_dirty()

    # ================================================================== M3：模型配置
    @property
    def personas(self):
        return self.bot.pipeline.personas

    def get_model_config(self) -> dict[str, Any]:
        return model_config.snapshot(self.settings)

    def apply_model_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = model_config.apply_model(self.settings, payload or {})
        if result.get("ok"):
            # 配置变了就让缓存的客户端失效
            self.bot.pipeline.invalidate_clients()
        self.mark_dirty()
        return result

    def apply_napcat_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = model_config.apply_napcat(self.settings, payload or {})
        self.mark_dirty()
        return result

    def test_model(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return model_config.test_model(self.settings, payload or {})

    # ================================================================== M3：账号
    def list_accounts(self) -> dict[str, Any]:
        credentials = set(detect_credential_accounts())
        known = set(netconfig.detect_accounts())
        current = self.settings.qq_account
        accounts = [
            {
                "qq": qq,
                "nickname": self.settings.qq_nickname if qq == current else None,
                "has_credential": qq in credentials,
                "known_by_qlark": qq in known,
                "current": qq == current,
            }
            for qq in sorted(credentials | known)
        ]
        return {"current": current, "accounts": accounts}

    def login_as(self, qq: str) -> dict[str, Any]:
        try:
            self.login.switch_account(str(qq))
        except LoginError as exc:
            return {"ok": False, "error": str(exc)}
        self.mark_dirty()
        return {"ok": True}

    # ================================================================== M3：人设
    def list_personas(self) -> dict[str, Any]:
        from .chat.personas import DEFAULT_PERSONA_UID

        return {
            "default_uid": DEFAULT_PERSONA_UID,
            "personas": self.personas.list_public(),
        }

    def create_persona(self) -> dict[str, Any]:
        try:
            result = self.personas.create()
        except PersonaError as exc:
            return {"ok": False, "error": str(exc)}
        self.mark_dirty()
        return result

    def update_persona(self, uid: int, fields: dict[str, Any]) -> dict[str, Any]:
        try:
            result = self.personas.update(int(uid), fields or {})
        except PersonaError as exc:
            return {"ok": False, "error": str(exc)}
        self.mark_dirty()
        return result

    def delete_persona(self, uid: int) -> dict[str, Any]:
        try:
            result = self.personas.delete(int(uid))
        except PersonaError as exc:
            return {"ok": False, "error": str(exc)}
        self.logbus.add(
            f"已删除人设 {uid}：{result.affected_sessions} 个会话回落到人设1，"
            f"删除 {result.deleted_messages} 条记忆",
            level="warn",
        )
        self.mark_dirty()
        return result.to_dict()

    def reset_persona_memory(self, uid: int) -> dict[str, Any]:
        try:
            result = self.personas.clear_memory(int(uid))
        except PersonaError as exc:
            return {"ok": False, "error": str(exc)}
        self.logbus.add(f"已清空人设 {uid} 的记忆（{result.get('deleted_messages')} 条）", level="warn")
        self.mark_dirty()
        return result

    def get_persona_permission(self) -> dict[str, Any]:
        return {
            "whitelist_only": bool(self.settings.get("persona_whitelist_only", True)),
            "whitelist": self.personas.whitelist(),
        }

    def set_persona_permission(self, whitelist_only: bool) -> dict[str, Any]:
        self.settings.set("persona_whitelist_only", bool(whitelist_only))
        self.mark_dirty()
        return {"ok": True, "whitelist_only": bool(whitelist_only)}

    def add_persona_whitelist(self, qq: str) -> dict[str, Any]:
        try:
            whitelist = self.personas.add_whitelist(qq)
        except PersonaError as exc:
            return {"ok": False, "error": str(exc)}
        self.mark_dirty()
        return {"ok": True, "whitelist": whitelist}

    def remove_persona_whitelist(self, qq: str) -> dict[str, Any]:
        whitelist = self.personas.remove_whitelist(qq)
        self.mark_dirty()
        return {"ok": True, "whitelist": whitelist}

    def mark_dirty(self) -> None:
        self._dirty.set()

    # ================================================================== 推送
    def attach_window(self, window: Any) -> None:
        self._window = window

    def _eval(self, script: str) -> None:
        window = self._window
        if window is None:
            return
        try:
            window.evaluate_js(script)
        except Exception as exc:  # 界面还没就绪时调用会失败，忽略即可
            log.debug("evaluate_js 失败：%s", exc)

    def push_state(self) -> None:
        payload = json.dumps(self.state(), ensure_ascii=False)
        self._eval(f"window.qlark && window.qlark.onState({payload})")
        self._last_state_push = time.monotonic()

    def push_logs(self) -> None:
        lines = self.logbus.since(self._last_log_seq, limit=LOG_PUSH_BATCH)
        if not lines:
            return
        self._last_log_seq = lines[-1].seq
        payload = json.dumps([line.to_dict() for line in lines], ensure_ascii=False)
        self._eval(f"window.qlark && window.qlark.onLog({payload})")

    def _pump_loop(self) -> None:
        """定时器线程：批量推送日志与状态。"""
        while not self._shutdown_event.wait(0.25):
            try:
                self.push_logs()
                if self._dirty.is_set():
                    self._dirty.clear()
                    self.push_state()
                elif time.monotonic() - self._last_state_push > STATE_HEARTBEAT:
                    if self.login.is_running:
                        self.push_state()
            except Exception:
                log.exception("状态推送异常")

    def _start_pump(self) -> None:
        if self._pump_thread is not None and self._pump_thread.is_alive():
            return
        self._shutdown_event.clear()
        self._pump_thread = threading.Thread(
            target=self._pump_loop, name="qlark-pump", daemon=True
        )
        self._pump_thread.start()

    def _on_window_loaded(self) -> None:
        """界面 DOM 就绪：回填历史日志并推送一次完整状态。"""
        tail = self.logbus.tail(LOG_BACKFILL)
        if tail:
            self._last_log_seq = tail[-1].seq
            payload = json.dumps([line.to_dict() for line in tail], ensure_ascii=False)
            self._eval(f"window.qlark && window.qlark.onLog({payload})")
        self.push_state()
        self._auto_start()

    def _auto_start(self) -> None:
        """双击即开：界面就绪后自动拉起 NapCat 并完成登录。"""
        if self._auto_started:
            return
        self._auto_started = True

        def worker() -> None:
            time.sleep(1.0)
            self.start_napcat()

        threading.Thread(target=worker, name="qlark-autostart", daemon=True).start()

    # ================================================================== 动作
    def start_napcat(self) -> None:
        if self.login.is_running:
            self.logbus.add("NapCat 已在运行，忽略启动请求", level="debug")
            return
        self.login.start()

    def stop_napcat(self) -> None:
        self.login.stop(wait=False)
        self.mark_dirty()

    def restart_napcat(self) -> None:
        self.logbus.add("正在重启 NapCat…", level="info")
        self.login.restart()

    def relogin(self) -> None:
        self.logbus.add("已清除本地账号记录，将重新扫码登录", level="warn")
        self.login.relogin()

    def logout(self) -> None:
        self.login.logout()

    def run_env_check(self) -> dict[str, Any]:
        report = self.login.run_env_check(with_ports=not self.login.is_running)
        self.mark_dirty()
        return report.to_dict()

    def get_logs(self, since_seq: int = 0) -> dict[str, Any]:
        start = int(since_seq or 0)
        lines = self.logbus.since(start, limit=LOG_PUSH_BATCH)
        next_seq = lines[-1].seq if lines else start
        dropped = 0
        if start and lines and lines[0].seq > start + 1:
            # 客户端落后太多，缓冲里最早的序号已经被挤掉了
            dropped = lines[0].seq - start - 1
        return {"lines": [line.to_dict() for line in lines], "next": next_seq, "dropped": dropped}

    def clear_logs(self) -> None:
        self.logbus.clear()
        self._last_log_seq = self.logbus.latest_seq
        self.logbus.add("日志已清空", level="info")

    def export_logs(self) -> str:
        """导出全部日志文本（前端触发下载）。"""
        return self.logbus.export_text()

    def set_theme(self, theme: str) -> None:
        self.settings.theme = theme
        self.mark_dirty()

    # ================================================================== 系统
    def _open_path(self, target: Path, *, create: bool = False) -> None:
        try:
            if create:
                target.mkdir(parents=True, exist_ok=True)
            if target.exists():
                os.startfile(str(target))  # type: ignore[attr-defined]  # noqa: S606
            else:
                self.logbus.add(f"路径不存在：{target}", level="warn")
        except Exception as exc:
            self.logbus.add(f"打开路径失败：{exc}", level="error")

    def open_data_dir(self) -> None:
        self._open_path(paths.data_dir(), create=True)

    def open_runtime_dir(self) -> None:
        self._open_path(paths.napcat_dir())

    def open_log_dir(self) -> None:
        self._open_path(paths.log_dir(), create=True)

    # ================================================================== 生命周期
    def run(self, *, on_ready: Callable[[Any], None] | None = None) -> int:
        """启动图形界面（阻塞直到窗口关闭）。

        Args:
            on_ready: 可选的窗口就绪回调，收到 ``window`` 对象。供自动化脚本
                （截图、界面自检）在真实后端下驱动界面使用。
        """
        import webview  # 延迟导入：headless 模式不需要它

        index = paths.resolve_web_index()
        if not index.is_file():
            raise FileNotFoundError(f"找不到前端入口文件：{index}")

        from .api import JsApi

        api = JsApi(self)
        window = webview.create_window(
            WINDOW_TITLE,
            url=index.as_uri(),
            js_api=api,
            width=1180,
            height=780,
            min_size=(1000, 660),
            background_color="#12141a",
            text_select=True,
        )
        self.attach_window(window)
        window.events.loaded += self._on_window_loaded
        if on_ready is not None:
            window.events.loaded += lambda: on_ready(window)
        self._start_pump()

        log.info("界面已启动：%s", index)
        # private_mode=False：保留 localStorage，主题选择才能被记住
        webview.start(debug=self.debug, private_mode=False)

        self.shutdown()
        return 0

    def request_quit(self) -> None:
        """前端请求退出程序。

        M1 的关闭行为是"退出程序并停掉 NapCat"（安全、可预期）。
        按需求规格 v1.1 §3.1 应改为"最小化到托盘"，托盘与右键菜单留待后续实现。
        """
        window = self._window
        if window is not None:
            try:
                window.destroy()
                return
            except Exception:
                log.exception("销毁窗口失败")
        self.shutdown()

    def shutdown(self) -> None:
        """退出：停掉 NapCat 与后台线程，关闭数据库。"""
        if self._shutdown_event.is_set():
            return
        log.info("QLark 正在退出…")
        self._shutdown_event.set()
        try:
            self.bot.stop(wait=True)
        except Exception:
            log.exception("停止机器人失败")
        try:
            self.login.stop(wait=True)
        except Exception:
            log.exception("停止 NapCat 失败")
        thread = self._pump_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
        try:
            self.logbus.add("QLark 已退出", level="info")
            self.logbus.close()
        finally:
            self.db.close()

    # 便于测试直接注入
    def env_checker(self) -> EnvChecker:
        return EnvChecker(
            host=self.settings.onebot_host,
            port=self.settings.onebot_port,
            event_port=self.settings.event_port,
        )

    @property
    def phase(self) -> Phase:
        return self.login.phase
