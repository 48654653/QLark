"""OneBot 11 HTTP 客户端。

调用约定（实测固化）：

.. code-block:: text

    POST http://127.0.0.1:3000/<action>
    Content-Type: application/json
    Authorization: Bearer <token>

    {"参数名": "值"}     // 无参数时传 {}

响应里有个 **非标准字段** ``"stream"``（NapCat v4.8.115+ 引入 Stream API 后新增，
普通接口值为 ``"normal-action"``）—— 写解析代码时不要把它当成异常。
"""

from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger(__name__)


class OneBotError(RuntimeError):
    """OneBot 调用失败。"""


class OneBotAuthError(OneBotError):
    """token 校验失败。"""


class OneBotTimeout(OneBotError):
    """请求超时。"""


class OneBotClient:
    def __init__(self, base_url: str, token: str = "", *, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._session = requests.Session()
        # 本机 OneBot 调用**绝对不能走系统代理**。
        #
        # Windows 上 WinINET 的 ProxyOverride 白名单（通常含 127.*）在 Python 这边是
        # **不生效的** —— urllib 读注册表时只取 ProxyServer/ProxyEnable，忽略白名单。
        # 实测（2026-09-25）：本机配了 127.0.0.1:7890，于是连 http://127.0.0.1:3000
        # 的请求也被丢给代理，探针偶发超时 → 扫码登录后认不出账号；更糟的是
        # send_msg 也走代理，表现成"偶发发不出消息"。
        # 模型请求走代理是**对的**（要翻墙访问远端 API），只有本机调用必须绕开。
        self._session.trust_env = False

    # ------------------------------------------------------------------ 调用
    def call(self, action: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> dict[str, Any]:
        url = f"{self.base_url}/{action}"
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        try:
            response = self._session.post(
                url,
                json=params or {},
                headers=headers,
                timeout=timeout or self.timeout,
            )
        except requests.Timeout as exc:
            raise OneBotTimeout(f"{action} 请求超时（{self.base_url}）") from exc
        except requests.RequestException as exc:
            raise OneBotError(f"无法连接 OneBot 服务端 {self.base_url}：{exc}") from exc

        if response.status_code != 200:
            raise OneBotError(f"{action} 返回 HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise OneBotError(f"{action} 返回内容不是 JSON：{response.text[:200]}") from exc

        if not isinstance(payload, dict):
            raise OneBotError(f"{action} 返回结构异常：{payload!r:.120}")

        retcode = payload.get("retcode")
        if retcode not in (0, None):
            message = str(payload.get("message") or payload.get("wording") or "")
            if "token" in message.lower():
                raise OneBotAuthError(f"{action} 鉴权失败：{message}")
            raise OneBotError(f"{action} 失败（retcode={retcode}）：{message}")

        return payload

    # ------------------------------------------------------------------ 快捷接口
    def get_status(self) -> dict[str, Any]:
        return self.call("get_status")

    def get_login_info(self) -> dict[str, Any]:
        return self.call("get_login_info")

    def get_version_info(self) -> dict[str, Any]:
        return self.call("get_version_info")

    # ------------------------------------------------------------------ 资源
    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # pragma: no cover
            pass

    def __enter__(self) -> "OneBotClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
