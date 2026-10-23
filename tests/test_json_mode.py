"""``response_format`` 的双重降级。

v2 每一轮都要 JSON，所以发 ``response_format={"type":"json_object"}`` 当第一道保险
（第二道是消息末尾的提醒）。但它是 OpenAI 的扩展语义，各家兼容服务表现不同：

  · MiMo：支持，实测一次就对
  · DeepSeek：**认这个字段，但带历史时返回空内容**（实测 0/8）

所以客户端必须有两条降级路径，否则一次误判就会让机器人彻底不回复：
  ① 服务端报错（400/422 且提到 response_format / json_object）→ 去掉重试，记住
  ② 服务端不报错但返回**空内容** → 同样去掉重试，记住
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from qlark.llm.client import OpenAIChatClient, looks_like_json_mode_unsupported


class _Stub:
    """可编排的假服务：记录每次请求体，按剧本返回。"""

    def __init__(self, script: list[tuple[int, str]]) -> None:
        #: [(状态码, 正文)]，按顺序消费；用完后重复最后一个
        self.script = list(script)
        self.payloads: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.payloads.append(body)
                status, content = outer.script[
                    min(len(outer.payloads) - 1, len(outer.script) - 1)
                ]
                data = {
                    "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3},
                }
                raw = json.dumps(data, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()


def _client(port: int, *, json_mode: bool = True) -> OpenAIChatClient:
    client = OpenAIChatClient(f"http://127.0.0.1:{port}/v1", "sk-x", "m", json_mode=json_mode)
    client._session.trust_env = False
    return client


def test_json_mode_is_sent() -> None:
    with _Stub([(200, '{"messages": ["嗯"]}')]) as stub:
        _client(stub.port).complete([{"role": "user", "content": "hi"}], retries=0)
    assert stub.payloads[0]["response_format"] == {"type": "json_object"}


def test_json_mode_off_sends_nothing() -> None:
    with _Stub([(200, "hi")]) as stub:
        _client(stub.port, json_mode=False).complete(
            [{"role": "user", "content": "hi"}], retries=0
        )
    assert "response_format" not in stub.payloads[0]


def test_error_mentioning_response_format_drops_it_and_retries() -> None:
    """服务端明确说不认这个参数 → 去掉再来一次，并记住别再发。"""
    body = json.dumps(
        {"error": {"message": "response_format is not supported by this model"}}
    )
    with _Stub([(422, body), (200, '{"messages": ["嗯"]}')]) as stub:
        client = _client(stub.port)
        result = client.complete([{"role": "user", "content": "hi"}], retries=0)

    assert result.text == '{"messages": ["嗯"]}'
    assert len(stub.payloads) == 2, "应该重试一次"
    assert "response_format" in stub.payloads[0]
    assert "response_format" not in stub.payloads[1], "重试时不该再带"
    assert client.json_mode is False, "应该记住别再发"


def test_empty_reply_also_drops_json_mode() -> None:
    """不报错但返回空内容 —— DeepSeek 带历史时就是这样，也必须能自救。"""
    with _Stub([(200, ""), (200, '{"messages": ["嗯"]}')]) as stub:
        client = _client(stub.port)
        result = client.complete([{"role": "user", "content": "hi"}], retries=0)

    assert result.text == '{"messages": ["嗯"]}'
    assert len(stub.payloads) == 2
    assert "response_format" not in stub.payloads[1]
    assert client.json_mode is False


def test_other_errors_still_raise() -> None:
    """不相关的 422 必须原样抛出，不能无脑重试。"""
    body = json.dumps({"error": {"message": "max_tokens too large"}})
    with _Stub([(422, body)]) as stub:
        with pytest.raises(Exception):
            _client(stub.port).complete([{"role": "user", "content": "hi"}], retries=0)
    assert len(stub.payloads) == 1


@pytest.mark.parametrize(
    "status, body, expected",
    [
        (422, "response_format is not supported", True),
        (400, "unknown field json_object", True),
        (422, "thinking is not supported", False),
        (500, "response_format is not supported", False),
        (200, "response_format is not supported", False),
    ],
)
def test_matcher(status, body, expected) -> None:
    assert looks_like_json_mode_unsupported(status, body) is expected
