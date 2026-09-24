"""OpenAI 兼容的对话补全客户端。

只依赖 ``requests``，不做流式（QQ 消息本来也攒完再发）。
错误要**分类**，因为用户看到"调用失败"毫无帮助，看到"401 鉴权失败"才知道去改 Key。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence
from urllib.parse import urlsplit, urlunsplit

import requests

log = logging.getLogger(__name__)


class LLMErrorKind(str, Enum):
    """失败类型。前端会据此给不同提示。"""

    CONFIG = "config"           # 配置缺失/非法（还没发出请求）
    AUTH = "auth"               # 401 / 403
    NOT_FOUND = "not_found"     # 404：地址或模型名不对
    RATE_LIMIT = "rate_limit"   # 429
    TIMEOUT = "timeout"         # 超时
    CONNECTION = "connection"   # 连不上
    TLS = "tls"                 # 证书问题
    SERVER = "server"           # 5xx
    BAD_RESPONSE = "bad_response"  # 返回体不符合预期


#: 值得重试的类型
RETRYABLE = frozenset(
    {
        LLMErrorKind.TIMEOUT,
        LLMErrorKind.CONNECTION,
        LLMErrorKind.RATE_LIMIT,
        LLMErrorKind.SERVER,
    }
)

ERROR_HINTS: dict[str, str] = {
    LLMErrorKind.CONFIG: "模型配置不完整",
    LLMErrorKind.AUTH: "鉴权失败，请检查 API Key",
    LLMErrorKind.NOT_FOUND: "地址或模型名不存在（404）",
    LLMErrorKind.RATE_LIMIT: "请求过于频繁，被限流（429）",
    LLMErrorKind.TIMEOUT: "请求超时",
    LLMErrorKind.CONNECTION: "无法连接到服务端",
    LLMErrorKind.TLS: "TLS 证书校验失败",
    LLMErrorKind.SERVER: "服务端错误（5xx）",
    LLMErrorKind.BAD_RESPONSE: "返回内容不符合 OpenAI 格式",
}


class LLMError(RuntimeError):
    """一次模型调用的失败。"""

    def __init__(
        self,
        kind: LLMErrorKind | str,
        message: str,
        *,
        status: int | None = None,
    ) -> None:
        self.kind = LLMErrorKind(kind) if not isinstance(kind, LLMErrorKind) else kind
        self.status = status
        super().__init__(message)

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE

    def hint(self) -> str:
        return ERROR_HINTS.get(self.kind.value, "调用失败")


@dataclass
class ChatResult:
    text: str
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str = ""
    #: 服务端没返回 usage 时，token 数是我们按字符估的
    usage_estimated: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


# --------------------------------------------------------------------------- #
# URL 归一化
# --------------------------------------------------------------------------- #
def normalize_base_url(raw: str) -> str:
    """把用户填的 Base URL 归一化成 ``.../chat/completions`` 端点。

    ==========================================  ============================================
    输入                                         输出
    ==========================================  ============================================
    ``https://api.deepseek.com``                 ``https://api.deepseek.com/v1/chat/completions``
    ``https://api.deepseek.com/v1``              ``https://api.deepseek.com/v1/chat/completions``
    ``https://api.openai.com``                   ``https://api.openai.com/v1/chat/completions``
    ``http://127.0.0.1:11434/v1``                ``http://127.0.0.1:11434/v1/chat/completions``
    ==========================================  ============================================
    """
    value = (raw or "").strip().rstrip("/")
    if not value:
        raise LLMError(LLMErrorKind.CONFIG, "未配置 Base URL")

    if value.endswith("/chat/completions"):
        return value
    if value.endswith("/v1") or value.endswith("/v1beta"):
        return f"{value}/chat/completions"
    return f"{value}/v1/chat/completions"


def models_url(base_url: str) -> str:
    """由 Base URL 推出 ``/models`` 端点（M3 的零 Token 连通性测试用）。"""
    endpoint = normalize_base_url(base_url)
    return endpoint[: -len("/chat/completions")] + "/models"


def _host_of(url: str) -> str:
    try:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    except ValueError:
        return url


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
def estimate_tokens(text: str) -> int:
    """没有 usage 时的粗略估算：中日韩字符按 1 个 token，其余按 4 字符 1 个。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u3000" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef")
    other = len(text) - cjk
    return cjk + max(0, other // 4)


def _flatten_content(content: Any) -> str:
    """``content`` 可能是字符串，也可能是多模态数组，统一取文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif item.get("type") == "image_url":
                    parts.append("[图片]")
        return "".join(parts)
    return "" if content is None else str(content)


class OpenAIChatClient:
    """极简的 OpenAI 兼容客户端。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 60.0,
        max_tokens: int | None = None,
        temperature: float | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._session = session or requests.Session()

    # ------------------------------------------------------------------ 校验
    def validate(self) -> None:
        """调用前先自查配置，避免发出必然失败的请求。"""
        if not (self.base_url or "").strip():
            raise LLMError(LLMErrorKind.CONFIG, "未配置 Base URL")
        if not (self.api_key or "").strip():
            raise LLMError(LLMErrorKind.CONFIG, "未配置 API Key")
        if not (self.model or "").strip():
            raise LLMError(LLMErrorKind.CONFIG, "未配置模型名")

    # ------------------------------------------------------------------ 调用
    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        retries: int = 2,
        backoff: float = 1.5,
    ) -> ChatResult:
        """发一次对话补全请求，可重试的类型自动退避重试。"""
        self.validate()
        endpoint = normalize_base_url(self.base_url)

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "stream": False,
        }
        if self.max_tokens:
            payload["max_tokens"] = int(self.max_tokens)
        if self.temperature is not None:
            payload["temperature"] = float(self.temperature)

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        attempts = max(1, int(retries) + 1)
        last_error: LLMError | None = None
        for index in range(attempts):
            try:
                response = self._session.post(
                    endpoint, json=payload, headers=headers, timeout=self.timeout
                )
            except requests.Timeout as exc:
                last_error = LLMError(LLMErrorKind.TIMEOUT, f"请求超时（{self.timeout:.0f}s）")
                _ = exc
            except requests.exceptions.SSLError as exc:
                last_error = LLMError(LLMErrorKind.TLS, f"TLS 证书校验失败：{exc}")
            except requests.ConnectionError as exc:
                last_error = LLMError(
                    LLMErrorKind.CONNECTION, f"无法连接到 {_host_of(endpoint)}：{exc}"
                )
            except requests.RequestException as exc:
                last_error = LLMError(LLMErrorKind.CONNECTION, f"请求失败：{exc}")
            else:
                try:
                    return self._parse(response, messages)
                except LLMError as exc:
                    last_error = exc

            if last_error is None or not last_error.retryable or index + 1 >= attempts:
                break
            wait = backoff * (2**index)
            log.warning(
                "模型调用失败（%s），%.1fs 后重试（%d/%d）",
                last_error.kind.value,
                wait,
                index + 1,
                attempts - 1,
            )
            time.sleep(wait)

        assert last_error is not None
        raise last_error

    # ------------------------------------------------------------------ 解析
    def _parse(
        self, response: requests.Response, messages: Sequence[dict[str, Any]]
    ) -> ChatResult:
        status = response.status_code
        if status in (401, 403):
            raise LLMError(LLMErrorKind.AUTH, f"鉴权失败（HTTP {status}）", status=status)
        if status == 404:
            raise LLMError(
                LLMErrorKind.NOT_FOUND, f"接口或模型不存在（HTTP 404）：{response.url}", status=status
            )
        if status == 429:
            raise LLMError(LLMErrorKind.RATE_LIMIT, "被限流（HTTP 429）", status=status)
        if status >= 500:
            raise LLMError(LLMErrorKind.SERVER, f"服务端错误（HTTP {status}）", status=status)
        if status != 200:
            raise LLMError(
                LLMErrorKind.BAD_RESPONSE,
                f"意外状态码 HTTP {status}：{response.text[:200]}",
                status=status,
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise LLMError(
                LLMErrorKind.BAD_RESPONSE, f"返回内容不是 JSON：{response.text[:200]}"
            ) from exc
        if not isinstance(data, dict):
            raise LLMError(LLMErrorKind.BAD_RESPONSE, "返回结构不是对象")

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            message = ""
            error = data.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or "")
            raise LLMError(
                LLMErrorKind.BAD_RESPONSE,
                f"响应里没有 choices{'：' + message if message else ''}",
            )

        first = choices[0] if isinstance(choices[0], dict) else {}
        message_obj = first.get("message") if isinstance(first.get("message"), dict) else {}
        text = _flatten_content(message_obj.get("content"))

        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        estimated = False
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        if not isinstance(prompt_tokens, int) or not isinstance(completion_tokens, int):
            # 服务端没给 usage（部分 OpenAI 兼容实现会省掉），按字符粗略估算
            estimated = True
            prompt_tokens = sum(
                estimate_tokens(_flatten_content(m.get("content"))) for m in messages
            )
            completion_tokens = estimate_tokens(text)

        return ChatResult(
            text=text,
            model=str(data.get("model") or self.model),
            prompt_tokens=int(prompt_tokens or 0),
            completion_tokens=int(completion_tokens or 0),
            finish_reason=str(first.get("finish_reason") or ""),
            usage_estimated=estimated,
            raw=data,
        )

    # ------------------------------------------------------------------ 资源
    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # pragma: no cover
            pass


__all__ = [
    "ChatResult",
    "ERROR_HINTS",
    "LLMError",
    "LLMErrorKind",
    "OpenAIChatClient",
    "RETRYABLE",
    "estimate_tokens",
    "models_url",
    "normalize_base_url",
]
