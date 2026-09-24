"""OpenAI 兼容的模型调用层。"""

from .client import (
    ChatResult,
    ERROR_HINTS,
    LLMError,
    LLMErrorKind,
    OpenAIChatClient,
    estimate_tokens,
    models_url,
    normalize_base_url,
)

__all__ = [
    "ChatResult",
    "ERROR_HINTS",
    "LLMError",
    "LLMErrorKind",
    "OpenAIChatClient",
    "estimate_tokens",
    "models_url",
    "normalize_base_url",
]
