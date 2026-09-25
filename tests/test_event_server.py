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
import hmac
import http.client
import json

import pytest

from qlark.events.server import EventServer

TOKEN = "qlark-local"


def sign(token: str, body: bytes) -> str:
    """复刻 NapCat 的签名：**HMAC-SHA1（token 作密钥）**。

    NapCat 源码里是 ``import { createHmac as _L } from "crypto"`` 然后
    ``_L("sha1", token).update(body).digest("hex")``。

    注意**不是** ``sha1(token + body)`` —— 那个写法能和同样写错的自写脚本
    互相"验证通过"，却对不上真身发来的签名（每条事件 401）。
    """
    return "sha1=" + hmac.new(token.encode("utf-8"), body, hashlib.sha1).hexdigest()


#: 由 **Node 的 crypto**（与 NapCat 同一个库）算出的参考值，用来钉死算法。
#: 命令：node -e "console.log(require('crypto').createHmac('sha1','qlark-local')
#:        .update('{\"post_type\":\"message\",\"message_id\":1}').digest('hex'))"
NODE_REFERENCE_TOKEN = "qlark-local"
NODE_REFERENCE_BODY = b'{"post_type":"message","message_id":1}'
NODE_REFERENCE_SIGNATURE = "3aeb48cc22f3c4f7208f2353606bbebdcea1f1bd"


def test_signature_algorithm_matches_node_reference() -> None:
    """签名算法必须与 NapCat 用的 Node crypto 逐位一致。

    这是**跨实现**的已知答案测试：期望值由 Node 生成、硬编码在这里。
    如果哪天有人（包括我）把算法改成 ``sha1(token + body)`` 之类的"看起来也对"
    写法，这条会立刻失败 —— 而不是等到真机上每条事件都 401 才发现。
    """
    produced = sign(NODE_REFERENCE_TOKEN, NODE_REFERENCE_BODY)
    assert produced == f"sha1={NODE_REFERENCE_SIGNATURE}", (
        f"签名算法与 NapCat 不一致：得到 {produced}"
    )


def test_wrong_algorithm_is_not_accepted(server: EventServer, events: list[dict]) -> None:
    """``sha1(token + body)`` 这种错误写法必须被拒（防止回归到旧实现）。"""
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        raw = b'{"post_type":"message"}'
        naive = "sha1=" + hashlib.sha1(TOKEN.encode("utf-8") + raw).hexdigest()
        status, _b, _r = post(conn, {"post_type": "message"}, {"x-signature": naive})
    finally:
        conn.close()
    assert status == 401
    assert events == []


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


# --------------------------------------------------------------------------- #
# chunked 传输：真 NapCat 走的正是这条
# --------------------------------------------------------------------------- #
def post_chunked(conn: http.client.HTTPConnection, body: bytes, headers: dict | None = None):
    """完全不设 Content-Length，改用 chunked —— 与真 NapCat 的 httpClient 一致。

    真身只设 Content-Type / x-self-id / x-signature，Node 就自动切成 chunked 了。
    只认 Content-Length 的服务端会读到空体：验签失败（每条事件 401），
    而且没读走的字节会污染 keep-alive 连接上的下一个请求。
    """
    conn.putrequest("POST", "/", skip_accept_encoding=True)
    conn.putheader("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        conn.putheader(key, value)
    conn.putheader("Transfer-Encoding", "chunked")
    conn.endheaders()
    conn.send(f"{len(body):X}\r\n".encode("ascii") + body + b"\r\n0\r\n\r\n")
    response = conn.getresponse()
    payload = response.read()
    return response.status, payload


def test_accepts_chunked_body_with_signature(server: EventServer, events: list[dict]) -> None:
    """真 NapCat 不设 Content-Length，走 chunked —— 这条必须过。

    踩过的坑：服务端只读 Content-Length，于是把 chunked 请求体读成空，
    验签对不上 → 每条事件都回 401，机器人一个字都不回。
    """
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        payload = {"post_type": "message", "marker": "chunked", "filler": "x" * 3000}
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, _body = post_chunked(
            conn, raw, {"x-self-id": "2157649443", "x-signature": sign(TOKEN, raw)}
        )
    finally:
        conn.close()

    assert status == 200, f"chunked 请求体没被读出来（HTTP {status}）"
    assert [e.get("marker") for e in events] == ["chunked"]


def test_chunked_body_keeps_connection_usable(server: EventServer, events: list[dict]) -> None:
    """chunked 也必须读干净，否则下一个请求会被残留字节搞坏。"""
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        for index in range(3):
            raw = json.dumps({"marker": index, "filler": "y" * 2000}).encode("utf-8")
            status, _body = post_chunked(conn, raw, {"x-signature": sign(TOKEN, raw)})
            assert status == 200, f"第 {index + 1} 个 chunked 请求失败（HTTP {status}）"
    finally:
        conn.close()
    assert [e.get("marker") for e in events] == [0, 1, 2]


def test_chunked_multi_chunk_body(server: EventServer, events: list[dict]) -> None:
    """分成多个 chunk 发送也要能拼回完整请求体。"""
    payload = {"marker": "multi", "text": "中" * 500}
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    half = len(raw) // 2
    parts = [raw[:half], raw[half:]]

    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        conn.putrequest("POST", "/", skip_accept_encoding=True)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("x-signature", sign(TOKEN, raw))
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        for part in parts:
            conn.send(f"{len(part):X}\r\n".encode("ascii") + part + b"\r\n")
        conn.send(b"0\r\n\r\n")
        response = conn.getresponse()
        status = response.status
        response.read()
    finally:
        conn.close()

    assert status == 200
    assert events and events[0]["marker"] == "multi"


def test_chunked_with_bad_signature_is_rejected(server: EventServer, events: list[dict]) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        raw = b'{"marker": "bad"}'
        status, _body = post_chunked(conn, raw, {"x-signature": sign("wrong", raw)})
    finally:
        conn.close()
    assert status == 401
    assert events == []


def test_oversized_chunked_body_is_rejected(server: EventServer, events: list[dict]) -> None:
    """超大 chunked 请求体要拒绝并断开，不能把内存吃爆。"""
    import qlark.events.server as server_module

    original = server_module.MAX_BODY_BYTES
    server_module.MAX_BODY_BYTES = 1024
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        raw = b"z" * 5000
        status, _body = post_chunked(conn, raw, {"x-signature": sign(TOKEN, raw)})
    finally:
        conn.close()
        server_module.MAX_BODY_BYTES = original

    assert status == 401, "鉴权在读取之后才判；超大时先失败也算可接受，但不能 200"
    assert events == []


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
