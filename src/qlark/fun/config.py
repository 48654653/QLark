"""趣味功能的配置：读写、校验、打码。

设计上和「模型配置」保持一致：

* 页面提交的是一整份表单，后端**逐字段校验**、只写合法的那些；
* 密钥永远只回显打码值（``qweather_key_masked``），空白表示"不改动"；
* 开关打开时，必需项缺失要**在保存时就报错**，而不是等群里有人问天气才失败。
"""

from __future__ import annotations

import re
from typing import Any

from ..settings import Settings
from .weather import DEFAULT_KEYWORDS

#: 和风的专属域名后缀。公共域名（api.qweather.com / devapi.qweather.com）已经
#: 返回 ``Invalid Host`` —— API Host 本身就是认证的一部分，所以必须校验这个后缀。
HOST_SUFFIX = ".qweatherapi.com"

#: 默认城市（开了功能但没填城市时用）
DEFAULT_CITY = "北京"

#: 城市名长度上限（和风侧也用不到更长的）
CITY_MAX_CHARS = 20

#: 触发词表的上限：条数与单条长度
KEYWORD_MAX_ITEMS = 30
KEYWORD_MAX_CHARS = 12

#: 分隔符（用户可能粘一行逗号/顿号/换行的词进来）
_KEYWORD_SPLIT = re.compile(r"[\n,，、;；]+")


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


def weather_on(settings: Settings) -> bool:
    """天气开关。

    v2.3.2 起"每个功能各自一个开关"，所以主键是 ``weather_enabled``；
    之前那版只有一个 ``fun_enabled`` 总开关，这里做兼容（没设新键就沿用旧键）。
    """
    value = settings.get("weather_enabled")
    if value is None:
        return bool(settings.get("fun_enabled", False))
    return bool(value)


def normalize_keywords(raw: Any) -> list[str]:
    """把用户输入（数组或一行逗号/顿号/换行分隔的词）归一成词表。"""
    if isinstance(raw, str):
        parts: list[str] = _KEYWORD_SPLIT.split(raw)
    elif isinstance(raw, (list, tuple, set)):
        parts = [str(item) for item in raw]
    else:
        parts = []

    result: list[str] = []
    for part in parts:
        word = " ".join(str(part).split())
        if not word:
            continue
        word = word[:KEYWORD_MAX_CHARS]
        if word not in result:
            result.append(word)
        if len(result) >= KEYWORD_MAX_ITEMS:
            break
    return result


def keywords(settings: Settings) -> list[str]:
    """当前生效的触发词表。空表 = 用内置默认（用户清空不该等于"永不再查"）。"""
    items = normalize_keywords(settings.get("weather_keywords"))
    return items or list(DEFAULT_KEYWORDS)


def snapshot(settings: Settings) -> dict[str, Any]:
    key = str(settings.get("qweather_key") or "")
    return {
        "ok": True,
        "weather_enabled": weather_on(settings),
        "weather_keywords": keywords(settings),
        # 默认词表也回给前端：「恢复默认」按钮直接用，避免前后端各存一份
        "weather_default_keywords": list(DEFAULT_KEYWORDS),
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

    if "weather_enabled" in payload:
        enabled = _as_bool(payload.get("weather_enabled"))
    elif "fun_enabled" in payload:  # 老前端兼容（那时只有一个总开关）
        enabled = _as_bool(payload.get("fun_enabled"))
    else:
        enabled = weather_on(settings)
    updates["weather_enabled"] = enabled
    # 旧键同步写一份：万一回退到 v2.3.1 的界面，开关状态还是对的
    updates["fun_enabled"] = enabled

    if "weather_keywords" in payload:
        words = normalize_keywords(payload.get("weather_keywords"))
        if enabled and not words:
            errors["weather_keywords"] = "开天气功能至少要留一个触发词（或点「恢复默认」）"
        else:
            updates["weather_keywords"] = words

    if "qweather_host" in payload:
        host = normalize_host(payload.get("qweather_host"))
        if host and not is_valid_host(host):
            errors["qweather_host"] = (
                "API Host 应该形如 xxx.re.qweatherapi.com（控制台「设置」里那一行）"
            )
        elif not host and enabled:
            errors["qweather_host"] = "开天气功能必须填写和风 API Host"
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
            errors["qweather_host"] = "开天气功能必须填写和风 API Host"
        if not final_key and "qweather_key" not in errors:
            errors["qweather_key"] = "开天气功能必须填写和风 API Key"

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
    "KEYWORD_MAX_CHARS",
    "KEYWORD_MAX_ITEMS",
    "apply",
    "is_valid_host",
    "keywords",
    "mask",
    "normalize_host",
    "normalize_keywords",
    "resolve",
    "snapshot",
    "weather_on",
]
