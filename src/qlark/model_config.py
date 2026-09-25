"""模型配置与 NapCat 连接配置的读写、校验与连通性测试。

分界很清楚：

* **模型侧**（Base URL / Key / 模型名 / 参数 / 记忆参数）→ 应用后**立即生效**，
  不需要重启任何东西（流水线每次调用前都重新读配置）。
* **NapCat 侧**（OneBot 端口 / 事件端口 / token）→ 写进 ``onebot11_<QQ>.json``，
  而**配置文件不支持热重载**，必须重启 NapCat 才生效。所以这里只置一个
  ``restart_required`` 标记，由界面提示用户去点「立即重启」。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .chat.moderation import normalize_words
from .llm.client import LLMError, OpenAIChatClient, normalize_base_url
from .settings import Settings
from .version import DEFAULT_ONEBOT_TOKEN

log = logging.getLogger(__name__)

#: 模型侧可写的字段
MODEL_FIELDS = (
    "base_url",
    "model",
    "vision_model",
    "summary_model",
    "max_tokens",
    "temperature",
    "memory_rounds",
    "auto_summary",
    "reply_quote_group",
    "message_label",
    "max_reply_chars",
    "notify_error_private",
    # ---- 风控（需求规格 §9.3）----
    "rate_limit_per_minute",
    "blocked_words",
)

#: NapCat 侧可写的字段（改任何一项都要重启 NapCat）
NAPCAT_FIELDS = ("onebot_host", "onebot_port", "event_host", "event_port")


def mask(value: str | None) -> str:
    """把密钥打码后给界面显示。"""
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}{'*' * (len(text) - 8)}{text[-4:]}"


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
def snapshot(settings: Settings) -> dict[str, Any]:
    api_key = str(settings.get("api_key") or "")
    token = settings.onebot_token
    return {
        "base_url": str(settings.get("base_url") or ""),
        "api_key_masked": mask(api_key),
        "api_key_set": bool(api_key),
        "model": str(settings.get("model") or ""),
        "vision_model": str(settings.get("vision_model") or ""),
        "summary_model": str(settings.get("summary_model") or ""),
        "max_tokens": settings.get("max_tokens"),
        "temperature": settings.get("temperature"),
        "memory_rounds": settings.get("memory_rounds"),
        "auto_summary": bool(settings.get("auto_summary")),
        "reply_quote_group": bool(settings.get("reply_quote_group")),
        "message_label": bool(settings.get("message_label", True)),
        "max_reply_chars": settings.get("max_reply_chars"),
        "notify_error_private": bool(settings.get("notify_error_private")),
        "rate_limit_per_minute": settings.get("rate_limit_per_minute"),
        "blocked_words": list(settings.get("blocked_words") or []),
        "onebot_host": settings.onebot_host,
        "onebot_port": settings.onebot_port,
        "event_host": settings.event_host,
        "event_port": settings.event_port,
        "token_masked": mask(token),
        "token_set": bool(token),
        "restart_required": settings.restart_required,
    }


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _port_error(value: Any, label: str) -> str | None:
    port = _as_int(value)
    if port is None:
        return f"{label}必须是数字"
    if not 1 <= port <= 65535:
        return f"{label}必须在 1~65535 之间"
    return None


def validate_model(payload: dict[str, Any], current: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}

    if "base_url" in payload:
        value = str(payload.get("base_url") or "").strip()
        if not value:
            errors["base_url"] = "Base URL 不能为空"
        else:
            try:
                normalize_base_url(value)
            except LLMError as exc:
                errors["base_url"] = str(exc)

    if payload.get("api_key"):
        if len(str(payload["api_key"])) > 512:
            errors["api_key"] = "API Key 过长"

    model = payload.get("model", current.get("model"))
    if "model" in payload and not str(model or "").strip():
        errors["model"] = "模型名不能为空"

    for field, label in (("vision_model", "视觉模型"), ("summary_model", "总结模型")):
        if field in payload and len(str(payload.get(field) or "")) > 128:
            errors[field] = f"{label}名最长 128 个字符"

    if "max_tokens" in payload:
        value = _as_int(payload["max_tokens"])
        if value is None:
            errors["max_tokens"] = "最大输出 Token 不能为空" if _is_blank(payload["max_tokens"]) else "最大输出 Token 必须是整数"
        elif not 1 <= value <= 32000:
            errors["max_tokens"] = "最大输出 Token 必须在 1~32000 之间"

    if "temperature" in payload:
        value = _as_float(payload["temperature"])
        if value is None:
            errors["temperature"] = "温度不能为空" if _is_blank(payload["temperature"]) else "温度必须是数字"
        elif not 0.0 <= value <= 2.0:
            errors["temperature"] = "温度必须在 0~2 之间"

    if "memory_rounds" in payload:
        value = _as_int(payload["memory_rounds"])
        if value is None:
            errors["memory_rounds"] = "记忆轮数不能为空" if _is_blank(payload["memory_rounds"]) else "记忆轮数必须是整数"
        elif not 1 <= value <= 200:
            errors["memory_rounds"] = "记忆轮数必须在 1~200 之间"

    if "max_reply_chars" in payload:
        value = _as_int(payload["max_reply_chars"])
        if value is None:
            errors["max_reply_chars"] = "最大字数不能为空" if _is_blank(payload["max_reply_chars"]) else "最大字数必须是整数"
        elif not 100 <= value <= 5000:
            errors["max_reply_chars"] = "单条回复最大字数必须在 100~5000 之间"

    if "rate_limit_per_minute" in payload:
        value = _as_int(payload["rate_limit_per_minute"])
        if value is None:
            errors["rate_limit_per_minute"] = (
                "速率上限不能为空" if _is_blank(payload["rate_limit_per_minute"]) else "速率上限必须是整数"
            )
        elif not 0 <= value <= 600:
            errors["rate_limit_per_minute"] = "速率上限必须在 0~600 之间（0 表示不限制）"

    if "blocked_words" in payload and not isinstance(payload["blocked_words"], (list, tuple, str)):
        errors["blocked_words"] = "敏感词格式不对"

    return errors


def _is_blank(value: Any) -> bool:
    """界面把清空的数字框发成 ``null``（而不是 0），要能识别成"没填"。"""
    return value is None or (isinstance(value, str) and not value.strip())


def validate_napcat(payload: dict[str, Any], *, onebot_port: int, event_port: int) -> dict[str, str]:
    errors: dict[str, str] = {}
    for field, label in (("onebot_port", "OneBot 端口"), ("event_port", "事件端口")):
        if field in payload:
            error = _port_error(payload[field], label)
            if error:
                errors[field] = error

    for field, label in (("onebot_host", "监听地址"), ("event_host", "事件监听地址")):
        if field in payload:
            value = str(payload.get(field) or "").strip()
            if not value:
                errors[field] = f"{label}不能为空"
            elif not _is_local_host(value):
                errors[field] = "只允许本机地址（127.0.0.1 / localhost / ::1），不要把 QQ 账号暴露到公网"

    final_onebot = _as_int(payload.get("onebot_port")) or onebot_port
    final_event = _as_int(payload.get("event_port")) or event_port
    if final_onebot == final_event:
        errors["event_port"] = "事件端口不能与 OneBot 端口相同"
    return errors


def _is_local_host(value: str) -> bool:
    return value.lower() in ("127.0.0.1", "localhost", "::1")


# --------------------------------------------------------------------------- #
# 应用
# --------------------------------------------------------------------------- #
def apply_model(settings: Settings, payload: dict[str, Any]) -> dict[str, Any]:
    current = snapshot(settings)
    errors = validate_model(payload, current)
    if errors:
        return {"ok": False, "errors": errors}

    updates: dict[str, Any] = {}

    if "base_url" in payload:
        updates["base_url"] = str(payload["base_url"]).strip()

    # api_key：没这个键 = 不改；空串 = 不改；有值 = 设置；clear_api_key = 清空
    if payload.get("clear_api_key"):
        updates["api_key"] = ""
    elif payload.get("api_key"):
        updates["api_key"] = str(payload["api_key"]).strip()

    for field in ("model", "vision_model", "summary_model"):
        if field in payload:
            updates[field] = str(payload.get(field) or "").strip()

    if "max_tokens" in payload:
        updates["max_tokens"] = _as_int(payload["max_tokens"])
    if "temperature" in payload:
        updates["temperature"] = _as_float(payload["temperature"])
    if "memory_rounds" in payload:
        updates["memory_rounds"] = _as_int(payload["memory_rounds"])
    if "max_reply_chars" in payload:
        updates["max_reply_chars"] = _as_int(payload["max_reply_chars"])
    if "auto_summary" in payload:
        updates["auto_summary"] = bool(payload["auto_summary"])
    if "reply_quote_group" in payload:
        updates["reply_quote_group"] = bool(payload["reply_quote_group"])
    if "message_label" in payload:
        updates["message_label"] = bool(payload["message_label"])
    # 界面用的键名是 error_notice_private，内部存的是 notify_error_private —— 两个都收
    if "error_notice_private" in payload:
        updates["notify_error_private"] = bool(payload["error_notice_private"])
    elif "notify_error_private" in payload:
        updates["notify_error_private"] = bool(payload["notify_error_private"])
    if "rate_limit_per_minute" in payload:
        updates["rate_limit_per_minute"] = _as_int(payload["rate_limit_per_minute"])
    if "blocked_words" in payload:
        # 界面上是一行一个，也容忍逗号 / 顿号分隔
        updates["blocked_words"] = normalize_words(payload["blocked_words"])

    if updates:
        settings.update(**updates)
        log.info("已应用模型配置：%s", "、".join(sorted(updates)))

    return {"ok": True}


def apply_napcat(settings: Settings, payload: dict[str, Any]) -> dict[str, Any]:
    errors = validate_napcat(
        payload, onebot_port=settings.onebot_port, event_port=settings.event_port
    )
    if errors:
        return {"ok": False, "errors": errors}

    updates: dict[str, Any] = {}
    for field in ("onebot_host", "event_host"):
        if field in payload:
            updates[field] = str(payload[field]).strip()
    for field in ("onebot_port", "event_port"):
        if field in payload:
            updates[field] = _as_int(payload[field])

    if payload.get("clear_token"):
        # token 不允许为空（服务端模式下没有 token 等于把 QQ 账号交出去），
        # 所以"清除"的语义是**恢复出厂默认 token**，而不是留空。
        updates["onebot_token"] = DEFAULT_ONEBOT_TOKEN
    elif payload.get("token"):
        updates["onebot_token"] = str(payload["token"]).strip()

    changed = {key: value for key, value in updates.items() if settings.get(key) != value}
    if not changed:
        return {"ok": True, "restart_required": settings.restart_required, "changed": False}

    settings.update(**updates)
    settings.restart_required = True
    log.warning("NapCat 侧配置已改动，需要重启 NapCat 才能生效：%s", "、".join(sorted(changed)))
    return {"ok": True, "restart_required": True, "changed": True}


# --------------------------------------------------------------------------- #
# 连通性测试
# --------------------------------------------------------------------------- #
def test_model(settings: Settings, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """真发一次最小请求（``max_tokens=1``）验证连通性。

    刻意不用 ``/models``：用户要确认的是"**对话接口**能不能用"，
    而且不少 OpenAI 兼容服务对 ``/models`` 支持不一致。
    """
    payload = payload or {}
    # 界面上"清空就按清空测"：显式传了空串就报配置错误，**不要**悄悄退回已保存的值，
    # 否则用户以为在测新填的地址，实际测的是旧的。
    if "base_url" in payload:
        base_url = str(payload.get("base_url") or "").strip()
    else:
        base_url = str(settings.get("base_url") or "").strip()
    if "model" in payload:
        model = str(payload.get("model") or "").strip()
    else:
        model = str(settings.get("model") or "").strip()
    api_key = payload["api_key"] if "api_key" in payload else settings.get("api_key")

    client = OpenAIChatClient(
        base_url,
        str(api_key),
        model,
        timeout=30.0,
        max_tokens=1,
        temperature=None,
    )

    started = time.monotonic()
    try:
        result = client.complete([{"role": "user", "content": "1"}], retries=0)
    except LLMError as exc:
        return {
            "ok": False,
            "kind": exc.kind.value,
            "message": str(exc),
            "hint": exc.hint(),
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    except Exception as exc:  # 兜底，界面永远要拿到结构化结果
        log.exception("模型测试异常")
        return {
            "ok": False,
            "kind": "bad_response",
            "message": str(exc),
            "hint": "调用失败",
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    finally:
        client.close()

    return {
        "ok": True,
        "model": result.model or model,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
    }


__all__ = [
    "MODEL_FIELDS",
    "NAPCAT_FIELDS",
    "apply_model",
    "apply_napcat",
    "mask",
    "snapshot",
    "test_model",
    "validate_model",
    "validate_napcat",
]
