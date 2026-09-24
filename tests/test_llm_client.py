"""OpenAI 兼容客户端测试（对着 tools/fake_openai.py 打）。"""

from __future__ import annotations

import pytest

from conftest import FakeOpenAI
from qlark.llm.client import (
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
