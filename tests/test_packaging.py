"""打包相关的自检：冒烟检查 + 个人数据清理 + 发版前校验。

对应一次真实事故：发版前用文本替换清理 QQ 号时，把正则 ``\\d{5,12}`` 改成了
``\\d{5,00}``，``re.compile`` 在**导入期**就抛异常，打包版一启动就崩。
当时改完源码没跑测试、直接打包，直到启动测试才发现。

所以这里盯两件事：
1. ``smoke_check`` 能在打包前抓到"源码根本导入不了"；
2. ``sanitize_runtime`` / ``verify_clean`` 真的能清掉并认出个人数据。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import build_exe  # noqa: E402


# --------------------------------------------------------------------------- #
# 冒烟检查
# --------------------------------------------------------------------------- #
def test_smoke_check_passes_on_current_source() -> None:
    """当前源码必须能通过冒烟检查（跑不通就说明有导入期错误）。"""
    problems = build_exe.smoke_check()
    assert problems == [], "\n".join(problems)


def test_smoke_modules_cover_the_main_path() -> None:
    """主链路模块都要在冒烟清单里，否则等于没查。"""
    for name in ("qlark.app", "qlark.bot", "qlark.napcat.patterns", "qlark.chat.pipeline"):
        assert name in build_exe.SMOKE_MODULES, f"{name} 不在冒烟清单里"


# --------------------------------------------------------------------------- #
# patterns：正则必须能编译且能匹配（这次事故的直接受害者）
# --------------------------------------------------------------------------- #
def test_patterns_compile_and_match() -> None:
    from qlark.napcat import patterns

    assert patterns.RE_QUICK_LOGIN.search("[info] 正在快速登录  10001")
    assert patterns.RE_ACCOUNT_CONFIG.search("配置文件 napcat_10001.json加载 {}")
    assert patterns.RE_ONEBOT_START.search("[HTTP Server Adapter] Start On 127.0.0.1:3000")
    assert patterns.RE_QUICK_LOGIN.pattern.count("{5,12}") == 1, (
        "量词被改坏了 —— 上次就是把 {5,12} 改成了 {5,00}"
    )


# --------------------------------------------------------------------------- #
# 个人数据清理
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_runtime(tmp_path: Path) -> Path:
    """造一个"跑过一轮、带着个人数据"的运行时目录。"""
    root = tmp_path / "runtime" / "napcat"
    config = root / "napcat" / "config"
    cache = root / "napcat" / "cache"
    logs = root / "napcat" / "logs"
    for folder in (config, cache, logs):
        folder.mkdir(parents=True)

    # 该留的
    (config / "napcat.json").write_text('{"fileLog": false}', encoding="utf-8")

    # 该清的
    (config / "onebot11_10001.json").write_text('{"token": "secret"}', encoding="utf-8")
    (config / "onebot11_10001.json.bak.123").write_text("{}", encoding="utf-8")
    (config / "napcat_10001.json").write_text("{}", encoding="utf-8")
    (config / "napcat_protocol_10001.json").write_text("{}", encoding="utf-8")
    (config / "webui.json").write_text('{"token": "abcd"}', encoding="utf-8")
    (cache / "qrcode.png").write_bytes(b"\x89PNG")
    (logs / "napcat.log").write_text("log", encoding="utf-8")
    (root / "guild1.db").write_bytes(b"db")
    (root / "guild1.db-wal").write_bytes(b"wal")
    (root / "beacon_report.log").write_text("", encoding="utf-8")
    return root


def test_verify_clean_flags_personal_data(fake_runtime: Path) -> None:
    problems = build_exe.verify_clean(fake_runtime)
    joined = "\n".join(problems)
    for keyword in ("onebot11_10001", "webui.json", "guild1.db", "qrcode.png", "napcat.log"):
        assert keyword in joined, f"没认出 {keyword}：\n{joined}"


def test_sanitize_then_verify_is_clean(fake_runtime: Path) -> None:
    build_exe.sanitize_runtime(fake_runtime)
    assert build_exe.verify_clean(fake_runtime) == [], "清理之后仍然不干净"


def test_sanitize_keeps_the_shared_default_config(fake_runtime: Path) -> None:
    """``napcat.json`` 是通用默认配置，不含账号信息，必须留下。"""
    build_exe.sanitize_runtime(fake_runtime)
    kept = fake_runtime / "napcat" / "config" / "napcat.json"
    assert kept.is_file(), "把该留的默认配置也删了"
    assert kept.read_text(encoding="utf-8")


def test_sanitize_keeps_directories(fake_runtime: Path) -> None:
    """目录本身要留着 —— 程序启动时要用，删掉会让 NapCat 报错。"""
    build_exe.sanitize_runtime(fake_runtime)
    for name in ("cache", "logs", "config"):
        folder = fake_runtime / "napcat" / name
        assert folder.is_dir(), f"{name} 目录被删掉了"
        assert list(folder.iterdir()) == [] or name == "config"


def test_clean_runtime_passes_verification(tmp_path: Path) -> None:
    """本来就是干净的目录不该被误报。"""
    root = tmp_path / "runtime" / "napcat"
    (root / "napcat" / "config").mkdir(parents=True)
    (root / "napcat" / "config" / "napcat.json").write_text("{}", encoding="utf-8")
    assert build_exe.verify_clean(root) == []
