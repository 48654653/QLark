"""OneBot 配置合并写入的单元测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qlark.napcat import netconfig


def test_strip_json5_removes_comments_and_trailing_commas() -> None:
    raw = """
    {
      // 这是注释
      "a": 1, /* 块注释 */
      "url": "https://example.com//not-a-comment",
      "b": [1, 2, 3,],
    }
    """
    parsed = json.loads(netconfig.strip_json5(raw))
    assert parsed["a"] == 1
    # 字符串里的 // 不能被当成注释删掉
    assert parsed["url"] == "https://example.com//not-a-comment"
    assert parsed["b"] == [1, 2, 3]


def test_strip_json5_handles_bom_and_escapes() -> None:
    raw = '\ufeff{"a": "引号\\"里的//", "b": 2,}'
    parsed = json.loads(netconfig.strip_json5(raw))
    assert parsed["a"] == '引号"里的//'
    assert parsed["b"] == 2


def test_network_is_configured_detects_enabled_entries() -> None:
    assert netconfig.network_is_configured({}) is False
    assert netconfig.network_is_configured({"network": netconfig.EMPTY_NETWORK}) is False
    assert (
        netconfig.network_is_configured(
            {"network": {"httpServers": [{"name": "x", "enable": False}]}}
        )
        is False
    )
    assert (
        netconfig.network_is_configured(
            {"network": {"websocketClients": [{"name": "x", "enable": True}]}}
        )
        is True
    )


def test_ensure_network_config_creates_when_missing(tmp_path: Path) -> None:
    result = netconfig.ensure_network_config(
        "123456", host="127.0.0.1", port=34567, token="tk", napcat_dir=tmp_path
    )
    assert result.changed is True
    assert result.created is True
    assert result.path.is_file()

    cfg = json.loads(result.path.read_text(encoding="utf-8"))
    servers = cfg["network"]["httpServers"]
    assert len(servers) == 1
    assert servers[0]["name"] == netconfig.SERVER_NAME
    assert servers[0]["port"] == 34567
    assert servers[0]["token"] == "tk"
    assert servers[0]["messagePostFormat"] == "array"


def test_ensure_network_config_is_idempotent(tmp_path: Path) -> None:
    first = netconfig.ensure_network_config(
        "123456", host="127.0.0.1", port=34567, token="tk", napcat_dir=tmp_path
    )
    assert first.changed is True

    second = netconfig.ensure_network_config(
        "123456", host="127.0.0.1", port=34567, token="tk", napcat_dir=tmp_path
    )
    assert second.changed is False, "第二次不应重复写入"


def test_ensure_network_config_merges_and_preserves_other_fields(tmp_path: Path) -> None:
    """必须合并写入，不能整体覆盖 —— 用户自己的配置不能被丢掉。"""
    path = netconfig.onebot_config_path("123456", tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "network": {"httpServers": [], "websocketClients": [{"name": "用户自己的"}]},
                "musicSignUrl": "https://example.com/music",
                "timeout": {"baseTimeout": 12345},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = netconfig.ensure_network_config(
        "123456", host="127.0.0.1", port=34567, token="tk", napcat_dir=tmp_path
    )
    assert result.changed is True

    cfg = json.loads(path.read_text(encoding="utf-8"))
    assert cfg["musicSignUrl"] == "https://example.com/music"
    assert cfg["timeout"]["baseTimeout"] == 12345
    assert cfg["network"]["websocketClients"] == [{"name": "用户自己的"}]
    assert len(cfg["network"]["httpServers"]) == 1


def test_ensure_network_config_backs_up_before_write(tmp_path: Path) -> None:
    path = netconfig.onebot_config_path("123456", tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"network": {"httpServers": []}}', encoding="utf-8")

    result = netconfig.ensure_network_config(
        "123456", host="127.0.0.1", port=34567, token="tk", napcat_dir=tmp_path
    )
    assert result.backup is not None and result.backup.is_file()
    assert "httpServers" in result.backup.read_text(encoding="utf-8")


def test_ensure_network_config_updates_port_change(tmp_path: Path) -> None:
    netconfig.ensure_network_config(
        "123456", host="127.0.0.1", port=3000, token="tk", napcat_dir=tmp_path
    )
    result = netconfig.ensure_network_config(
        "123456", host="127.0.0.1", port=4000, token="tk", napcat_dir=tmp_path
    )
    assert result.changed is True
    cfg = json.loads(result.path.read_text(encoding="utf-8"))
    assert cfg["network"]["httpServers"][0]["port"] == 4000
    assert len(cfg["network"]["httpServers"]) == 1, "端口变更应更新而不是追加"


def test_dry_run_does_not_write(tmp_path: Path) -> None:
    result = netconfig.ensure_network_config(
        "123456", host="127.0.0.1", port=34567, token="tk", napcat_dir=tmp_path, write=False
    )
    assert result.changed is True
    assert not result.path.exists()


def test_detect_accounts(tmp_path: Path) -> None:
    config_dir = tmp_path / "napcat" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "napcat_123456.json").write_text("{}", encoding="utf-8")
    (config_dir / "napcat_654321.json").write_text("{}", encoding="utf-8")
    (config_dir / "onebot11_111111.json").write_text("{}", encoding="utf-8")
    (config_dir / "napcat.json").write_text("{}", encoding="utf-8")
    assert netconfig.detect_accounts(tmp_path) == ["123456", "654321"]


def test_parse_account_stem() -> None:
    assert netconfig.parse_account_stem("napcat_2157649443") == "2157649443"
    assert netconfig.parse_account_stem("napcat_protocol_2157649443") == "2157649443"
    # napcat.json 是基础设置，必须排除
    assert netconfig.parse_account_stem("napcat") is None
    assert netconfig.parse_account_stem("onebot11_2157649443") is None
    assert netconfig.parse_account_stem("napcat_abc") is None
    assert netconfig.parse_account_stem("napcat_protocol") is None


def test_detect_accounts_uses_protocol_file_changes(tmp_path: Path) -> None:
    """``napcat_<QQ>.json`` 不再变化时，靠 ``napcat_protocol_<QQ>.json`` 仍能识别本次登录。"""
    import time

    config_dir = tmp_path / "napcat" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "napcat_123456.json").write_text("{}", encoding="utf-8")
    (config_dir / "napcat_protocol_123456.json").write_text("{}", encoding="utf-8")

    baseline = netconfig.account_file_mtimes(tmp_path)
    assert baseline.get("123456") is not None
    assert netconfig.detect_accounts(tmp_path, baseline=baseline) == []

    time.sleep(0.05)
    # 只有协议文件被重新生成
    (config_dir / "napcat_protocol_123456.json").write_text('{"again": 1}', encoding="utf-8")
    assert netconfig.detect_accounts(tmp_path, baseline=baseline) == ["123456"]


def test_detect_accounts_ignores_files_from_previous_runs(tmp_path: Path) -> None:
    """上次登录残留的配置文件不能让本次扫码流程误判为已登录。"""
    import os
    import time

    config_dir = tmp_path / "napcat" / "config"
    config_dir.mkdir(parents=True)
    stale = config_dir / "napcat_123456.json"
    stale.write_text("{}", encoding="utf-8")
    old = time.time() - 3600
    os.utime(stale, (old, old))

    baseline = netconfig.account_file_mtimes(tmp_path)
    assert netconfig.detect_accounts(tmp_path, baseline=baseline) == []

    # since 兜底判据同理
    assert netconfig.detect_accounts(tmp_path, since=time.time()) == []

    # 本次登录新写的文件应当被识别
    fresh = config_dir / "napcat_654321.json"
    fresh.write_text("{}", encoding="utf-8")
    assert netconfig.detect_accounts(tmp_path, baseline=baseline) == ["654321"]


def test_detect_accounts_detects_rewritten_file(tmp_path: Path) -> None:
    """同一个账号被重新登录（文件被改写）也必须算作"本次登录成功"。"""
    import time

    config_dir = tmp_path / "napcat" / "config"
    config_dir.mkdir(parents=True)
    path = config_dir / "napcat_123456.json"
    path.write_text("{}", encoding="utf-8")

    baseline = netconfig.account_file_mtimes(tmp_path)
    time.sleep(0.05)
    path.write_text('{"again": true}', encoding="utf-8")

    assert netconfig.detect_accounts(tmp_path, baseline=baseline) == ["123456"]


def test_atomic_write_retries_on_sharing_violation(tmp_path, monkeypatch) -> None:
    """Windows 上目标文件被别的进程短暂占用时，写入必须重试而不是直接失败。"""
    import os as _os

    path = tmp_path / "onebot11_123456.json"
    path.write_text("{}", encoding="utf-8")

    real_replace = _os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(5, "拒绝访问。")
        return real_replace(src, dst)

    monkeypatch.setattr(netconfig.os, "replace", flaky_replace)
    netconfig._atomic_write_json(path, {"ok": True})

    assert calls["n"] == 3, "前两次失败后应当重试成功"
    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}
    assert not path.with_suffix(path.suffix + ".tmp").exists()
