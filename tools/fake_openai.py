"""假 OpenAI 兼容服务：在没有真实 API Key 的情况下端到端测试聊天机器人。

它只做三件事，而且全部可控：

1. 接受 ``POST {base_url}/chat/completions``，按**可预测的规则**拼出一条回复；
2. 记录收到的每一个请求体，供测试回看"客户端到底发了什么"；
3. 支持注入错误（``POST /_qlark/fail``），用来验证客户端的重试与降级。

**仅使用 Python 标准库**（``http.server`` / ``json`` / ``threading`` 等），不需要装任何依赖。

用法::

    python tools/fake_openai.py [--port 8999] [--host 127.0.0.1] [--reply "固定回复"]

把被测客户端的 ``base_url`` 指向 ``http://127.0.0.1:8999/v1`` 即可；
不带 ``/v1`` 的 ``http://127.0.0.1:8999`` 同样可用（两种路径都接受）。
``Ctrl+C`` 干净退出，返回码 0。

接口一览：

===================================  ==================================================
``POST /v1/chat/completions``        OpenAI 兼容补全（``/chat/completions`` 等价）
``POST /_qlark/reset``               清空请求历史，并重置 id 计数；返回 ``{"ok": true}``
``GET  /_qlark/requests``            ``{"count": N, "requests": [...], "meta": [...]}``
``POST /_qlark/fail``                ``{"times": 2, "status": 500, "message": "boom"}``
``POST /_qlark/ok``                  恢复正常；返回 ``{"ok": true}``
``GET  /_qlark/health``              ``{"ok": true, "requests": N, "fail_remaining": N}``
===================================  ==================================================

回复规则（测试要能预测）：

* 环境变量 ``FAKE_OPENAI_REPLY`` 或 ``--reply`` 已给出非空文本 → 直接用它作为回复；
* 否则回复 ``"收到：<最后一条 user 消息的纯文本>"``。多模态 content 数组里所有
  ``type == "text"`` 的片段按原顺序拼接，每个 ``image_url`` 片段替换成 ``[图片]``，
  所以"只发了一张图"的回复就是 ``"收到：[图片]"``；
* ``messages`` 为空、或没有任何 user 消息 → 回复 ``"收到："``。

用量规则（同样可预测）：

* ``prompt_tokens`` = 所有消息文本的字符数之和；多模态里每个 ``image_url`` 固定按
  ``85`` 个 token 计（不按字符数，也不看 base64 长度）；
* ``completion_tokens`` = 回复文本的字符数；
* ``total_tokens`` = 两者之和。

关于记录：``/_qlark/requests`` 的 ``requests`` 就是**原样的请求体列表**（因此
``requests[0]["messages"]`` 可以直接用）；``meta`` 是与之等长的并行列表，用来放
不属于请求体的信息（``Authorization`` 头就记在这里）。只有 ``/chat/completions``
的请求会被记录，``/_qlark/*`` 自身不会。

可用环境变量（命令行参数优先）：

===================================  ==============================================
``FAKE_OPENAI_PORT``                 监听端口（默认 8999）
``FAKE_OPENAI_HOST``                 监听地址（默认 127.0.0.1）
``FAKE_OPENAI_REPLY``                固定回复文本（空字符串视为未设置）
===================================  ==============================================
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

# 真实 OpenAI 客户端 / 本项目其它工具都把 stdout 当 UTF-8 读；这里强制一致，
# 否则中文在 Windows 管道里会跟随控制台代码页（GBK）变成乱码。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:  # pragma: no cover - 个别环境下 stdout 不可重配置
    pass

#: 一个图片片段折算的 prompt token 数（固定值，便于测试断言）
IMAGE_TOKENS = 85
#: 流式回复时每个 data 分片携带的字符数（仅为可预测，不代表真实分词）
STREAM_DELTA_CHARS = 4

#: 补全端点：``base_url`` 带 ``/v1`` 与不带时各一种写法
CHAT_PATHS = ("/v1/chat/completions", "/chat/completions")

#: 固定回复文本；``None`` 表示按"收到：..."的默认规则生成。由 :func:`main` 设置。
FIXED_REPLY: str | None = None


def emit(text: str) -> None:
    """往 stdout 写一行（立刻 flush，方便测试脚本等就绪信息）。"""
    print(text, flush=True)


# --------------------------------------------------------------------------- #
# 纯函数：回复文本与用量计算（不碰 IO，测试里可以直接 import 复用）
# --------------------------------------------------------------------------- #
def _content_text(content: object) -> str:
    """把一条消息的 content 压成"给人看的纯文本"，图片片段转成 ``[图片]``。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    pieces: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "image_url":
            pieces.append("[图片]")
            continue
        # type == "text"（以及未知但带 text 的类型）都取 text
        text = part.get("text")
        if isinstance(text, str):
            pieces.append(text)
    return "".join(pieces)


def _content_tokens(content: object) -> int:
    """把一条消息的 content 折算成 prompt token 数（图片固定 ``IMAGE_TOKENS``）。"""
    if isinstance(content, str):
        return len(content)
    if not isinstance(content, list):
        return 0
    total = 0
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "image_url":
            total += IMAGE_TOKENS
            continue
        text = part.get("text")
        if isinstance(text, str):
            total += len(text)
    return total


def _last_user_text(messages: list[object]) -> str | None:
    """返回最后一条 user 消息的纯文本；没有 user 消息时返回 ``None``。"""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            return _content_text(message.get("content"))
    return None


def count_prompt_tokens(messages: object) -> int:
    """``prompt_tokens``：所有消息文本字符数之和，外加每个图片片段的 85。"""
    if not isinstance(messages, list):
        return 0
    return sum(
        _content_tokens(message.get("content"))
        for message in messages
        if isinstance(message, dict)
    )


#: 我们的【输出规则】里一定有这一句。它出现就说明这次请求在要 JSON 输出。
#: v2 起规则**每一轮都在**，所以走完整流水线的请求都会命中。
JSON_MARKER = "只输出一个 JSON 对象"


def _wants_json(messages: list) -> bool:
    """这次请求是不是要求 JSON 输出（按提示词里有没有那句标记判断）。"""
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            if JSON_MARKER in content:
                return True
            continue
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and JSON_MARKER in str(part.get("text") or ""):
                    return True
    return False


def build_reply(body: dict) -> str:
    """按固定规则生成回复文本（见模块 docstring）。"""
    scripted = STATE.take_reply()
    if scripted is not None:
        return scripted
    if FIXED_REPLY is not None:
        return FIXED_REPLY
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return "收到："
    text = _last_user_text(messages)
    if text is None:
        return "收到："
    # 提示词要求 JSON 就吐一个合法的 JSON —— 走完整流水线的测试看到的
    # 回复文本仍然和以前一模一样（「收到：xxx」），只是格式符合 v2 的约定。
    if _wants_json(messages):
        return json.dumps({"messages": [f"收到：{text}"]}, ensure_ascii=False)
    return f"收到：{text}"


def _error_body(message: str) -> dict:
    """OpenAI 风格的错误体。

    所有错误（含 404/400）都复用这个形状，方便客户端统一解析。
    """
    return {"error": {"message": message, "type": "fake_error", "code": "fake"}}


def _normalize_path(raw: str) -> str:
    """去掉查询串与末尾多余的 ``/``，便于路径匹配。"""
    path = urlsplit(raw).path
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    return path


def _env_int(name: str, default: int) -> int:
    """读一个整数环境变量，非法时退回默认值。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        emit(f"[fake-openai] 环境变量 {name}={raw!r} 不是整数，改用 {default}")
        return default


# --------------------------------------------------------------------------- #
# 共享状态：请求历史 + 失败注入
# --------------------------------------------------------------------------- #
class _State:
    """进程内共享的可变状态，所有访问都在锁里完成。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[dict] = []  # 原样的请求体
        self.meta: list[dict] = []  # 与 requests 等长的附加信息
        self.fail_remaining = 0  # 还要失败几次
        self.fail_infinite = False  # True 表示一直失败
        self.fail_status = 500
        self.fail_message = "boom"
        self.seq = 0  # 成功回复的序号，用于 chatcmpl-fake-<n>
        #: 排队的固定回复。非空时按顺序取，用来测分段/表情/JSON 重试这些路径
        self.replies: list[str] = []
        #: 队列空了之后，最后一条是否反复使用（False = 退回默认的"收到：..."）
        self.repeat_last = False

    # -- 固定回复 --------------------------------------------------------- #
    def take_reply(self) -> str | None:
        """取一条排队的固定回复；没有就返回 ``None`` 走默认回显。"""
        with self.lock:
            if not self.replies:
                return None
            if len(self.replies) == 1 and self.repeat_last:
                return self.replies[0]
            return self.replies.pop(0)

    def set_replies(self, replies: list[str] | None, repeat_last: bool) -> None:
        with self.lock:
            self.replies = [str(item) for item in (replies or [])]
            self.repeat_last = bool(repeat_last)

    # -- 记录 ------------------------------------------------------------- #
    def record(self, body: dict, path: str, authorization: str | None,
               user_agent: str | None, failed_status: int | None) -> None:
        with self.lock:
            index = len(self.requests) + 1
            self.requests.append(body)
            self.meta.append({
                "index": index,
                "path": path,
                "authorization": authorization,  # 请求带了才非 None
                "user_agent": user_agent,
                "model": body.get("model"),
                "stream": bool(body.get("stream")),
                "message_count": len(body["messages"]) if isinstance(body.get("messages"), list) else 0,
                "failed_status": failed_status,  # None 表示这次成功返回
            })

    def snapshot(self) -> tuple[list[dict], list[dict]]:
        with self.lock:
            return list(self.requests), list(self.meta)

    # -- id 与失败注入 ----------------------------------------------------- #
    def next_id(self) -> str:
        """成功回复才消耗序号，所以 reset 之后第一条又是 ``chatcmpl-fake-1``。"""
        with self.lock:
            self.seq += 1
            return f"chatcmpl-fake-{self.seq}"

    def take_failure(self) -> tuple[int, str] | None:
        """取出一次失败注入；返回 ``None`` 表示正常返回。"""
        with self.lock:
            if self.fail_infinite:
                return self.fail_status, self.fail_message
            if self.fail_remaining > 0:
                self.fail_remaining -= 1
                return self.fail_status, self.fail_message
            return None

    def set_failure(self, times: int | None, status: int, message: str) -> None:
        with self.lock:
            self.fail_status = status
            self.fail_message = message
            if times is None:
                self.fail_infinite = True
                self.fail_remaining = 0
            else:
                self.fail_infinite = False
                self.fail_remaining = max(0, times)  # times=0 等价于恢复正常

    def clear_failure(self) -> None:
        with self.lock:
            self.fail_infinite = False
            self.fail_remaining = 0

    def failure_view(self) -> int:
        """给 ``/_qlark/health`` 用；``-1`` 表示"一直失败"。"""
        with self.lock:
            return -1 if self.fail_infinite else self.fail_remaining

    def request_count(self) -> int:
        with self.lock:
            return len(self.requests)

    def reset(self) -> None:
        """清空请求历史并重置 id 计数（失败注入状态不动，用 /_qlark/ok 恢复）。"""
        with self.lock:
            self.requests.clear()
            self.meta.clear()
            self.seq = 0


STATE = _State()


class _BadRequest(Exception):
    """请求体不合规（缺 Content-Length、不是合法 JSON、不是 JSON 对象……）。"""


# --------------------------------------------------------------------------- #
# HTTP 处理
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    """所有端点的处理器。``/_qlark/*`` 是给测试用的控制面。"""

    protocol_version = "HTTP/1.1"
    server_version = "FakeOpenAI/1.0"
    sys_version = ""

    # 静默：不往 stdout 刷访问日志（send_response/send_error 都会走这里）
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    # -- 路由 ------------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的命名约定
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def _dispatch(self, method: str) -> None:
        try:
            path = _normalize_path(self.path)
            if method == "POST" and path in CHAT_PATHS:
                self._handle_chat(path)
            elif method == "GET" and path == "/_qlark/requests":
                self._handle_requests()
            elif method == "GET" and path == "/_qlark/health":
                self._handle_health()
            elif method == "POST" and path == "/_qlark/reset":
                STATE.reset()
                self._send_json(200, {"ok": True})
            elif method == "POST" and path == "/_qlark/fail":
                self._handle_fail()
            elif method == "POST" and path == "/_qlark/reply":
                self._handle_reply()
            elif method == "POST" and path == "/_qlark/ok":
                STATE.clear_failure()
                self._send_json(200, {"ok": True})
            else:
                # 非 POST 打到已知路径、或路径本身不认识，都算 404
                self._send_json(404, _error_body(
                    f"未找到 {method} {path}；本服务只提供 "
                    f"{'、'.join(CHAT_PATHS)} 与 /_qlark/*"
                ))
        except _BadRequest as exc:
            self._send_json(400, _error_body(str(exc)))
        except Exception as exc:  # pragma: no cover - 兜底，避免测试端干等
            try:
                self._send_json(500, _error_body(f"假服务内部错误：{exc!r}"))
            except Exception:
                pass

    # -- 聊天补全 --------------------------------------------------------- #
    def _handle_chat(self, path: str) -> None:
        body = self._read_json(empty_ok=False)
        failure = STATE.take_failure()
        # 无论成功还是失败，收到的请求体都要留档（失败注入也是测试内容）
        STATE.record(
            body,
            path,
            self.headers.get("Authorization"),
            self.headers.get("User-Agent"),
            failure[0] if failure else None,
        )
        if failure is not None:
            status, message = failure
            self._send_json(status, _error_body(message))
            return

        model = body.get("model")
        if not isinstance(model, str) or not model:
            model = "fake-model"
        reply = build_reply(body)
        prompt_tokens = count_prompt_tokens(body.get("messages"))
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": len(reply),  # 按字符数算，可预测
            "total_tokens": prompt_tokens + len(reply),
        }
        if body.get("stream"):
            self._send_stream(model, reply, usage)
            return

        completion_id = STATE.next_id()
        self._send_json(200, {
            "id": completion_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,  # 回显请求里的 model
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }],
            "usage": usage,
        })

    def _send_stream(self, model: str, reply: str, usage: dict) -> None:
        """``stream: true`` 时按 SSE 逐片返回（本工具的加分项，见模块 docstring）。"""
        completion_id = STATE.next_id()
        created = int(time.time())

        def chunk(delta: dict, finish_reason: str | None, include_usage: bool) -> dict:
            return {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
                "usage": usage if include_usage else None,
            }

        # 没有 Content-Length 的响应必须靠关连接来结束
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self._sse(chunk({"role": "assistant", "content": ""}, None, False))
            for start in range(0, len(reply), STREAM_DELTA_CHARS):
                piece = reply[start:start + STREAM_DELTA_CHARS]
                self._sse(chunk({"content": piece}, None, False))
            self._sse(chunk({}, "stop", True))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):  # 客户端提前断开
            pass

    def _sse(self, payload: dict) -> None:
        line = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        self.wfile.write(line.encode("utf-8"))
        self.wfile.flush()

    # -- 控制面 ----------------------------------------------------------- #
    def _handle_requests(self) -> None:
        requests, meta = STATE.snapshot()
        self._send_json(200, {"count": len(requests), "requests": requests, "meta": meta})

    def _handle_health(self) -> None:
        self._send_json(200, {
            "ok": True,
            "requests": STATE.request_count(),
            "fail_remaining": STATE.failure_view(),  # -1 表示一直失败
        })

    def _handle_fail(self) -> None:
        body = self._read_json(empty_ok=True)
        times: int | None = None
        if "times" in body and body["times"] is not None:
            raw_times = body["times"]
            if not isinstance(raw_times, int) or isinstance(raw_times, bool) or raw_times < 0:
                raise _BadRequest(f"times 必须是非负整数，收到 {raw_times!r}")
            times = raw_times

        status = body.get("status", 500)
        if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
            raise _BadRequest(f"status 必须是 100~599 的整数，收到 {status!r}")

        message = body.get("message", "boom")
        if not isinstance(message, str):
            message = str(message)

        STATE.set_failure(times, status, message)
        self._send_json(200, {"ok": True})

    def _handle_reply(self) -> None:
        """排队若干条固定回复，用来测分段/表情/JSON 重试这些路径。

        ``{"replies": ["...", "..."], "repeat_last": true}``
        ``replies`` 为空（或省略）表示清空，退回默认的 ``收到：...`` 回显。
        """
        body = self._read_json(empty_ok=True)
        raw = body.get("replies")
        if raw is None:
            raw = []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            raise _BadRequest("replies 必须是字符串或字符串数组")
        repeat_last = bool(body.get("repeat_last", False))
        STATE.set_replies([str(item) for item in raw], repeat_last)
        self._send_json(
            200, {"ok": True, "queued": len(STATE.replies), "repeat_last": repeat_last}
        )

    # -- 读写工具 --------------------------------------------------------- #
    def _read_json(self, *, empty_ok: bool) -> dict:
        """读取并解析请求体；空体在 ``empty_ok`` 时视为 ``{}``。"""
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise _BadRequest("缺少 Content-Length（本假服务不支持 chunked 上传）")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise _BadRequest(f"Content-Length 非法：{raw_length!r}") from exc

        raw = self.rfile.read(length) if length > 0 else b""
        if not raw.strip():
            if empty_ok:
                return {}
            raise _BadRequest("请求体为空")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _BadRequest(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(body, dict):
            raise _BadRequest("请求体必须是 JSON 对象")
        return body

    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":  # HEAD 只回头部
            self.wfile.write(data)


class Server(ThreadingHTTPServer):
    """线程化 HTTP 服务；守护线程避免测试结束时卡住。"""

    daemon_threads = True
    allow_reuse_address = True


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def _clip(text: str, limit: int = 40) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="假 OpenAI 兼容服务（仅标准库）")
    parser.add_argument("--port", type=int, default=None,
                        help="监听端口；默认取 FAKE_OPENAI_PORT，再默认 8999")
    parser.add_argument("--host", default=None,
                        help="监听地址；默认取 FAKE_OPENAI_HOST，再默认 127.0.0.1")
    parser.add_argument("--reply", default=None,
                        help="固定回复文本；优先于 FAKE_OPENAI_REPLY")
    args = parser.parse_args(argv)

    host = args.host or os.environ.get("FAKE_OPENAI_HOST") or "127.0.0.1"
    port = args.port if args.port is not None else _env_int("FAKE_OPENAI_PORT", 8999)
    reply = args.reply if args.reply is not None else os.environ.get("FAKE_OPENAI_REPLY")

    global FIXED_REPLY
    FIXED_REPLY = reply or None  # 空字符串按"未设置"处理

    try:
        server = Server((host, port), Handler)
    except OSError as exc:
        emit(f"[fake-openai] 无法监听 {host}:{port}：{exc}")
        return 1

    bound_host, bound_port = server.server_address[0], server.server_address[1]
    shown = "默认" if FIXED_REPLY is None else f"固定：{_clip(FIXED_REPLY)}"
    emit(f"[fake-openai] listening on http://{bound_host}:{bound_port}/v1  (reply: {shown})")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        emit("[fake-openai] 收到 Ctrl+C，正在停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
