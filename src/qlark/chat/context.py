"""上下文组装：把人设、历史与当前消息拼成 OpenAI 兼容的 ``messages``。

顺序（需求规格 §6.3）：

.. code-block:: text

    [system]  人设提示词          ← 为空则整条不发
    ...最近 W 轮原文...
    [user]    本轮消息

> M4 会在 ``[system]`` 之后插入独立的「长期记忆摘要」块（不占轮次）。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

#: OpenAI 兼容接口只认这三个角色
VALID_ROLES = frozenset({"system", "user", "assistant"})


def build_chat_messages(
    *,
    system_prompt: str = "",
    history: Iterable[tuple[str, str]] = (),
    user_content: str | list[dict[str, Any]],
    extra_system: Sequence[str] = (),
    trailing_system: str = "",
) -> list[dict[str, Any]]:
    """拼装请求体里的 ``messages`` 数组。

    Args:
        system_prompt: 人设提示词。**空串会整条跳过** —— 不能发一条空的 system，
            部分 OpenAI 兼容服务会因此报错。
        history: ``(role, content)`` 序列，按时间正序。
        user_content: 本轮用户消息。字符串，或已是多模态数组（M5 图片理解用）。
        extra_system: 额外插入的 system 块（M4 的长期记忆摘要走这里）。
        trailing_system: **插在历史之后、用户消息之前**的一条 system。

            必须放在这个位置、而不能并进开头那坨 system —— 实测：
            格式要求离生成点隔了二十来条消息时，会被历史里"助手自己一直用
            散文说话"的示范盖过，模型照旧输出散文。搬到最后一条之后，
            成功率从 0% 升到 83%。
    """
    messages: list[dict[str, Any]] = []

    blocks = [block.strip() for block in (system_prompt, *extra_system) if block and block.strip()]
    if blocks:
        # 合并成**一条** system：部分服务只接受第一条 system 消息
        messages.append({"role": "system", "content": "\n\n".join(blocks)})

    for role, content in history:
        if role not in VALID_ROLES or role == "system":
            continue
        text = str(content or "").strip()
        if not text:
            continue
        messages.append({"role": role, "content": text})

    if trailing_system and trailing_system.strip():
        messages.append({"role": "system", "content": trailing_system.strip()})

    messages.append({"role": "user", "content": user_content})
    return messages


def estimate_chars(messages: Sequence[dict[str, Any]]) -> int:
    """粗略估算字符数（用于日志与预算保护，不做精确 token 计算）。"""
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total += len(str(part.get("text") or ""))
    return total


__all__ = ["VALID_ROLES", "build_chat_messages", "estimate_chars"]
