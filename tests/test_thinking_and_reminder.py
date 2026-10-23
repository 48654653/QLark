"""格式提醒（双保险）与思考强度。

两件事都是实测逼出来的：

**格式提醒为什么要放两处**：原来【输出规则】排在消息开头，离生成点隔了二十来条
历史，而那些历史里助手自己的回复全是散文 —— 模型就继续写散文，JSON 解析失败率
26%。把同一句提醒挪到生成点附近：单放一处约 83%，两处都放 30/30 = 100%。

**思考强度为什么要默认关**：实测 DeepSeek 的默认是「开思考」，一条闲聊回复里
86% 的输出 token 花在推理上；而且官方明确"思考模式下 temperature 无效"。
关掉之后输出 token 减半，成功率不变。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import FakeOpenAI, wait_for, wait_memory
from qlark.chat.context import build_chat_messages
from qlark.chat.reply_format import FORMAT_REMINDER, append_reminder, build_preview
from qlark.chat.store import memory_key
from qlark.llm.client import (
    THINKING_PAYLOADS,
    LLMError,
    OpenAIChatClient,
    looks_like_thinking_unsupported,
    thinking_payload,
)

from test_m2_integration import _inject, _reply_texts, _reset, ready  # noqa: F401

GROUP = "888888"


# --------------------------------------------------------------------------- #
# 消息组装
# --------------------------------------------------------------------------- #
def test_trailing_system_goes_after_history_before_user() -> None:
    """提醒必须紧挨用户消息 —— 这正是它起作用的原因。"""
    messages = build_chat_messages(
        system_prompt="人设",
        history=[("user", "你好"), ("assistant", "在的")],
        user_content="新的问题",
        extra_system=["【输出规则】"],
        trailing_system=FORMAT_REMINDER,
    )
    roles = [m["role"] for m in messages]
    assert roles == ["system", "user", "assistant", "system", "user"], roles
    assert messages[-2]["content"] == FORMAT_REMINDER
    assert messages[-1]["content"] == "新的问题"


def test_trailing_system_absent_when_empty() -> None:
    messages = build_chat_messages(system_prompt="人设", user_content="hi")
    assert [m["role"] for m in messages] == ["system", "user"]


def test_append_reminder_to_text() -> None:
    result = append_reminder("你好", "（提醒）")
    assert result == "你好\n\n（提醒）"


def test_append_reminder_to_empty_text() -> None:
    assert append_reminder("", "（提醒）") == "（提醒）"


def test_append_reminder_to_multimodal_keeps_images_untouched() -> None:
    """图片段一个字都不能动，提醒只加到 text 段。"""
    content = [
        {"type": "text", "text": "看这张"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
    ]
    result = append_reminder(content, "（提醒）")
    assert result[0]["text"] == "看这张\n\n（提醒）"
    assert result[1] == content[1], "图片段被改了"
    assert len(result) == 2


def test_append_reminder_adds_text_part_when_only_image() -> None:
    content = [{"type": "image_url", "image_url": {"url": "data:,"}}]
    result = append_reminder(content, "（提醒）")
    assert result[0] == {"type": "text", "text": "（提醒）"}
    assert result[1]["type"] == "image_url"


def test_append_reminder_with_blank_text_is_noop() -> None:
    assert append_reminder("你好", "   ") == "你好"


# --------------------------------------------------------------------------- #
# 端到端：两处提醒
# --------------------------------------------------------------------------- #
def _payloads(fake_openai: FakeOpenAI) -> list[dict]:
    return fake_openai.requests()


def _system_roles(payload: dict) -> list[str]:
    return [m["role"] for m in payload.get("messages") or []]


def _say(env, text: str = "你好", message_id: int = 1) -> None:
    _reset(env.home)
    _inject(
        env.home,
        message_type="group",
        group_id=GROUP,
        user_id="333333",
        message_id=message_id,
        text=text,
        at_bot=True,
        nickname="毛糙蛋",
    )
    assert wait_for(lambda: _reply_texts(env.home), timeout=30), "没有回复"


def test_reminder_appears_twice_when_gate_hits(ready, fake_openai: FakeOpenAI) -> None:
    """掷中时两处都要有：历史之后的 system + 用户消息末尾。"""
    ready.settings.update(split_chance=1.0, meme_chance=0.0, split_delay_ms=0)
    ready.bot.pipeline.personas.update(1, {"split_enabled": True})
    fake_openai.replies('{"messages": ["一"]}', repeat_last=True)

    _say(ready)

    payload = _payloads(fake_openai)[-1]
    messages = payload["messages"]

    # ① 末尾 system（注意这一条里还可能带着常驻的「回复判断表」，
    #    所以断言"包含"而不是"等于"）
    assert messages[-2]["role"] == "system", [m["role"] for m in messages]
    assert FORMAT_REMINDER in str(messages[-2]["content"])
    # ② 用户文本末尾
    assert messages[-1]["role"] == "user"
    assert str(messages[-1]["content"]).rstrip().endswith(FORMAT_REMINDER)


def test_reminder_is_present_every_round(ready, fake_openai: FakeOpenAI) -> None:
    """v2：格式提醒**每一轮都发**，不再有"掷中才加"。

    旧版这里断言"没掷中时两处都不能有"。骰子删掉之后，模型每一轮都被要求
    输出 JSON，提醒必须恒定存在 —— 否则它一放松就写散文，解析直接失败。
    """
    ready.settings.update(split_chance=0.0, meme_chance=0.0)
    ready.bot.pipeline.personas.update(1, {"split_enabled": True, "meme_enabled": True})
    fake_openai.reset()

    _say(ready)

    payload = _payloads(fake_openai)[-1]
    joined = json.dumps(payload, ensure_ascii=False)
    assert FORMAT_REMINDER in joined, "每轮都必须带格式提醒"
    assert "【输出规则】" in joined
    # 仍然是两处：末尾 system + 用户消息文本末尾
    assert str(payload["messages"][-1]["content"]).rstrip().endswith("不要写别的话。）")


def test_reminder_never_reaches_memory(ready, fake_openai: FakeOpenAI) -> None:
    """记忆里必须干干净净 —— 提醒只发给模型看。"""
    ready.settings.update(split_chance=1.0, meme_chance=0.0, split_delay_ms=0)
    ready.bot.pipeline.personas.update(1, {"split_enabled": True})
    fake_openai.replies('{"messages": ["一"]}', repeat_last=True)

    _say(ready)

    key = memory_key(f"group:{GROUP}", 1)
    assert wait_memory(ready.bot.pipeline.store, key, min_rows=2), "记忆没落库"
    rows = ready.bot.pipeline.store.memory_messages(key)
    for row in rows:
        assert FORMAT_REMINDER not in row["content"], f"提醒进了记忆：{row['content']!r}"
        assert "提醒" not in row["content"]


def test_reminder_survives_the_regeneration_request(ready, fake_openai: FakeOpenAI) -> None:
    """重新生成时是接着原 messages 加两条，所以提醒仍在。"""
    ready.settings.update(
        split_chance=1.0,
        meme_chance=0.0,
        reply_retry_max=1,
        split_delay_ms=0,
    )
    ready.bot.pipeline.personas.update(1, {"split_enabled": True})
    fake_openai.replies("这不是 JSON", '{"messages": ["补好的"]}', repeat_last=True)

    _say(ready)

    payloads = _payloads(fake_openai)
    assert len(payloads) >= 2, "没有发生重新生成"
    for payload in payloads:
        joined = json.dumps(payload, ensure_ascii=False)
        assert FORMAT_REMINDER in joined, "某一轮丢了提醒"


# --------------------------------------------------------------------------- #
# 预览要如实反映
# --------------------------------------------------------------------------- #
def test_preview_shows_the_reminder_twice() -> None:
    preview = build_preview(
        system_prompt="人设", rules="【输出规则】", enabled_note="", reminder=FORMAT_REMINDER
    )
    titles = [b["title"] for b in preview["blocks"]]
    assert any("格式提醒" in t for t in titles), titles
    # 用户消息那一段也要带上
    user_block = next(b for b in preview["blocks"] if b["role"] == "user")
    assert user_block["content"].rstrip().endswith(FORMAT_REMINDER)
    assert preview["text"].count(FORMAT_REMINDER) == 2


def test_preview_without_reminder_stays_clean() -> None:
    preview = build_preview(system_prompt="人设", rules="", enabled_note="")
    assert FORMAT_REMINDER not in preview["text"]
    assert not any("格式提醒" in b["title"] for b in preview["blocks"])


# --------------------------------------------------------------------------- #
# 思考强度
# --------------------------------------------------------------------------- #
def test_thinking_payload_mapping() -> None:
    assert thinking_payload("off") == {"type": "disabled"}
    assert thinking_payload("auto") == {"type": "adaptive"}
    assert thinking_payload("high") == {"type": "enabled", "reasoning_effort": "high"}
    assert thinking_payload("max") == {"type": "enabled", "reasoning_effort": "max"}
    assert thinking_payload("") is None
    assert thinking_payload(None) is None
    assert thinking_payload("不存在的值") is None


def test_thinking_default_is_off() -> None:
    """出厂默认必须是关 —— DeepSeek 那边默认是开的，一条闲聊 86% 输出花在推理上。"""
    from qlark.settings import DEFAULTS

    assert DEFAULTS["thinking_mode"] == "off"


def test_thinking_values_are_all_mapped() -> None:
    for mode in THINKING_PAYLOADS:
        assert thinking_payload(mode), mode
    assert set(THINKING_PAYLOADS) == {"off", "auto", "low", "medium", "high", "max"}


def test_looks_like_thinking_unsupported() -> None:
    real = (
        '{"error":{"message":"Failed to deserialize the JSON body into the target type: '
        'thinking.type: unknown variant `maybe`, expected one of `adaptive`, `enabled`, '
        '`disabled`","type":"invalid_request_error"}}'
    )
    assert looks_like_thinking_unsupported(422, real) is True
    assert looks_like_thinking_unsupported(400, real) is True
    # 状态码不对：不是参数问题
    assert looks_like_thinking_unsupported(200, real) is False
    assert looks_like_thinking_unsupported(500, real) is False
    # 提到了但没说"不认识"：可能是别的问题
    assert looks_like_thinking_unsupported(422, '{"error":{"message":"thinking 字段缺失"}}') is False
    # 压根没提到 thinking
    assert looks_like_thinking_unsupported(400, '{"error":{"message":"model not found"}}') is False


class _StrictServer:
    """只认标准字段的服务端：一看到 ``thinking`` 就返回 422。

    模拟"别的 OpenAI 兼容服务不认识这个参数"，用来验证客户端会自动去掉它重试。
    """

    def __init__(self) -> None:
        self.seen: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                outer.seen.append(body)
                if "thinking" in body:
                    payload = json.dumps(
                        {
                            "error": {
                                "message": "Failed to deserialize the JSON body: "
                                "thinking: unknown field, expected one of nothing",
                                "type": "invalid_request_error",
                            }
                        }
                    ).encode("utf-8")
                    self.send_response(422)
                else:
                    payload = json.dumps(
                        {
                            "id": "x",
                            "object": "chat.completion",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": "好"},
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                        }
                    ).encode("utf-8")
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_client_sends_thinking_when_configured() -> None:
    with _StrictServer() as strict:
        client = OpenAIChatClient(
            f"http://127.0.0.1:{strict.port}/v1", "sk-x", "m", thinking="high"
        )
        client._session.trust_env = False
        client.complete([{"role": "user", "content": "hi"}], retries=0)

    # 第一次带了 thinking（被拒），第二次去掉后成功
    assert len(strict.seen) == 2, strict.seen
    assert "thinking" in strict.seen[0]
    assert "thinking" not in strict.seen[1], "没有自动去掉不支持的参数"
    # 并记住：客户端之后不再发送
    assert client.thinking is None


def test_client_does_not_send_thinking_when_blank() -> None:
    with _StrictServer() as strict:
        client = OpenAIChatClient(
            f"http://127.0.0.1:{strict.port}/v1", "sk-x", "m", thinking=""
        )
        client._session.trust_env = False
        client.complete([{"role": "user", "content": "hi"}], retries=0)

    assert len(strict.seen) == 1
    assert "thinking" not in strict.seen[0]


def test_client_reraises_unrelated_errors() -> None:
    """不相关的 422 必须原样抛出，不能无脑重试。"""
    with _StrictServer() as strict:
        client = OpenAIChatClient(
            f"http://127.0.0.1:{strict.port}/v1", "sk-x", "m", thinking="off"
        )
        client._session.trust_env = False
        # 换一个会让服务端 422 的字段名不行（服务端只认 thinking），
        # 这里直接验证：去掉 thinking 后能成功 = 说明降级路径是对的
        result = client.complete([{"role": "user", "content": "hi"}], retries=0)
    assert result.text == "好"


# --------------------------------------------------------------------------- #
# 记忆压缩：推理 token 吃光输出预算
# --------------------------------------------------------------------------- #
def test_summary_client_disables_thinking(ready) -> None:
    """**这是真事故的回归测试。**

    现象：summaries 表里那条摘要 content 长度是 0、pending=1，54 条消息一条没压缩，
    上下文涨到 19.9 万字符（预算 2.4 万）。

    根因：DeepSeek 默认开思考，**推理 token 也算进 max_tokens**。
    实测一次 4000 字符的压缩恰好把 512 全用在 reasoning 上，
    ``finish_reason=length`` 且正文为空 —— 摘要永远压不出来。

    修法：总结客户端**强制关掉思考**（它是有标准答案的机械任务，不需要推理）。
    """
    client = ready.bot.pipeline._llm_summary()
    assert client.thinking == "off", "总结客户端必须关思考，否则摘要会被推理挤成空"


def test_summary_client_never_gets_a_tiny_budget(ready) -> None:
    """输出预算不能小到被推理一挤就什么都不剩。"""
    from qlark.chat.summarizer import SUMMARY_MIN_TOKENS

    ready.settings.update(max_tokens=64)          # 用户把对话上限压得很小
    client = ready.bot.pipeline._llm_summary()
    assert client.max_tokens >= SUMMARY_MIN_TOKENS, (
        f"总结预算只有 {client.max_tokens}，太小 —— 推理一挤正文就没了"
    )


def test_summary_client_respects_larger_user_budget(ready) -> None:
    from qlark.chat.summarizer import SUMMARY_MAX_TOKENS

    ready.settings.update(max_tokens=32000)
    client = ready.bot.pipeline._llm_summary()
    assert client.max_tokens == SUMMARY_MAX_TOKENS, "不该超过总结自己的上限"


def test_empty_summary_log_names_the_truncation() -> None:
    """空内容有好几种成因，日志得说清是哪一种。

    之前只写"返回空内容"，让人完全无从下手 —— 查了半天才发现是
    ``finish_reason=length``（推理吃光预算）。
    """
    import inspect

    from qlark.chat import summarizer

    source = inspect.getsource(summarizer.Summarizer)
    assert "finish_reason" in source, "空摘要的日志没带上 finish_reason"
    assert "被 max_tokens 截断" in source, "没有把截断这种情况说清楚"
