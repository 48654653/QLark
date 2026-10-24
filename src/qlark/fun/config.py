"""趣味功能的配置：读写、校验、打码。

设计上和「模型配置」保持一致：

* 页面提交的是一整份表单，后端**逐字段校验**、只写合法的那些；
* 密钥永远只回显打码值（``qweather_key_masked``），空白表示"不改动"；
* 开关打开时，必需项缺失要**在保存时就报错**，而不是等群里有人问天气才失败。
"""

from __future__ import annotations

from typing import Any

from ..settings import Settings

#: 和风的专属域名后缀。公共域名（api.qweather.com / devapi.qweather.com）已经
#: 返回 ``Invalid Host`` —— API Host 本身就是认证的一部分，所以必须校验这个后缀。
HOST_SUFFIX = ".qweatherapi.com"

#: 默认城市（开了功能但没填城市时用）
DEFAULT_CITY = "北京"

#: 城市名长度上限（和风侧也用不到更长的）
CITY_MAX_CHARS = 20


def mask(value: str | None) -> str:
    """把 API Key 打码后给界面显示。"""
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return text[:4] + "*" * 8 + text[-4:]


def normalize_host(raw: Any) -> str:
    """把用户粘进来的东西归一成域名。

    允许直接粘 URL（``https://xxx.re.qweatherapi.com/``），也允许带路径，
    因为控制台上那行很容易连着 ``https://`` 一起复制。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].strip()
    return text.lower()


def is_valid_host(host: str) -> bool:
    return bool(host) and host.endswith(HOST_SUFFIX) and len(host) > len(HOST_SUFFIX)


def snapshot(settings: Settings) -> dict[str, Any]:
    key = str(settings.get("qweather_key") or "")
    return {
        "ok": True,
        "fun_enabled": bool(settings.get("fun_enabled", False)),
        "qweather_host": str(settings.get("qweather_host") or ""),
        "qweather_city": str(settings.get("qweather_city") or DEFAULT_CITY),
        "qweather_key_masked": mask(key),
        "qweather_key_set": bool(key),
    }


def _as_bool(value: Any, fallback: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return fallback


def apply(settings: Settings, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """校验并保存。返回 ``{ok, errors?, ...snapshot}``。

    **只写校验通过的字段**：这样用户改了城市但 Host 填错时，不会把原来能用的
    Host 一起覆盖成坏的。
    """
    payload = payload or {}
    errors: dict[str, str] = {}
    updates: dict[str, Any] = {}

    enabled = _as_bool(payload.get("fun_enabled", settings.get("fun_enabled", False)))
    updates["fun_enabled"] = enabled

    if "qweather_host" in payload:
        host = normalize_host(payload.get("qweather_host"))
        if host and not is_valid_host(host):
            errors["qweather_host"] = (
                "API Host 应该形如 xxx.re.qweatherapi.com（控制台「设置」里那一行）"
            )
        elif not host and enabled:
            errors["qweather_host"] = "启用趣味功能必须填写和风 API Host"
        else:
            updates["qweather_host"] = host

    if "qweather_key" in payload:
        key = str(payload.get("qweather_key") or "").strip()
        if key:
            if len(key) < 16:
                errors["qweather_key"] = "API Key 看起来太短了，检查是不是粘漏了"
            else:
                updates["qweather_key"] = key
        # 空串 = 不改动已保存的 Key（和「访问 Token」的约定一致）

    # 「清除」走显式标记：空串的语义是"不改"，没法用它表达"删掉"
    if payload.get("qweather_key_clear"):
        updates["qweather_key"] = ""

    if "qweather_city" in payload:
        city = str(payload.get("qweather_city") or "").strip()
        if len(city) > CITY_MAX_CHARS:
            errors["qweather_city"] = f"城市名不要超过 {CITY_MAX_CHARS} 个字"
        else:
            updates["qweather_city"] = city or DEFAULT_CITY

    # 开关打开时的最终检查：用「合并后」的值判断，避免用户只改开关却漏填
    if enabled:
        final_host = str(updates.get("qweather_host", settings.get("qweather_host") or ""))
        final_key = str(updates.get("qweather_key", settings.get("qweather_key") or ""))
        if not final_host and "qweather_host" not in errors:
            errors["qweather_host"] = "启用趣味功能必须填写和风 API Host"
        if not final_key and "qweather_key" not in errors:
            errors["qweather_key"] = "启用趣味功能必须填写和风 API Key"

    if errors:
        return {"ok": False, "errors": errors}

    if updates:
        settings.update(**updates)
    return {**snapshot(settings), "ok": True}


def resolve(settings: Settings, payload: dict[str, Any] | None = None) -> dict[str, str]:
    """取一次「测试用」的连接参数：表单里填了就用表单的，否则用已保存的。

    测试连接**不允许写库**（用户可能只是试试），所以单独走这一个函数。
    """
    payload = payload or {}
    host = normalize_host(payload.get("qweather_host")) or str(
        settings.get("qweather_host") or ""
    )
    key = str(payload.get("qweather_key") or "").strip() or str(
        settings.get("qweather_key") or ""
    )
    city = str(payload.get("qweather_city") or "").strip() or str(
        settings.get("qweather_city") or DEFAULT_CITY
    )
    return {"host": host, "key": key, "city": city}


__all__ = [
    "CITY_MAX_CHARS",
    "DEFAULT_CITY",
    "HOST_SUFFIX",
    "apply",
    "is_valid_host",
    "mask",
    "normalize_host",
    "resolve",
    "snapshot",
]
