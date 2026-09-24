"""事件服务的 HTTP 行为测试。

这里锁住两个**真实踩过的坑**（2026-09-25 真机 NapCat 4.18.28）：

1. **NapCat 的 httpClient 不发 `Authorization`**，它发的是
   ``x-signature: sha1=<hex(sha1(token + 原始请求体))>``。
   只认 Bearer 时，QLark 会把每一条推送都回 401 —— 机器人"在线但完全不回复"。

2. **鉴权失败时也必须把请求体读干净**。之前 401 早退不读体，keep-alive 连接上
   残留的字节会被当成下一个请求行，NapCat 的 Node 解析器于是报
   ``Parse Error: Expected HTTP/``。这个报错出现在 NapCat 侧，很容易被误判成
   NapCat 的问题。
"""

from __future__ import annotations

import hashlib
import http.client
import json

import pytest

from qlark.events.server import EventServer

TOKEN = "qlark-local"


def sign(token: str, body: bytes) -> str:
    """复刻 NapCat 的签名方式：sha1(token + body) 的十六进制。"""
    return "sha1=" + hashlib.sha1(token.encode("utf-8") + body).hexdigest()


@pytest.fixture
def events() -> list[dict]:
    return []


@pytest.fixture
def server(events: list[dict]):
    instance = EventServer(
        host="127.0.0.1",
        port=0,  # 交给系统分配，避免测试之间抢端口
        token=TOKEN,
        on_event=events.append,
    )
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


def post(conn: http.client.HTTPConnection, body: dict, headers: dict | None = None):
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    merged = {"Content-Type": "application/json", "Content-Length": str(len(raw))}
    merged.update(headers or {})
    conn.request("POST", "/", body=raw, headers=merged)
    response = conn.getresponse()
    payload = response.read()
    return response.status, payload, raw


# --------------------------------------------------------------------------- #
# 鉴权
# --------------------------------------------------------------------------- #
def test_accepts_bearer_token(server: EventServer, events: list[dict]) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        status, _body, _raw = post(conn, {"post_type": "message"}, {"Authorization": f"Bearer {TOKEN}"})
    finally:
        conn.close()
    assert status == 200
    assert events, "带 Bearer 的事件应当被受理"


def test_accepts_napcat_style_x_signature(server: EventServer, events: list[dict]) -> None:
    """NapCat 实际发的是 x-signature，不是 Authorization。"""
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        payload = {"post_type": "message", "message_type": "private", "user_id": 2658495997}
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        conn.request(
            "POST",
            "/",
            body=raw,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(raw)),
                "x-self-id": "2157649443",
                "x-signature": sign(TOKEN, raw),
            },
        )
        response = conn.getresponse()
        status = response.status
        response.read()
    finally:
        conn.close()

    assert status == 200, "NapCat 的 x-signature 必须被接受，否则一条消息都收不到"
    assert events and events[0]["user_id"] == 2658495997


def test_rejects_wrong_signature(server: EventServer, events: list[dict]) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        status, _b, _r = post(conn, {"a": 1}, {"x-signature": sign("wrong-token", b'{"a": 1}')})
    finally:
        conn.close()
    assert status in (401, 400), "签名不对必须拒绝"
    assert events == []


def test_rejects_missing_auth(server: EventServer, events: list[dict]) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        status, _b, _r = post(conn, {"a": 1})
    finally:
        conn.close()
    assert status == 401
    assert events == []


# --------------------------------------------------------------------------- #
# keep-alive 上的请求体必须被读走
# --------------------------------------------------------------------------- #
def test_rejected_request_still_drains_body(server: EventServer, events: list[dict]) -> None:
    """被拒绝的请求也必须把请求体读干净，否则会污染同一条连接上的下一个请求。

    这是 NapCat 报 `Parse Error: Expected HTTP/` 的直接原因：它的 httpClient 复用
    连接连续推送，而 QLark 在 401 早退时把请求体留在了缓冲区里。
    """
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        # 1) 一个大 body 的无鉴权请求 —— 会被 401 拒绝
        big = {"post_type": "message", "filler": "x" * 2000}
        status1, _b1, _r1 = post(conn, big)
        assert status1 == 401

        # 2) 同一条连接上再发一个合法请求
        payload = {"post_type": "message", "marker": "second"}
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        conn.request(
            "POST",
            "/",
            body=raw,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(raw)),
                "x-signature": sign(TOKEN, raw),
            },
        )
        response = conn.getresponse()
        status2 = response.status
        response.read()
    finally:
        conn.close()

    assert status2 == 200, f"第二个请求被上一个残留的请求体搞坏了（HTTP {status2}）"
    assert [e.get("marker") for e in events] == ["second"]


# --------------------------------------------------------------------------- #
# 响应本身必须能被 NapCat 的 Node 解析器接受
# --------------------------------------------------------------------------- #
def test_success_response_is_json_not_204(server: EventServer) -> None:
    """成功时必须回带 JSON 体的 200。

    不能回 ``204 No Content``：RFC 7230 规定 204 **不允许带 Content-Length**，
    而 Python 的 ``send_response`` 会让我们补一个 ``Content-Length: 0``。
    NapCat 用的 Node 解析器对这种响应很敏感。
    """
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        raw = b"{}"
        conn.request(
            "POST",
            "/",
            body=raw,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(raw)),
                "x-signature": sign(TOKEN, raw),
            },
        )
        response = conn.getresponse()
        status = response.status
        content_length = response.getheader("Content-Length")
        body = response.read()
    finally:
        conn.close()

    assert status == 200
    assert content_length == "2", "必须有正确的 Content-Length"
    assert json.loads(body) == {}, "空对象表示'没有快速操作'"


def test_health_endpoint(server: EventServer) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        conn.request("GET", "/_qlark/health")
        response = conn.getresponse()
        data = json.loads(response.read())
    finally:
        conn.close()
    assert data["ok"] is True
    assert data["port"] == server.port
