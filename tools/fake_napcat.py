"""假 NapCat：用于在缺少真实运行时的情况下端到端验证 QLark。

它复刻真实 NapCat 的**对外可观测行为**，全部来自《NapCat 部署技术文档》的实测记录，
以及 M2 新增的事件推送语义：

* 输出 ``正在快速登录  <QQ号>`` → ``Worker进程已登录成功``
* 未登录时输出 ``没有 -q 指令指定快速登录，将使用二维码登录方式``，并写
  ``napcat/cache/qrcode.png``（真实 NapCat 约 121 秒重写一次）
* 登录成功后写 ``napcat_<QQ号>.json``（**只在账号首次注册时**）与
  ``napcat_protocol_<QQ号>.json``（每次登录都重写）
* **只有** ``onebot11_<QQ>.json`` 里配了启用中的 ``httpServers`` 才开动作端口；
  配了 ``httpClients`` 才会把事件推给 QLark
* 动作端口实现 ``get_login_info`` / ``get_status`` / ``get_version_info`` /
  ``send_group_msg`` / ``send_private_msg``，并校验 ``Authorization: Bearer <token>``

用法::

    python tools/fake_napcat.py [-q <QQ号>]

可用环境变量：

===============================  ==============================================
``FAKE_NAPCAT_QQ``               账号（默认 2157649443）
``FAKE_NAPCAT_PORT``             OneBot 动作端口（默认 3000）
``FAKE_NAPCAT_TOKEN``            token（默认 qlark-local）
``FAKE_NAPCAT_HAVE_CRED``        设为 ``1`` 表示本机有登录凭据，快速登录可成功
``FAKE_NAPCAT_QR_INTERVAL``      二维码重写间隔秒数（默认 3，真实约 121）
``FAKE_NAPCAT_SCAN_DELAY``       扫码后多久算登录成功（默认 2 秒）
``FAKE_NAPCAT_ACCOUNT_SIGNAL``   ``once``（默认，真实行为）/ ``always`` / ``never``
``FAKE_NAPCAT_PROTOCOL_SIGNAL``  ``always``（默认）/ ``never``
===============================  ==============================================

测试控制接口（不走 token 校验）：

* ``POST /_qlark/inject``  注入一条消息事件并推送给 QLark
* ``GET  /_qlark/sent``    查看机器人发出过的消息
* ``POST /_qlark/reset``   清空记录与消息计数器
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

QQ = os.environ.get("FAKE_NAPCAT_QQ", "2157649443")
PORT = int(os.environ.get("FAKE_NAPCAT_PORT", "3000"))
TOKEN = os.environ.get("FAKE_NAPCAT_TOKEN", "qlark-local")
HAVE_CRED = os.environ.get("FAKE_NAPCAT_HAVE_CRED", "0") == "1"
QR_INTERVAL = float(os.environ.get("FAKE_NAPCAT_QR_INTERVAL", "3"))
SCAN_DELAY = float(os.environ.get("FAKE_NAPCAT_SCAN_DELAY", "2"))

#: ``napcat_<QQ>.json`` 的写入策略。
#: 真实 NapCat **只在账号首次注册时**写这个文件，之后登录不再重写 ——
#: 这正是 QLark 曾经漏判"第二次扫码登录"的原因，所以默认按真实行为模拟。
ACCOUNT_SIGNAL = os.environ.get("FAKE_NAPCAT_ACCOUNT_SIGNAL", "once")  # once|always|never
#: ``napcat_protocol_<QQ>.json`` 的写入策略（真实 NapCat 每次登录都会重新生成）
PROTOCOL_SIGNAL = os.environ.get("FAKE_NAPCAT_PROTOCOL_SIGNAL", "always")  # always|never

BASE = Path(__file__).resolve().parent.parent  # 项目根
CWD = Path.cwd()
CONFIG_DIR = CWD / "napcat" / "config"
CACHE_DIR = CWD / "napcat" / "cache"
QRCODE_PATH = CACHE_DIR / "qrcode.png"

# 真实 NapCat 是 Node，stdout 恒为 UTF-8；这里强制一致，否则 Python 会跟随
# 控制台代码页（GBK）输出，QLark 按 UTF-8 读就会变成乱码。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:  # pragma: no cover
    pass


def emit(text: str) -> None:
    print(text, flush=True)


# --------------------------------------------------------------------------- #
# 测试用状态
# --------------------------------------------------------------------------- #
class _State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.message_seq = 1000
        self.sent: list[dict] = []
        self.pushed: list[dict] = []
        self.push_failures: list[str] = []
        #: 图片/文件类动作（get_image / get_file），图片链路测试要看它
        self.actions: list[dict] = []

    def next_message_id(self) -> int:
        with self.lock:
            self.message_seq += 1
            return self.message_seq

    def record_sent(self, action: str, params: dict) -> None:
        with self.lock:
            self.sent.append({"action": action, "params": params, "at": time.time()})

    def record_action(self, action: str, params: dict) -> None:
        with self.lock:
            self.actions.append({"action": action, "params": params, "at": time.time()})

    def record_push(self, payload: dict, error: str | None) -> None:
        with self.lock:
            self.pushed.append({"payload": payload, "error": error, "at": time.time()})
            if error:
                self.push_failures.append(error)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "sent": list(self.sent),
                "pushed": list(self.pushed),
                "push_failures": list(self.push_failures),
                "actions": list(self.actions),
            }

    def reset(self) -> None:
        with self.lock:
            self.sent.clear()
            self.pushed.clear()
            self.push_failures.clear()
            self.actions.clear()


STATE = _State()


# --------------------------------------------------------------------------- #
# 生成一张"看起来像二维码"的合法 PNG（不依赖 Pillow）
# --------------------------------------------------------------------------- #
def _png_bytes(modules: list[list[bool]], *, scale: int = 6, quiet: int = 4) -> bytes:
    side = (len(modules) + quiet * 2) * scale
    raw = bytearray()
    for py in range(side):
        raw.append(0)  # filter type: None
        my = py // scale - quiet
        for px in range(side):
            mx = px // scale - quiet
            dark = 0 <= my < len(modules) and 0 <= mx < len(modules) and modules[my][mx]
            value = 0 if dark else 255
            raw += bytes((value, value, value))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0)  # 8bit truecolor
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


def _fake_matrix(seed: int, size: int = 25) -> list[list[bool]]:
    """伪随机黑白矩阵 + 三个定位角，视觉上像二维码。"""
    state = seed & 0xFFFFFFFF
    rows: list[list[bool]] = []
    for y in range(size):
        row: list[bool] = []
        for x in range(size):
            state = (1103515245 * state + 12345) & 0x7FFFFFFF
            row.append((state >> 16) & 1 == 1)
        rows.append(row)

    def finder(ox: int, oy: int) -> None:
        for dy in range(7):
            for dx in range(7):
                edge = dx in (0, 6) or dy in (0, 6)
                core = 2 <= dx <= 4 and 2 <= dy <= 4
                rows[oy + dy][ox + dx] = edge or core

    finder(0, 0)
    finder(size - 7, 0)
    finder(0, size - 7)
    return rows


def write_qrcode() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    seed = int(time.time() * 1000) & 0xFFFFFFFF
    QRCODE_PATH.write_bytes(_png_bytes(_fake_matrix(seed)))
    emit("[info] [NapCat] 请扫描下面的二维码，然后在手Q上授权登录：")


# --------------------------------------------------------------------------- #
# 配置读取
# --------------------------------------------------------------------------- #
def _read_onebot_config(qq: str) -> dict:
    path = CONFIG_DIR / f"onebot11_{qq}.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def network_endpoint(qq: str) -> tuple[int, str] | None:
    """有启用中的 ``httpServers`` 才开动作端口 —— 与真实 NapCat 一致。"""
    network = _read_onebot_config(qq).get("network")
    if not isinstance(network, dict):
        return None
    for entry in network.get("httpServers") or []:
        if isinstance(entry, dict) and entry.get("enable"):
            return int(entry.get("port") or PORT), str(entry.get("token") or TOKEN)
    return None


def event_endpoint(qq: str) -> tuple[str, str] | None:
    """有启用中的 ``httpClients`` 才会把事件推给 QLark。"""
    network = _read_onebot_config(qq).get("network")
    if not isinstance(network, dict):
        return None
    for entry in network.get("httpClients") or []:
        if isinstance(entry, dict) and entry.get("enable") and entry.get("url"):
            return str(entry["url"]), str(entry.get("token") or TOKEN)
    return None


def push_event(payload: dict) -> bool:
    """把一条事件 POST 给 QLark 的事件服务。

    **必须逐项照抄真身的做法**，这个函数已经因为"替身比真身好说话"漏掉过两次真实故障：

    1. 鉴权用 ``x-signature``（sha1(token + 请求体)），**不发 Authorization**；
    2. **不设 Content-Length，改用 chunked 传输** —— 真 NapCat 的 httpClient 只设了
       Content-Type / x-self-id / x-signature，Node 就自动切成 chunked 了。
       用 urllib 的 ``data=`` 会自动补上 Content-Length，那会让
       "服务端只会读 Content-Length、读不出 chunked 请求体"这类问题在测试里消失。
    """
    endpoint = event_endpoint(QQ)
    if endpoint is None:
        STATE.record_push(payload, "未配置 httpClients，事件无法推送")
        return False
    url, token = endpoint

    parsed = urllib.parse.urlsplit(url)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    conn = http.client.HTTPConnection(parsed.hostname or "127.0.0.1", parsed.port or 80, timeout=5)
    try:
        conn.putrequest("POST", path, skip_host=False, skip_accept_encoding=True)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("x-self-id", str(QQ))
        if token:
            digest = hashlib.sha1(token.encode("utf-8") + body).hexdigest()
            conn.putheader("x-signature", f"sha1={digest}")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        # 一个 chunk + 结束块；这就是 Node 在没设 Content-Length 时发的形态
        conn.send(f"{len(body):X}\r\n".encode("ascii") + body + b"\r\n0\r\n\r\n")
        response = conn.getresponse()
        status = response.status
        response.read()
        ok = 200 <= status < 300
        STATE.record_push(payload, None if ok else f"HTTP {status}")
        return ok
    except Exception as exc:
        STATE.record_push(payload, str(exc))
        return False
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 事件构造
# --------------------------------------------------------------------------- #
def build_message_event(
    *,
    message_type: str = "group",
    group_id: str = "123456",
    user_id: str = "999",
    text: str = "你好",
    at_bot: bool = True,
    segments: list[dict] | None = None,
    nickname: str = "测试用户",
    role: str = "member",
    message_id: int | None = None,
) -> dict:
    """构造一条 OneBot 11 消息事件。"""
    if segments is None:
        segments = []
        if message_type == "group" and at_bot:
            segments.append({"type": "at", "data": {"qq": QQ}})
            segments.append({"type": "text", "data": {"text": " "}})
        segments.append({"type": "text", "data": {"text": text}})

    event: dict = {
        "post_type": "message",
        "message_type": message_type,
        "sub_type": "normal",
        "message_id": message_id if message_id is not None else STATE.next_message_id(),
        "user_id": _as_number(user_id),
        "self_id": _as_number(QQ),
        "message": segments,
        "raw_message": text,
        "font": 0,
        "time": int(time.time()),
        "sender": {
            "user_id": _as_number(user_id),
            "nickname": nickname,
            "card": "",
            "role": role,
        },
    }
    if message_type == "group":
        event["group_id"] = _as_number(group_id)
    return event


def _as_number(value: str) -> int | str:
    """OneBot 里 QQ/群号通常是数字，但也要容忍字符串。"""
    text = str(value)
    return int(text) if text.isdigit() else text


# --------------------------------------------------------------------------- #
# OneBot 动作端口
# --------------------------------------------------------------------------- #
def sample_image_bytes() -> bytes:
    """一张真实的 PNG（带颜色渐变，保证不是一坨纯色），供图片链路测试下载。

    用 Pillow 生成而不是硬编码 base64：这样测的是"真的能解码一张图"，
    而不是"能不能解开我们自己的常量"。
    """
    global _SAMPLE_CACHE
    if _SAMPLE_CACHE is not None:
        return _SAMPLE_CACHE
    try:
        import io

        from PIL import Image

        image = Image.new("RGB", (1200, 800))  # 故意比 1024 大，用来验证会缩放
        pixels = image.load()
        for y in range(0, 800, 4):
            for x in range(0, 1200, 4):
                color = (x % 256, y % 256, (x + y) % 256)
                for dy in range(4):
                    for dx in range(4):
                        if x + dx < 1200 and y + dy < 800:
                            pixels[x + dx, y + dy] = color
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        _SAMPLE_CACHE = buffer.getvalue()
    except Exception:  # pragma: no cover - 没有 Pillow 时退回一个能解码的最小 PNG
        _SAMPLE_CACHE = _png_bytes([[1] * 8 for _ in range(8)])
    return _SAMPLE_CACHE


_SAMPLE_CACHE: bytes | None = None


def make_handler(qq: str, token: str, port: int = 0) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:  # 静音，避免污染 QLark 日志
            return

        # ---------------------------------------------------------- 工具
        def _respond(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8", errors="replace"))
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}

        # ---------------------------------------------------------- 路由
        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/_qlark/sent":
                self._respond({"ok": True, **STATE.snapshot()})
                return
            if path == "/_qlark/health":
                self._respond({"ok": True, "qq": qq})
                return
            if path == "/_qlark/sample.png":
                # 供图片链路测试下载：一张真实的 PNG（不是假数据）
                payload = sample_image_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if path == "/_qlark/slow.png":
                # 用来测超时保护：先睡一会儿再回
                time.sleep(float(os.environ.get("FAKE_NAPCAT_SLOW_SECONDS", "5")))
                payload = sample_image_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self._respond({"status": "failed", "retcode": 1404, "message": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path.startswith("/_qlark/"):
                self._handle_control(path)
                return
            self._handle_action(path.strip("/"))

        # ---------------------------------------------------------- 控制接口
        def _handle_control(self, path: str) -> None:
            body = self._read_body()
            if path == "/_qlark/inject":
                event = build_message_event(
                    message_type=str(body.get("message_type") or "group"),
                    group_id=str(body.get("group_id") or "123456"),
                    user_id=str(body.get("user_id") or "999"),
                    text=str(body.get("text") or "你好"),
                    at_bot=bool(body.get("at_bot", True)),
                    segments=body.get("segments"),
                    nickname=str(body.get("nickname") or "测试用户"),
                    role=str(body.get("role") or "member"),
                    message_id=body.get("message_id"),
                )
                pushed = push_event(event)
                emit(f"[info] [Fake] 注入消息 event_id={event['message_id']} pushed={pushed}")
                self._respond({"ok": True, "pushed": pushed, "event": event})
                return
            if path == "/_qlark/reset":
                STATE.reset()
                self._respond({"ok": True})
                return
            self._respond({"ok": False, "error": "unknown control endpoint"}, status=404)

        # ---------------------------------------------------------- OneBot 动作
        def _handle_action(self, action: str) -> None:
            params = self._read_body()

            auth = self.headers.get("Authorization", "")
            if token and auth != f"Bearer {token}":
                self._respond(
                    {"status": "failed", "retcode": 1403, "message": "token verify failed!"}
                )
                return

            if action == "get_login_info":
                self._respond(_ok({"user_id": _as_number(qq), "nickname": "假账号"}))
            elif action == "get_status":
                self._respond(_ok({"online": True, "good": True, "stat": {}}))
            elif action == "get_version_info":
                self._respond(
                    _ok(
                        {
                            "app_name": "NapCat.Onebot",
                            "protocol_version": "v11",
                            "app_version": "4.18.28-fake",
                        }
                    )
                )
            elif action in ("send_group_msg", "send_private_msg", "send_msg"):
                STATE.record_sent(action, params)
                emit(f"[info] [Fake] 收到发送请求 {action}")
                self._respond(_ok({"message_id": STATE.next_message_id()}))
            elif action in ("get_image", "get_file"):
                # 真 NapCat 支持它：图片段只有 file 没有 url 时，客户端靠这个接口
                # 换到一个可下载的地址。随便编一个地址会让"立即下载"这条路测不到，
                # 所以这里返回本假服务真实提供的一张图。
                requested = str(params.get("file") or "")
                STATE.record_action(action, params)
                emit(f"[info] [Fake] {action} file={requested!r} -> /_qlark/sample.png")
                self._respond(
                    _ok(
                        {
                            "file": requested,
                            "url": f"http://127.0.0.1:{port}/_qlark/sample.png",
                            "file_size": len(sample_image_bytes()),
                        }
                    )
                )
            else:
                self._respond(
                    {"status": "failed", "retcode": 1404, "message": f"unknown action {action}"}
                )

    def _ok(data: dict) -> dict:
        return {"status": "ok", "retcode": 0, "data": data, "stream": "normal-action"}

    return Handler


def serve_forever(port: int, qq: str, token: str) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(qq, token, port))
    emit(f"[info] [OneBot] [HTTP Server Adapter] Start On 127.0.0.1:{port}")
    server.serve_forever()


# --------------------------------------------------------------------------- #
# 登录
# --------------------------------------------------------------------------- #
def mark_logged_in(qq: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # 真实 NapCat 只在账号首次注册时写 napcat_<QQ>.json
    account_path = CONFIG_DIR / f"napcat_{qq}.json"
    if ACCOUNT_SIGNAL == "always" or (ACCOUNT_SIGNAL == "once" and not account_path.exists()):
        account_path.write_text(
            json.dumps({"fake": True, "qq": qq}, ensure_ascii=False), encoding="utf-8"
        )

    # 真实 NapCat 每次登录都会打印这行 —— 里面带着 QQ 号，
    # 是 QLark 在扫码模式下识别"登录的是哪个号"最省事的来源
    emit(
        f"[info] [Core] [Config] 配置文件{account_path}加载 "
        '{"fileLog":false,"consoleLog":true,"fileLogLevel":"debug"}'
    )

    # 而 napcat_protocol_<QQ>.json 每次登录都会重新生成
    if PROTOCOL_SIGNAL == "always":
        (CONFIG_DIR / f"napcat_protocol_{qq}.json").write_text(
            json.dumps({"fake": True, "qq": qq, "protocol": "onebot11"}, ensure_ascii=False),
            encoding="utf-8",
        )

    # 首次登录时才生成网络配置（已存在就不动，这样才能验证"已就绪 → 不重启"）
    onebot_path = CONFIG_DIR / f"onebot11_{qq}.json"
    if onebot_path.exists():
        return
    onebot_path.write_text(
        json.dumps(
            {
                "network": {
                    "httpServers": [],
                    "httpSseServers": [],
                    "httpClients": [],
                    "websocketServers": [],
                    "websocketClients": [],
                    "plugins": [],
                },
                "musicSignUrl": "",
                "timeout": {"baseTimeout": 10000},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def qr_refresh_loop(stop: threading.Event) -> None:
    while not stop.wait(QR_INTERVAL):
        write_qrcode()


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fake_napcat")
    parser.add_argument("-q", "--quick", dest="qq", default=None)
    args = parser.parse_args(argv)

    emit("[info] [NapCat.Core] Version: 4.18.28-fake")
    stop = threading.Event()

    quick_ok = bool(args.qq) and HAVE_CRED
    if quick_ok:
        emit(f"[info] 正在快速登录  {args.qq}")
        emit("[info] [NapCat] [Fork] Worker进程已登录成功，切换到正常重试策略")
        login_qq = str(args.qq)
    else:
        emit("[info] 没有 -q 指令指定快速登录，将使用二维码登录方式")
        write_qrcode()
        threading.Thread(target=qr_refresh_loop, args=(stop,), daemon=True).start()
        login_qq = str(args.qq) if args.qq else QQ
        time.sleep(SCAN_DELAY)
        emit("[info] [NapCat] [Fork] Worker进程已登录成功，切换到正常重试策略")
        stop.set()

    mark_logged_in(login_qq)

    # 只有该账号配置了启用中的 OneBot 网络，端口才会开 —— 与真实 NapCat 一致
    endpoint = network_endpoint(login_qq)
    if endpoint:
        port, token = endpoint
        threading.Thread(
            target=serve_forever, args=(port, login_qq, token), daemon=True
        ).start()
    else:
        emit("[info] [OneBot] 该账号未配置 OneBot 网络，端口未开放")

    if event_endpoint(login_qq) is None:
        emit("[info] [OneBot] 该账号未配置事件推送（httpClients），QLark 收不到消息")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
