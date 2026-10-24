"""OpenAI 兼容客户端测试（对着 tools/fake_openai.py 打）。"""

from __future__ import annotations

import json

import pytest
import requests

from conftest import FakeOpenAI
from qlark.llm.client import (
    RETRYABLE,
    LLMError,
    LLMErrorKind,
    OpenAIChatClient,
    estimate_tokens,
    models_url,
    normalize_base_url,
)


# --------------------------------------------------------------------------- #
# URL 归一化
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://api.deepseek.com", "https://api.deepseek.com/v1/chat/completions"),
        ("https://api.deepseek.com/", "https://api.deepseek.com/v1/chat/completions"),
        ("https://api.deepseek.com/v1", "https://api.deepseek.com/v1/chat/completions"),
        ("https://api.openai.com", "https://api.openai.com/v1/chat/completions"),
        ("http://127.0.0.1:11434/v1", "http://127.0.0.1:11434/v1/chat/completions"),
        (
            "https://x.com/v1/chat/completions",
            "https://x.com/v1/chat/completions",
        ),
        ("  https://x.com/v1  ", "https://x.com/v1/chat/completions"),
    ],
)
def test_normalize_base_url(raw: str, expected: str) -> None:
    assert normalize_base_url(raw) == expected


def test_normalize_base_url_rejects_empty() -> None:
    with pytest.raises(LLMError) as info:
        normalize_base_url("")
    assert info.value.kind is LLMErrorKind.CONFIG


def test_models_url() -> None:
    assert models_url("https://api.deepseek.com") == "https://api.deepseek.com/v1/models"
    assert models_url("https://api.deepseek.com/v1") == "https://api.deepseek.com/v1/models"


def test_estimate_tokens() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("你好世界") == 4
    assert estimate_tokens("abcdefgh") == 2


# --------------------------------------------------------------------------- #
# 配置校验
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_url": "", "api_key": "k", "model": "m"},
        {"base_url": "https://x.com", "api_key": "", "model": "m"},
        {"base_url": "https://x.com", "api_key": "k", "model": ""},
    ],
)
def test_validate_rejects_incomplete_config(kwargs: dict) -> None:
    with pytest.raises(LLMError) as info:
        OpenAIChatClient(**kwargs).complete([{"role": "user", "content": "hi"}])
    assert info.value.kind is LLMErrorKind.CONFIG
    assert info.value.retryable is False


# --------------------------------------------------------------------------- #
# 真实请求（假服务）
# --------------------------------------------------------------------------- #
def test_complete_returns_text_and_usage(fake_openai: FakeOpenAI) -> None:
    client = OpenAIChatClient(fake_openai.base_url, "test-key", "fake-model")
    result = client.complete([{"role": "user", "content": "你好"}])
    assert result.text == "收到：你好"
    assert result.model == "fake-model"
    assert result.usage_estimated is False
    assert result.prompt_tokens == 2
    assert result.completion_tokens == 5
    assert result.total_tokens == 7


def test_complete_sends_authorization_header(fake_openai: FakeOpenAI) -> None:
    client = OpenAIChatClient(fake_openai.base_url, "secret-key", "fake-model")
    client.complete([{"role": "user", "content": "hi"}])
    import json
    import urllib.request

    with urllib.request.urlopen(f"{fake_openai.base_url}/_qlark/requests", timeout=5) as response:
        data = json.loads(response.read().decode("utf-8"))
    assert data["meta"][0]["authorization"] == "Bearer secret-key"


def test_complete_retries_then_surfaces_server_error(fake_openai: FakeOpenAI) -> None:
    """5xx 属于可重试：重试耗尽后仍失败就要抛出，并保留类型。"""
    fake_openai.fail_next(times=5, status=500, message="boom")
    client = OpenAIChatClient(fake_openai.base_url, "k", "m")
    with pytest.raises(LLMError) as info:
        client.complete([{"role": "user", "content": "hi"}], retries=1, backoff=0.05)
    assert info.value.kind is LLMErrorKind.SERVER
    assert info.value.retryable is True
    assert "500" in str(info.value)


def test_complete_recovers_when_retry_succeeds(fake_openai: FakeOpenAI) -> None:
    fake_openai.fail_next(times=1, status=500)
    client = OpenAIChatClient(fake_openai.base_url, "k", "m")
    result = client.complete([{"role": "user", "content": "hi"}], retries=2, backoff=0.05)
    assert result.text == "收到：hi"


def test_rate_limit_is_classified(fake_openai: FakeOpenAI) -> None:
    fake_openai.fail_next(times=5, status=429, message="slow down")
    client = OpenAIChatClient(fake_openai.base_url, "k", "m")
    with pytest.raises(LLMError) as info:
        client.complete([{"role": "user", "content": "hi"}], retries=0)
    assert info.value.kind is LLMErrorKind.RATE_LIMIT


def test_not_found_is_not_retryable(fake_openai: FakeOpenAI) -> None:
    client = OpenAIChatClient(f"{fake_openai.base_url}/nope", "k", "m")
    with pytest.raises(LLMError) as info:
        client.complete([{"role": "user", "content": "hi"}], retries=2, backoff=0.05)
    assert info.value.kind is LLMErrorKind.NOT_FOUND
    assert info.value.retryable is False


def test_connection_error_on_closed_port() -> None:
    from conftest import free_port

    port = free_port()  # 拿到就关掉，保证没人监听
    client = OpenAIChatClient(f"http://127.0.0.1:{port}", "k", "m", timeout=2)
    with pytest.raises(LLMError) as info:
        client.complete([{"role": "user", "content": "hi"}], retries=0)
    assert info.value.kind in (LLMErrorKind.CONNECTION, LLMErrorKind.TIMEOUT)


def test_multimodal_content_is_passed_through(fake_openai: FakeOpenAI) -> None:
    client = OpenAIChatClient(fake_openai.base_url, "k", "m")
    result = client.complete(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "这是什么"},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
                ],
            }
        ]
    )
    assert result.text == "收到：这是什么[图片]"


def test_error_hint_is_human_readable(fake_openai: FakeOpenAI) -> None:
    client = OpenAIChatClient(f"{fake_openai.base_url}/nope", "k", "m")
    with pytest.raises(LLMError) as info:
        client.complete([{"role": "user", "content": "hi"}], retries=0)
    assert "404" in info.value.hint() or "模型" in info.value.hint()


# --------------------------------------------------------------------------- #
# TLS：连接级故障要重试，证书问题不要
# --------------------------------------------------------------------------- #
BAD_RECORD_MAC = (
    "HTTPSConnectionPool(host='api.xiaomimimo.com', port=443): Max retries exceeded "
    "with url: /v1/chat/completions (Caused by SSLError(SSLError(1, "
    "'[SSL: SSLV3_ALERT_BAD_RECORD_MAC] sslv3 alert bad record mac (_ssl.c:2658)')))"
)
CERT_FAILED = (
    "HTTPSConnectionPool(host='api.xiaomimimo.com', port=443): Max retries exceeded "
    "with url: /v1/chat/completions (Caused by SSLError(SSLCertVerificationError(1, "
    "'[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
    "unable to get local issuer certificate (_ssl.c:1010)')))"
)


class _StubResponse:
    """够 ``_parse`` 用就行。"""

    def __init__(self, payload: dict, *, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status
        self.url = "https://stub/chat/completions"
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


class _FlakySession:
    """按顺序抛出预设的异常，抛完就返回一条正常回复。"""

    def __init__(self, errors: list[Exception]) -> None:
        self._errors = list(errors)
        self.calls = 0
        self.closed = 0

    def post(self, url: str, **kwargs) -> _StubResponse:
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return _StubResponse(
            {
                "model": "m",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": '{"messages":["好了"]}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            }
        )

    def close(self) -> None:
        self.closed += 1


def test_tls_interruption_is_retryable() -> None:
    assert LLMErrorKind.TLS in RETRYABLE


def test_bad_record_mac_is_retried_and_recovers() -> None:
    """``bad record mac`` 是连接级故障：丢掉连接池、换一条连接重发就好。

    这条是拿真实故障换来的：2026-10-24 04:45~04:46 连续三次
    ``SSLV3_ALERT_BAD_RECORD_MAC``，每条都把用户的回复弄丢了。
    """
    session = _FlakySession([requests.exceptions.SSLError(BAD_RECORD_MAC)])
    client = OpenAIChatClient("https://api.example.com", "k", "m", session=session)

    result = client.complete([{"role": "user", "content": "hi"}], retries=2, backoff=0.01)

    assert result.text == '{"messages":["好了"]}'
    assert session.calls == 2, "应当重试一次"
    assert session.closed >= 1, "重试前必须先丢掉坏掉的连接池"


def test_bad_record_mac_gives_up_after_retries() -> None:
    session = _FlakySession([requests.exceptions.SSLError(BAD_RECORD_MAC)] * 3)
    client = OpenAIChatClient("https://api.example.com", "k", "m", session=session)

    with pytest.raises(LLMError) as info:
        client.complete([{"role": "user", "content": "hi"}], retries=2, backoff=0.01)

    assert info.value.kind is LLMErrorKind.TLS
    assert "中断" in str(info.value)
    assert session.calls == 3


def test_certificate_failure_is_not_retried() -> None:
    """证书问题重试多少次都一样，不该白等 4.5 秒的退避。"""
    session = _FlakySession([requests.exceptions.SSLError(CERT_FAILED)] * 3)
    client = OpenAIChatClient("https://api.example.com", "k", "m", session=session)

    with pytest.raises(LLMError) as info:
        client.complete([{"role": "user", "content": "hi"}], retries=2, backoff=0.01)

    assert info.value.kind is LLMErrorKind.TLS
    assert info.value.retryable is False
    assert session.calls == 1, "证书错误不该重试"
    assert "证书" in str(info.value)


def test_certificate_error_detection() -> None:
    import ssl

    from qlark.llm.client import _is_certificate_error

    assert _is_certificate_error(ssl.SSLCertVerificationError(1, CERT_FAILED))
    assert not _is_certificate_error(requests.exceptions.SSLError(BAD_RECORD_MAC))
    # 包一层也要认得出（requests 会把底层异常塞进 args）
    wrapped = requests.exceptions.SSLError(CERT_FAILED)
    wrapped.__cause__ = ssl.SSLCertVerificationError(1, "certificate verify failed")
    assert _is_certificate_error(wrapped)
