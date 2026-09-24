"""启动 / 登录状态机的端到端测试（使用假 NapCat）。"""

from __future__ import annotations

import json
import os
import time

from qlark.core.models import Phase
from qlark.napcat import netconfig

from conftest import build_manager


def _load_onebot(home) -> dict:
    path = netconfig.onebot_config_path(home.qq, home.napcat)
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 快速登录
# --------------------------------------------------------------------------- #
def test_quick_login_then_ready(env, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_NAPCAT_HAVE_CRED", "1")
    env.settings.qq_account = env.home.qq

    env.manager.start()
    assert env.manager.wait_until(lambda: env.manager.phase is Phase.READY, timeout=90), (
        f"未能进入 READY，当前阶段={env.manager.phase} 错误={env.manager.last_error}"
    )

    snap = env.manager.snapshot()
    assert snap["qq"] == env.home.qq
    assert snap["online"] is True
    assert snap["nickname"] == "假账号"
    assert snap["napcat_running"] is True
    assert snap["pid"]
    assert snap["qrcode"] is None, "快速登录路径不应该产生二维码"


def test_quick_login_writes_onebot_config_and_restarts(env, monkeypatch) -> None:
    """协议端默认不开端口：写完配置必须重启一次 NapCat 才生效。"""
    monkeypatch.setenv("FAKE_NAPCAT_HAVE_CRED", "1")
    env.settings.qq_account = env.home.qq

    # 预置 NapCat 自己会生成的配置（网络为空，但带它自己的字段），
    # 用来验证我们是"合并写入"而不是整体覆盖
    path = netconfig.onebot_config_path(env.home.qq, env.home.napcat)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
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
                "musicSignUrl": "https://example.com/music",
                "timeout": {"baseTimeout": 12345},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    env.manager.start()
    assert env.manager.wait_until(lambda: env.manager.phase is Phase.READY, timeout=90), (
        f"未进入 READY，阶段={env.manager.phase} 错误={env.manager.last_error}"
    )

    cfg = _load_onebot(env.home)
    assert netconfig.network_is_configured(cfg) is True
    server = cfg["network"]["httpServers"][0]
    assert server["port"] == env.home.port
    assert server["token"] == env.home.token
    assert server["messagePostFormat"] == "array"
    assert env.starts() >= 2, "写过网络配置后应当重启一次"

    # 写配置前必须留出落盘时间：实测登录成功 0.6 秒后就硬杀，紧接着的快速登录
    # 会被 QQ 要求"手Q验证"，用户被迫二次扫码
    assert any("落盘登录状态" in line.text for line in env.logbus.tail(4000)), (
        "写配置前缺少落盘等待"
    )

    # NapCat 自己的字段不能被覆盖
    assert cfg["musicSignUrl"] == "https://example.com/music"
    assert cfg["timeout"]["baseTimeout"] == 12345


def test_no_restart_when_network_already_configured(env, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_NAPCAT_HAVE_CRED", "1")
    env.settings.qq_account = env.home.qq
    netconfig.ensure_network_config(
        env.home.qq,
        host="127.0.0.1",
        port=env.home.port,
        event_host="127.0.0.1",
        event_port=env.home.event_port,
        token=env.home.token,
        napcat_dir=env.home.napcat,
    )
    env.manager.start()
    assert env.manager.wait_until(lambda: env.manager.phase is Phase.READY, timeout=90)
    assert env.starts() == 1, "配置已就绪时不应重启 NapCat"


def test_m1_style_config_is_upgraded_with_event_channel(env, monkeypatch) -> None:
    """回归：M1 时代只写了 ``httpServers``。

    升级到 M2 后必须自动补上 ``httpClients``（事件通道）并重启一次 ——
    否则 NapCat 不会把消息推给 QLark，机器人"在线但永远不回复"。
    """
    monkeypatch.setenv("FAKE_NAPCAT_HAVE_CRED", "1")
    env.settings.qq_account = env.home.qq

    # 造一份只有动作通道的旧配置（就是 M1 写出来的样子）
    path = netconfig.onebot_config_path(env.home.qq, env.home.napcat)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "network": {
                    "httpServers": [
                        netconfig.build_server_entry(
                            "127.0.0.1", env.home.port, env.home.token
                        )
                    ],
                    "httpSseServers": [],
                    "httpClients": [],
                    "websocketServers": [],
                    "websocketClients": [],
                    "plugins": [],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    env.manager.start()
    assert env.manager.wait_until(lambda: env.manager.phase is Phase.READY, timeout=90), (
        f"未进入 READY，阶段={env.manager.phase} 错误={env.manager.last_error}"
    )

    cfg = _load_onebot(env.home)
    clients = cfg["network"]["httpClients"]
    assert clients, "没有补上事件通道（httpClients）"
    assert clients[0]["enable"] is True
    assert clients[0]["url"].endswith(f":{env.home.event_port}")
    assert clients[0]["messagePostFormat"] == "array"
    assert env.starts() >= 2, "补写事件通道后应当重启一次 NapCat"


# --------------------------------------------------------------------------- #
# 扫码登录
# --------------------------------------------------------------------------- #
def test_scan_login_produces_qrcode_and_ready(env) -> None:
    assert env.settings.qq_account is None, "前置条件：没有已保存账号"

    env.manager.start()

    assert env.manager.wait_until(lambda: env.manager.qrcode is not None, timeout=60), (
        "扫码模式下应当能看到二维码"
    )
    snapshot = env.manager.qrcode
    assert snapshot is not None
    assert snapshot.data_url.startswith("data:image/png;base64,")

    assert env.manager.wait_until(lambda: env.manager.phase is Phase.READY, timeout=120), (
        f"未能进入 READY，当前阶段={env.manager.phase} 错误={env.manager.last_error}"
    )
    # 扫码成功后必须把账号持久化到客户端自己的配置里
    assert env.settings.qq_account == env.home.qq


def _preexisting_account_file(home, qq: str, *, age: float = 3600.0):
    """模拟"这个账号以前登录过"：``napcat_<QQ>.json`` 已存在且不会被重写。"""
    home.config_dir.mkdir(parents=True, exist_ok=True)
    path = home.config_dir / f"napcat_{qq}.json"
    path.write_text('{"fake": true}', encoding="utf-8")
    old = time.time() - age
    os.utime(path, (old, old))
    return path


def test_scan_login_when_account_file_already_exists(env) -> None:
    """回归：``napcat_<QQ>.json`` 只在账号**首次注册**时写入。

    实测证据：第 1 次扫码登录时该文件被创建；第 2 次扫码登录时它纹丝不动
    （mtime 未变）。只看"文件是否新增/被改写"会把成功判成失败，
    用户会看到"登录信号已收到，但未找到账号配置文件"。
    """
    _preexisting_account_file(env.home, env.home.qq)
    assert env.settings.qq_account is None

    env.manager.start()
    assert env.manager.wait_until(lambda: env.manager.phase is Phase.READY, timeout=120), (
        f"未进入 READY，阶段={env.manager.phase} 错误={env.manager.last_error}"
    )
    assert env.settings.qq_account == env.home.qq


def test_scan_login_detected_via_onebot_probe(env, monkeypatch) -> None:
    """连协议文件都不变化时，仍要能通过 OneBot 接口问出登录的是哪个账号。"""
    monkeypatch.setenv("FAKE_NAPCAT_ACCOUNT_SIGNAL", "never")
    monkeypatch.setenv("FAKE_NAPCAT_PROTOCOL_SIGNAL", "never")

    # 预置网络配置（相当于该账号上次会话已经配好）→ 登录后端口会起来，接口可问
    netconfig.ensure_network_config(
        env.home.qq,
        host="127.0.0.1",
        port=env.home.port,
        event_host="127.0.0.1",
        event_port=env.home.event_port,
        token=env.home.token,
        napcat_dir=env.home.napcat,
    )

    env.manager.start()
    assert env.manager.wait_until(lambda: env.manager.phase is Phase.READY, timeout=120), (
        f"未进入 READY，阶段={env.manager.phase} 错误={env.manager.last_error}"
    )
    assert env.settings.qq_account == env.home.qq


def test_stale_qrcode_is_not_shown(env) -> None:
    """坑 21：登录成功后残留的二维码不能被当成可用二维码显示。"""
    env.home.qrcode.parent.mkdir(parents=True, exist_ok=True)
    env.home.qrcode.write_bytes(b"\x89PNG\r\n\x1a\n" + b"stale")
    stale_mtime = time.time() - 3600
    os.utime(env.home.qrcode, (stale_mtime, stale_mtime))

    env.manager.start()
    assert env.manager.wait_until(lambda: env.manager.qrcode is not None, timeout=60)
    snapshot = env.manager.qrcode
    assert snapshot is not None
    assert snapshot.mtime > stale_mtime + 60, "显示的必须是本次新生成的二维码"


# --------------------------------------------------------------------------- #
# 停止与清理
# --------------------------------------------------------------------------- #
def test_stop_terminates_process(env, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_NAPCAT_HAVE_CRED", "1")
    env.settings.qq_account = env.home.qq
    env.manager.start()
    assert env.manager.wait_until(lambda: env.manager.phase is Phase.READY, timeout=90)

    pid = env.manager.snapshot()["pid"]
    assert pid

    env.manager.stop(wait=True)
    assert env.manager.phase is Phase.STOPPED
    assert env.manager.snapshot()["napcat_running"] is False
    assert env.manager.wait_until(lambda: not _pid_alive(pid), timeout=15)


def test_relogin_clears_saved_account(env, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_NAPCAT_HAVE_CRED", "1")
    env.settings.qq_account = env.home.qq
    env.manager.start()
    assert env.manager.wait_until(lambda: env.manager.phase is Phase.READY, timeout=90)

    env.manager.relogin()
    assert env.manager.wait_until(lambda: env.manager.qrcode is not None, timeout=60), (
        "重新登录应当强制走扫码"
    )
    env.manager.stop(wait=True)


# --------------------------------------------------------------------------- #
# 环境门禁
# --------------------------------------------------------------------------- #
def test_env_gate_blocks_startup_when_napcat_missing(qlark_home, monkeypatch) -> None:
    monkeypatch.delenv("QLARK_SKIP_ENV_CHECK", raising=False)
    monkeypatch.setenv("QLARK_NAPCAT_DIR", str(qlark_home.root / "not-there"))

    bundle = build_manager(qlark_home)
    try:
        bundle.manager.start()
        assert bundle.manager.wait_until(
            lambda: bundle.manager.phase is Phase.ENV_ERROR, timeout=30
        )
        assert bundle.manager.snapshot()["napcat_running"] is False
        assert "环境自检未通过" in (bundle.manager.last_error or "")
    finally:
        bundle.manager.stop(wait=True)
        bundle.logbus.close()
        bundle.db.close()


def _pid_alive(pid: int | None) -> bool:
    from qlark.napcat.process import pid_alive

    return pid_alive(pid)
