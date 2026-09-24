"""接收 NapCat 事件推送的本地 HTTP 服务。

**为什么需要它**：OneBot 的 HTTP 服务端模式是**单工**的——只能我们主动 POST 到 NapCat，
NapCat 不会把消息推给我们（技术文档 §3.2）。所以必须再开一个方向：

.. code-block:: text

    NapCat  ──(httpClients: POST 事件)──>  QLark EventServer (127.0.0.1:3001)
    QLark   ──(httpServers: POST 动作)──>  NapCat OneBot (127.0.0.1:3000)

事件服务只做三件事：**鉴权 → 解析 → 立刻入队并返回**。任何耗时操作都不能在这里做，
否则会拖住 NapCat 的事件推送。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from ..core.models import LogSource
from ..napcat.logbus import LogBus

log = logging.getLogger(__name__)

#: 单条事件负载上限（防止异常请求吃内存）
MAX_BODY_BYTES = 4 * 1024 * 1024
#: 健康检查路径
HEALTH_PATH = "/_qlark/health"


class EventServer:
    """把 NapCat 推来的事件转交给流水线。"""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 3001,
        token: str = "",
        on_event: Callable[[dict[str, Any]], None],
        logbus: LogBus | None = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.token = token
        self._on_event = on_event
        self._logbus = logbus

        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

        self.received = 0
        self.rejected = 0
        self.invalid = 0
        self.last_event_at: float | None = None

    # ------------------------------------------------------------------ 属性
    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "host": self.host,
            "port": self.port,
            "received": self.received,
            "rejected": self.rejected,
            "invalid": self.invalid,
            "last_event_at": self.last_event_at,
        }

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> int:
        """开始监听，返回实际端口。端口被占用时抛 :class:`OSError`。"""
        with self._lock:
            if self._server is not None:
                return self.port

            try:
                server = _make_server(self.host, self.port, _build_handler(self))
            except OSError as exc:
                raise OSError(
                    f"事件服务无法监听 {self.host}:{self.port}（{exc}）"
                ) from exc

            self._server = server
            self._thread = threading.Thread(
                target=server.serve_forever, name="qlark-events", daemon=True
            )
            self._thread.start()
            self._log(f"事件服务已启动：{self.base_url}")
            return self.port

    def stop(self) -> None:
        with self._lock:
            server, self._server = self._server, None
            thread, self._thread = self._thread, None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                log.exception("关闭事件服务失败")
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)
        self._log("事件服务已停止", level="debug")

    # ------------------------------------------------------------------ 内部
    def _log(self, message: str, *, level: str = "info") -> None:
        if self._logbus is not None:
            self._logbus.add(message, level=level, source=LogSource.QLARK.value)

    def authorized(self, header: str | None) -> bool:
        """校验 ``Authorization`` 头。未配置 token 时一律放行（仅限本机监听）。"""
        if not self.token:
            return True
        return header == f"Bearer {self.token}"

    def handle_payload(self, payload: dict[str, Any]) -> None:
        """处理一条事件（由 HTTP 线程调用，**必须立刻返回**）。"""
        self.received += 1
        self.last_event_at = time.time()
        try:
            self._on_event(payload)
        except Exception:
            log.exception("事件投递失败")
            self._log("事件投递失败，已忽略该事件", level="warn")


# --------------------------------------------------------------------------- #
# HTTP 处理
# --------------------------------------------------------------------------- #
def _build_handler(owner: EventServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "QLark-EventServer/1.0"

        # NapCat 每次推送都写访问日志会把界面日志刷爆，静音
        def log_message(self, *args: Any) -> None:  # noqa: D102
            return

        # ---------------------------------------------------------- 工具
        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_empty(self, status: int) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        # ---------------------------------------------------------- 路由
        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?", 1)[0] == HEALTH_PATH:
                self._send_json(200, {"ok": True, **owner.snapshot()})
                return
            self._send_json(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            try:
                self._handle_post()
            except Exception:
                log.exception("事件服务处理请求异常")
                try:
                    self._send_json(500, {"ok": False, "error": "internal error"})
                except Exception:
                    pass

        def _handle_post(self) -> None:
            if not owner.authorized(self.headers.get("Authorization")):
                owner.rejected += 1
                self._send_json(401, {"ok": False, "error": "unauthorized"})
                return

            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0

            if length <= 0 or length > MAX_BODY_BYTES:
                owner.invalid += 1
                self._send_json(400, {"ok": False, "error": "bad content-length"})
                return

            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                owner.invalid += 1
                self._send_json(400, {"ok": False, "error": "invalid json"})
                return

            if not isinstance(payload, dict):
                owner.invalid += 1
                self._send_json(400, {"ok": False, "error": "payload must be an object"})
                return

            # 先入队再应答：入队是非阻塞的，绝不能在这里做耗时工作
            owner.handle_payload(payload)
            self._send_empty(204)

    return Handler


def _make_server(host: str, port: int, handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    class _Server(ThreadingHTTPServer):
        daemon_threads = True
        # 不开 SO_REUSEADDR：Windows 上它会允许两个进程绑同一端口，
        # 端口冲突就变成"静默抢占"而不是报错
        allow_reuse_address = False

    return _Server((host, port), handler)


__all__ = ["HEALTH_PATH", "MAX_BODY_BYTES", "EventServer"]
