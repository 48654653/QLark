"""pytest 公共夹具。

测试不依赖真实的 NapCat 运行时（约 300MB，且需要 QQ 账号）：用
``tools/fake_napcat.py`` 复刻 NapCat 的可观测行为，把状态机完整跑通。
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
FAKE_NAPCAT = ROOT / "tools" / "fake_napcat.py"
FAKE_OPENAI = ROOT / "tools" / "fake_openai.py"

NAPCAT_STUB_FILES = (
    "node.exe",
    "index.js",
    "wrapper.node",
    "package.json",
    "config.json",
    "crypto.dll",
    "ssl.dll",
)

TEST_TOKEN = "test-token"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def make_napcat_tree(root: Path) -> Path:
    """搭出一个结构完整的假 NapCat 目录。"""
    napcat = root / "runtime" / "napcat"
    (napcat / "napcat" / "config").mkdir(parents=True, exist_ok=True)
    (napcat / "napcat" / "cache").mkdir(parents=True, exist_ok=True)
    for name in NAPCAT_STUB_FILES:
        (napcat / name).write_bytes(b"stub")
    return napcat


@pytest.fixture
def qlark_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """隔离的运行环境：独立的 NapCat 目录、数据目录与端口。"""
    napcat = make_napcat_tree(tmp_path)
    data = tmp_path / "data"
    port = free_port()
    event_port = free_port()

    monkeypatch.setenv("QLARK_NAPCAT_DIR", str(napcat))
    monkeypatch.setenv("QLARK_DATA_DIR", str(data))
    monkeypatch.setenv("QLARK_NAPCAT_EXE", sys.executable)
    monkeypatch.setenv("QLARK_NAPCAT_ENTRY", str(FAKE_NAPCAT))
    monkeypatch.setenv("QLARK_SKIP_ENV_CHECK", "1")

    monkeypatch.setenv("FAKE_NAPCAT_PORT", str(port))
    monkeypatch.setenv("FAKE_NAPCAT_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("FAKE_NAPCAT_QQ", "2157649443")
    monkeypatch.setenv("FAKE_NAPCAT_QR_INTERVAL", "1")
    monkeypatch.setenv("FAKE_NAPCAT_SCAN_DELAY", "1")
    monkeypatch.delenv("FAKE_NAPCAT_HAVE_CRED", raising=False)
    monkeypatch.delenv("FAKE_NAPCAT_ACCOUNT_SIGNAL", raising=False)
    monkeypatch.delenv("FAKE_NAPCAT_PROTOCOL_SIGNAL", raising=False)
    # 生产上要等 5 秒让登录状态落盘；测试里压缩到 0.2 秒
    monkeypatch.setenv("QLARK_LOGIN_SETTLE", "0.2")

    return SimpleNamespace(
        root=tmp_path,
        napcat=napcat,
        config_dir=napcat / "napcat" / "config",
        qrcode=napcat / "napcat" / "cache" / "qrcode.png",
        data=data,
        port=port,
        event_port=event_port,
        token=TEST_TOKEN,
        qq="2157649443",
    )


def build_manager(home: SimpleNamespace):
    """构造一套不依赖 GUI 的 LoginManager + BotService。"""
    from qlark.bot import BotService
    from qlark.napcat.logbus import LogBus
    from qlark.napcat.login import LoginManager
    from qlark.settings import Settings
    from qlark.storage.db import Database

    db = Database(home.data / "qlark.db")
    settings = Settings(db)
    settings.update(
        onebot_port=home.port,
        onebot_token=home.token,
        event_host="127.0.0.1",
        event_port=home.event_port,
    )
    logbus = LogBus(capacity=4000, log_dir=home.data / "logs", persist=True)

    bot = BotService(settings, db, logbus)
    manager = LoginManager(
        settings,
        logbus,
        on_ready=bot.start,
        on_stopped=lambda: bot.stop(wait=False),
    )
    return SimpleNamespace(
        manager=manager,
        bot=bot,
        settings=settings,
        logbus=logbus,
        db=db,
        home=home,
        starts=lambda: sum(
            1 for line in logbus.tail(4000) if "启动 NapCat：" in line.text
        ),
    )


@pytest.fixture
def env(qlark_home: SimpleNamespace):
    """给出一个已装配好的 LoginManager + BotService，测试结束自动清理。"""
    bundle = build_manager(qlark_home)
    try:
        yield bundle
    finally:
        try:
            bundle.bot.stop(wait=False)
        finally:
            try:
                bundle.manager.stop(wait=True)
            finally:
                bundle.logbus.close()
                bundle.db.close()


# --------------------------------------------------------------------------- #
# 假 OpenAI 服务
# --------------------------------------------------------------------------- #
class FakeOpenAI:
    """驱动 ``tools/fake_openai.py`` 的测试夹具。"""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def reset(self) -> None:
        _post(f"{self.base_url}/_qlark/reset")

    def requests(self) -> list[dict]:
        return _get(f"{self.base_url}/_qlark/requests").get("requests", [])

    def fail_next(self, times: int, status: int = 500, message: str = "boom") -> None:
        _post(
            f"{self.base_url}/_qlark/fail",
            {"times": times, "status": status, "message": message},
        )

    def ok(self) -> None:
        _post(f"{self.base_url}/_qlark/ok")

    def health(self) -> dict:
        return _get(f"{self.base_url}/_qlark/health")


@pytest.fixture
def fake_openai(tmp_path: Path):
    """启动一个假 OpenAI 兼容服务，测试结束杀掉。"""
    import subprocess

    port = free_port()
    log_path = tmp_path / "fake_openai.log"
    handle = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(FAKE_OPENAI), "--port", str(port)],
        stdout=handle,
        stderr=subprocess.STDOUT,
        cwd=str(ROOT),
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_http(f"{base_url}/_qlark/health", timeout=20)
        yield FakeOpenAI(base_url)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        handle.close()


def _local_opener():
    """访问本机服务的 urllib opener：**强制绕开系统代理**。

    Windows 的 ProxyOverride 白名单（含 127.*）在 Python 里不生效，不显式禁用的话
    测试会走用户的代理，既慢又依赖代理是否在跑（实测踩过）。
    """
    import urllib.request

    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _wait_http(url: str, timeout: float = 20.0) -> None:
    import time as _time
    import urllib.error

    opener = _local_opener()
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        try:
            with opener.open(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            _time.sleep(0.15)
    raise RuntimeError(f"服务未在 {timeout:.0f}s 内就绪：{url}")


def _with_retry(call, *, attempts: int = 4, delay: float = 0.2):
    """重试**瞬时连接错误**。返回 ``call()`` 的结果，重试用尽则抛最后一个异常。

    为什么需要：集成测试会同时开着好几个本地 HTTP 服务（假 NapCat、假 OpenAI、
    事件服务），满负载时 Windows 偶发 ``ConnectionAbortedError 10053`` ——
    实测大约三次全量跑就会碰到一次，而且每次挂的用例都不一样。
    那是环境抖动（本机 TCP 层还有已知异常：SYN 被丢弃、代理劫持回环），
    不是被测行为。把它当失败会让整个套件变得不可信。

    对 POST 也重试是安全的：控制面接口要么幂等（``/_qlark/reset``），
    要么重复投递会被消息去重挡掉（``/_qlark/inject`` 用 ``session#message_id`` 去重）。
    """
    import http.client
    import time as _time
    import urllib.error

    last: Exception | None = None
    for index in range(max(1, attempts)):
        try:
            return call()
        except (
            ConnectionError,
            http.client.HTTPException,
            urllib.error.URLError,
            OSError,
        ) as exc:
            last = exc
            if index + 1 < attempts:
                _time.sleep(delay * (index + 1))
    assert last is not None
    raise last


def _get(url: str) -> dict:
    import json as _json

    def once() -> dict:
        with _local_opener().open(url, timeout=10) as response:
            return _json.loads(response.read().decode("utf-8"))

    return _with_retry(once)


def _post(url: str, payload: dict | None = None) -> dict:
    import json as _json
    import urllib.request

    body = _json.dumps(payload or {}).encode("utf-8")

    def once() -> dict:
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        with _local_opener().open(request, timeout=10) as response:
            raw = response.read().decode("utf-8")
        return _json.loads(raw) if raw else {}

    return _with_retry(once)


def wait_for(predicate, timeout: float = 30.0, interval: float = 0.1) -> bool:
    """轮询等待条件成立。"""
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if predicate():
            return True
        _time.sleep(interval)
    return bool(predicate())
