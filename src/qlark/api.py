"""暴露给前端的 JS API。

前端通过 ``window.pywebview.api.<method>()`` 调用这里的方法（返回 Promise）。
方法名与 ``src/qlark/web/app.js`` 中的调用一一对应，改名必须同步。
"""

from __future__ import annotations

import logging
from typing import Any

from .app import Application

log = logging.getLogger(__name__)


class JsApi:
    """pywebview 的 ``js_api`` 桥接对象。"""

    def __init__(self, app: Application) -> None:
        self._app = app

    # ------------------------------------------------------------------ 状态
    def get_state(self) -> dict[str, Any]:
        return self._app.state()

    # ------------------------------------------------------------------ 控制
    def start(self) -> dict[str, Any]:
        self._app.start_napcat()
        return self._app.state()

    def stop(self) -> dict[str, Any]:
        self._app.stop_napcat()
        return self._app.state()

    def restart_napcat(self) -> dict[str, Any]:
        self._app.restart_napcat()
        return self._app.state()

    def run_env_check(self) -> dict[str, Any]:
        self._app.run_env_check()
        return self._app.state()

    def relogin(self) -> dict[str, Any]:
        self._app.relogin()
        return self._app.state()

    def logout(self) -> dict[str, Any]:
        self._app.logout()
        return self._app.state()

    # ------------------------------------------------------------------ 机器人
    def set_paused(self, paused: bool) -> dict[str, Any]:
        self._app.set_paused(bool(paused))
        return {"paused": self._app.bot.paused}

    # ------------------------------------------------------------------ 模型配置
    def get_model_config(self) -> dict[str, Any]:
        return self._app.get_model_config()

    def apply_model_config(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._app.apply_model_config(payload or {})

    def apply_napcat_config(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._app.apply_napcat_config(payload or {})

    def test_model(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._app.test_model(payload or {})

    # ------------------------------------------------------------------ 账号
    def list_accounts(self) -> dict[str, Any]:
        return self._app.list_accounts()

    def login_as(self, qq: str) -> dict[str, Any]:
        return self._app.login_as(qq)

    # ------------------------------------------------------------------ 人设
    def list_personas(self) -> dict[str, Any]:
        return self._app.list_personas()

    def create_persona(self) -> dict[str, Any]:
        return self._app.create_persona()

    def update_persona(self, uid: int, fields: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._app.update_persona(int(uid), fields or {})

    def delete_persona(self, uid: int) -> dict[str, Any]:
        return self._app.delete_persona(int(uid))

    def reset_persona_memory(self, uid: int) -> dict[str, Any]:
        return self._app.reset_persona_memory(int(uid))

    def get_persona_permission(self) -> dict[str, Any]:
        return self._app.get_persona_permission()

    def set_persona_permission(self, whitelist_only: bool) -> dict[str, Any]:
        return self._app.set_persona_permission(bool(whitelist_only))

    def add_persona_whitelist(self, qq: str) -> dict[str, Any]:
        return self._app.add_persona_whitelist(qq)

    def remove_persona_whitelist(self, qq: str) -> dict[str, Any]:
        return self._app.remove_persona_whitelist(qq)

    # ------------------------------------------------------------------ 日志
    def get_logs(self, since_seq: int = 0) -> dict[str, Any]:
        return self._app.get_logs(since_seq)

    def clear_logs(self) -> dict[str, Any]:
        self._app.clear_logs()
        return {"ok": True}

    def export_logs(self) -> str:
        return self._app.export_logs()

    # ------------------------------------------------------------------ 系统
    def set_theme(self, theme: str) -> dict[str, Any]:
        self._app.set_theme(theme)
        return {"theme": self._app.settings.theme}

    def open_data_dir(self) -> None:
        self._app.open_data_dir()

    def open_runtime_dir(self) -> None:
        self._app.open_runtime_dir()

    def open_log_dir(self) -> None:
        self._app.open_log_dir()

    def quit_app(self) -> None:
        self._app.request_quit()

    # ------------------------------------------------------------------ 元信息
    def get_app_info(self) -> dict[str, Any]:
        from .version import APP_NAME, APP_NAME_CN, __version__

        return {
            "name": APP_NAME,
            "name_cn": APP_NAME_CN,
            "version": __version__,
        }
