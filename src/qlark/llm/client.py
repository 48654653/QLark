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
#:
#: ``TLS`` 也在里面：``SSLV3_ALERT_BAD_RECORD_MAC`` / ``UNEXPECTED_EOF`` 这类
#: 是**连接级**故障（对端的 TLS 会话状态丢了、中间设备改坏了记录、空闲连接被
#: 服务端回收后又被复用），换一条连接重发基本就好。
#:
#: **唯一例外是证书问题**（``CERTIFICATE_VERIFY_FAILED``、系统时间不对、
#: 证书过期）：那是确定性的，重试多少次都一样 —— 见 ``_is_certificate_error``，
#: 那种错误会被显式标成不可重试。
RETRYABLE = frozenset(
    {
        LLMErrorKind.TIMEOUT,
        LLMErrorKind.CONNECTION,
        LLMErrorKind.RATE_LIMIT,
        LLMErrorKind.SERVER,
        LLMErrorKind.TLS,
    }
)

ERROR_HINTS: dict[str, str] = {
    LLMErrorKind.CONFIG: "模型配置不完整",
    LLMErrorKind.AUTH: "鉴权失败，请检查 API Key",
    LLMErrorKind.NOT_FOUND: "地址或模型名不存在（404）",
    LLMErrorKind.RATE_LIMIT: "请求过于频繁，被限流（429）",
    LLMErrorKind.TIMEOUT: "请求超时",
    LLMErrorKind.CONNECTION: "无法连接到服务端",
    LLMErrorKind.TLS: "TLS 连接异常（证书或加密中断）",
    LLMErrorKind.SERVER: "服务端错误（5xx）",
    LLMErrorKind.BAD_RESPONSE: "返回内容不符合 OpenAI 格式",
}

#: 出现这些字样说明是**证书/握手**问题（确定性失败，重试没意义）
_CERT_MARKERS = (
    "certificate verify failed",
    "certificate_verify_failed",
    "hostname mismatch",
    "self-signed certificate",
    "self signed certificate",
    "unable to get local issuer certificate",
    "certificate has expired",
    "certificate is not yet valid",
    "bad_certificate",
    "unknown ca",
)


def _is_certificate_error(exc: BaseException) -> bool:
    """判断一个 SSL 异常是不是**证书**问题（而不是连接被中断）。

    为什么要分开：``requests.exceptions.SSLError`` 是一个很粗的桶，既装
    "证书验不过"（改了也没用，重试纯属浪费），也装 "bad record mac"
    （换条连接重发就好）。把它们当成一类处理，要么该重试的不重试、
    要么把确定性错误重试三遍还多等 4.5 秒。
    """
    import ssl

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        text = str(current).lower()
        if any(marker in text for marker in _CERT_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False


class LLMError(RuntimeError):
    """一次模型调用的失败。"""

    def __init__(
        self,
        kind: LLMErrorKind | str,
        message: str,
        *,
        status: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        self.kind = LLMErrorKind(kind) if not isinstance(kind, LLMErrorKind) else kind
        self.status = status
        #: 覆盖 `RETRYABLE` 的判定；``None`` 表示按类型走默认规则
        self._retryable = retryable
        super().__init__(message)

    @property
    def retryable(self) -> bool:
        if self._retryable is not None:
            return self._retryable
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


#: 「思考强度」选项 → 请求体里的 ``thinking`` 片段。
#:
#: 这几个值是**实测**出来的（不是照抄文档）：直接发非法值给 DeepSeek，错误信息里
#: 会列出它接受的全部取值 —— ``thinking.type`` 认 ``adaptive``/``enabled``/``disabled``。
#:
#: 空串 ``""`` = **不发送这个字段**，跟随服务端默认。
#: 注意 DeepSeek 的默认是 ``enabled``（即默认就在思考），所以想省钱必须显式关掉。
THINKING_PAYLOADS: dict[str, dict[str, Any]] = {
    "off": {"type": "disabled"},
    "auto": {"type": "adaptive"},
    "low": {"type": "enabled", "reasoning_effort": "low"},
    "medium": {"type": "enabled", "reasoning_effort": "medium"},
    "high": {"type": "enabled", "reasoning_effort": "high"},
    "max": {"type": "enabled", "reasoning_effort": "max"},
}

#: 出现这些字样说明"服务端不认识这个参数"，据此自动降级
_UNSUPPORTED_HINTS = (
    "unsupported",
    "unrecognized",
    "unknown",
    "not supported",
    "unexpected",
    "extra inputs",
    "invalid_request_error",
)


def thinking_payload(mode: str | None) -> dict[str, Any] | None:
    """把设置里的思考强度转成请求体片段；未知值/空值返回 ``None``（不发送）。"""
    return THINKING_PAYLOADS.get(str(mode or "").strip().lower())


def looks_like_json_mode_unsupported(status: int | None, body: str) -> bool:
    """判断这次失败是不是"服务端不认 response_format"。"""
    if status not in (400, 422):
        return False
    low = str(body or "").lower()
    if "response_format" not in low and "json_object" not in low and "json mode" not in low:
        return False
    return any(hint in low for hint in _UNSUPPORTED_HINTS)


def looks_like_thinking_unsupported(status: int | None, body: str) -> bool:
    """判断这次失败是不是"服务端不认 thinking 参数"。

    ``thinking`` 不是 OpenAI 标准字段。DeepSeek 会**静默忽略**不认识的字段，
    但别的兼容服务可能直接报错 —— 那就得去掉它重试，不能让整次调用失败。
    """
    if status not in (400, 422):
        return False
    low = str(body or "").lower()
    if "thinking" not in low and "reasoning_effort" not in low:
        return False
    return any(hint in low for hint in _UNSUPPORTED_HINTS)


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
        thinking: str | None = None,
        json_mode: bool = False,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature
        #: 思考强度（``off``/``auto``/``low``/``medium``/``high``/``max``）。
        #: 服务端明确表示不支持时会被就地清成 ``None``，之后不再发送。
        self.thinking = thinking
        #: 要不要发 ``response_format: {"type": "json_object"}``。
        #:
        #: v2 起每次都要 JSON，这是双保险里的第一道（第二道是末尾的提醒）。
        #: **但它是 OpenAI 专有字段的扩展语义**，各家兼容服务的表现不一样：
        #:   · MiMo：支持，实测一次就对
        #:   · DeepSeek：认这个字段，但**带历史时会返回空内容**（实测 0/8）
        #: 所以下面加了两条自动降级：报错就关掉重试；返回空也关掉重试并记住。
        self.json_mode = json_mode
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
        timeout: float | None = None,
    ) -> ChatResult:
        """发一次对话补全请求，可重试的类型自动退避重试。

        Args:
            timeout: 本次请求的超时（秒）。``None`` 用客户端的默认值。
                带视频的请求要显式放宽 —— 服务端得**先把整段视频拉下来解码**，
                实测一段 1MB 的短视频就要 60s 上下，用默认的 60s 会直接超时。
        """
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
        extra = thinking_payload(self.thinking)
        if extra is not None:
            payload["thinking"] = extra
        if self.json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        try:
            result = self._request_loop(
                endpoint, payload, messages, headers, retries, backoff, timeout
            )
        except LLMError as exc:
            # 服务端不认 thinking 参数 → 去掉它再来一轮，并**记住**别再发。
            # `thinking` 不是 OpenAI 标准字段：DeepSeek 会静默忽略未知字段，
            # 但别的兼容服务可能直接 400/422，不能让整次调用就这么失败。
            if "thinking" in payload and looks_like_thinking_unsupported(exc.status, str(exc)):
                log.warning("服务端不支持 thinking 参数，已自动去掉并重试一次：%s", exc)
                payload.pop("thinking", None)
                self.thinking = None
                result = self._request_loop(
                    endpoint, payload, messages, headers, retries, backoff, timeout
                )
            elif "response_format" in payload and looks_like_json_mode_unsupported(
                exc.status, str(exc)
            ):
                log.warning("服务端不支持 response_format，已自动去掉并重试一次：%s", exc)
                payload.pop("response_format", None)
                self.json_mode = False
                result = self._request_loop(
                    endpoint, payload, messages, headers, retries, backoff, timeout
                )
            else:
                raise

        # 有些服务**认这个字段但会返回空**（DeepSeek 带历史时实测 0/8）。
        # 这种情况不报错，只能靠"空内容"识别：关掉它再问一次，然后记住别再用。
        if self.json_mode and not str(result.text or "").strip():
            log.warning("response_format 生效后返回空内容，关掉它重试一次")
            payload.pop("response_format", None)
            self.json_mode = False
            result = self._request_loop(
                endpoint, payload, messages, headers, retries, backoff, timeout
            )
        return result

    def _request_loop(
        self,
        endpoint: str,
        payload: dict[str, Any],
        messages: Sequence[dict[str, Any]],
        headers: dict[str, str],
        retries: int,
        backoff: float,
        timeout: float | None = None,
    ) -> ChatResult:
        """带退避重试的请求循环。**会就地修改传入的 payload**（例如去掉参数）。"""
        attempts = max(1, int(retries) + 1)
        wait_for = float(timeout if timeout is not None and timeout > 0 else self.timeout)
        last_error: LLMError | None = None
        for index in range(attempts):
            try:
                response = self._session.post(
                    endpoint, json=payload, headers=headers, timeout=wait_for
                )
            except requests.Timeout as exc:
                last_error = LLMError(LLMErrorKind.TIMEOUT, f"请求超时（{wait_for:.0f}s）")
                _ = exc
            except requests.exceptions.SSLError as exc:
                if _is_certificate_error(exc):
                    # 证书 / 主机名 / 时间不对：确定性失败，重试没意义
                    last_error = LLMError(
                        LLMErrorKind.TLS,
                        f"TLS 证书校验失败（请检查系统时间、代理或杀软的 HTTPS 扫描）：{exc}",
                        retryable=False,
                    )
                else:
                    # 连接级故障（bad record mac / 对端关闭 / 会话状态丢失）：
                    # 换一条连接重发基本就好，所以是可重试的
                    last_error = LLMError(
                        LLMErrorKind.TLS,
                        f"TLS 连接被中断（对端拒绝或链路异常）：{exc}",
                        retryable=True,
                    )
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

            if last_error.kind is LLMErrorKind.TLS:
                # 关键：**坏掉的那条 TLS 连接不能留在池子里**给下一次复用，
                # 否则重试还是打在同一个坏会话上（实测会连续失败三次）。
                self._drop_connections()

            wait = backoff * (2**index)
            log.warning(
                "模型调用失败（%s），%.1fs 后重试（%d/%d）：%s",
                last_error.kind.value,
                wait,
                index + 1,
                attempts - 1,
                last_error,
            )
            time.sleep(wait)

        assert last_error is not None
        raise last_error

    def _drop_connections(self) -> None:
        """清空连接池（已有连接全部丢弃，下一次请求走全新握手）。

        ``Session.close()`` 只关适配器里的连接池，Session 本身还能继续用 ——
        所以这里不需要重建客户端。
        """
        try:
            self._session.close()
        except Exception:  # pragma: no cover - 关连接池失败不该影响重试
            pass

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
            # 多截一点：兼容降级要靠这段文本判断"是不是参数不支持"，
            # 200 字符有时会在关键信息之前就断掉
            raise LLMError(
                LLMErrorKind.BAD_RESPONSE,
                f"意外状态码 HTTP {status}：{response.text[:400]}",
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
