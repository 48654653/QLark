"""环境自检测试。"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from qlark.core.models import EnvItem, EnvReport
from qlark.napcat import envcheck

from conftest import free_port, make_napcat_tree


def test_port_is_free_reflects_reality() -> None:
    port = free_port()
    assert envcheck.port_is_free("127.0.0.1", port) is True

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
        assert envcheck.port_is_free("127.0.0.1", port) is False


def test_check_napcat_runtime_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    napcat = make_napcat_tree(tmp_path)
    monkeypatch.setenv("QLARK_NAPCAT_DIR", str(napcat))
    item = envcheck.check_napcat_runtime()
    assert item.ok is True


def test_check_napcat_runtime_missing_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QLARK_NAPCAT_DIR", str(tmp_path / "nope"))
    item = envcheck.check_napcat_runtime()
    assert item.ok is False
    assert item.fixable is False


def test_check_napcat_runtime_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    napcat = make_napcat_tree(tmp_path)
    (napcat / "wrapper.node").unlink()
    monkeypatch.setenv("QLARK_NAPCAT_DIR", str(napcat))
    item = envcheck.check_napcat_runtime()
    assert item.ok is False
    assert "wrapper.node" in item.detail


def test_check_napcat_dlls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    napcat = make_napcat_tree(tmp_path)
    monkeypatch.setenv("QLARK_NAPCAT_DIR", str(napcat))
    assert envcheck.check_napcat_dlls().ok is True

    (napcat / "crypto.dll").unlink()
    item = envcheck.check_napcat_dlls()
    assert item.ok is False
    assert item.fixable is True
    assert "crypto.dll" in item.detail


def test_check_ports_detects_busy_port() -> None:
    port = free_port()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
        item = envcheck.check_ports("127.0.0.1", port)
    assert item.ok is False
    assert str(port) in item.detail


def test_check_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QLARK_DATA_DIR", str(tmp_path / "data"))
    assert envcheck.check_data_dir().ok is True


# --------------------------------------------------------------------------- #
# QQ 登录凭据
# --------------------------------------------------------------------------- #
def _fake_tencent_files(tmp_path: Path, accounts: list[str]) -> Path:
    root = tmp_path / "Tencent Files"
    root.mkdir(parents=True, exist_ok=True)
    (root / "NapCat").mkdir(exist_ok=True)  # 非账号目录，必须被忽略
    for account in accounts:
        (root / account).mkdir(exist_ok=True)
    (root / "not-a-number").mkdir(exist_ok=True)
    return root


def test_detect_credential_accounts(tmp_path: Path) -> None:
    root = _fake_tencent_files(tmp_path, ["10000001", "20000002"])
    assert envcheck.detect_credential_accounts(root) == ["10000001", "20000002"]


def test_detect_credential_accounts_missing_root(tmp_path: Path) -> None:
    assert envcheck.detect_credential_accounts(tmp_path / "nope") == []


def test_credential_item_ok_when_account_saved(tmp_path: Path) -> None:
    root = _fake_tencent_files(tmp_path, ["10000001"])
    item = envcheck.check_qq_credential("10000001", root=root)
    assert item.ok is True
    assert item.critical is False
    assert "免扫码" in item.detail


def test_credential_item_fails_when_account_has_no_credentials(tmp_path: Path) -> None:
    root = _fake_tencent_files(tmp_path, ["20000002"])
    item = envcheck.check_qq_credential("10000001", root=root)
    assert item.ok is False
    assert "扫码" in item.detail


def test_credential_item_lists_detected_accounts_on_first_run(tmp_path: Path) -> None:
    """首次运行时应当告诉用户本机有哪些号可以扫码登录，而不是笼统说"没有账号"。"""
    root = _fake_tencent_files(tmp_path, ["10000001", "20000002"])
    item = envcheck.check_qq_credential(None, root=root)
    assert item.ok is False
    assert item.critical is False
    assert "10000001" in item.detail and "20000002" in item.detail
    assert "2 个" in item.detail


def test_credential_item_on_clean_machine(tmp_path: Path) -> None:
    root = _fake_tencent_files(tmp_path, [])
    item = envcheck.check_qq_credential(None, root=root)
    assert item.ok is False
    assert "未检测到" in item.detail


def test_report_ok_ignores_non_critical_failures() -> None:
    report = EnvReport(
        items=[
            EnvItem(key="a", label="关键项", ok=True),
            EnvItem(key="b", label="非关键项", ok=False, critical=False),
        ]
    )
    assert report.ok is True
    assert len(report.failures()) == 1


def test_report_fails_on_critical() -> None:
    report = EnvReport(
        items=[
            EnvItem(key="a", label="关键项", ok=False),
            EnvItem(key="b", label="非关键项", ok=True, critical=False),
        ]
    )
    assert report.ok is False


def test_env_checker_full_run_with_stub_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    napcat = make_napcat_tree(tmp_path)
    monkeypatch.setenv("QLARK_NAPCAT_DIR", str(napcat))
    monkeypatch.setenv("QLARK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("QLARK_NAPCAT_EXE", str(napcat / "node.exe"))

    report = envcheck.EnvChecker(host="127.0.0.1", port=free_port()).run(with_ports=True)
    keys = {item.key for item in report.items}
    assert {"napcat_runtime", "napcat_dlls", "vc_runtime", "webview2", "data_dir", "ports"} <= keys

    # 假 NapCat 目录是完整的，这两个关键项必须通过
    by_key = {item.key: item for item in report.items}
    assert by_key["napcat_runtime"].ok is True
    assert by_key["napcat_dlls"].ok is True
    assert by_key["data_dir"].ok is True
