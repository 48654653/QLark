"""路径管理。

QLark 把文件分成两类，**必须严格分开**：

* **程序目录**（``app_dir``）—— 安装位置，可能位于 ``Program Files`` 这类只读目录，
  只存放代码与 NapCat 运行时。
* **用户数据目录**（``data_dir``）—— ``%LOCALAPPDATA%\\QLark``，存放配置、数据库、
  日志、缓存。永远可写，卸载重装不丢。

所有路径都可以用环境变量覆盖，方便测试与便携部署：

===========================  ==========================================
环境变量                      作用
===========================  ==========================================
``QLARK_RUNTIME_DIR``        NapCat 运行时根目录（默认 ``<程序目录>/runtime``）
``QLARK_NAPCAT_DIR``         直接指定 ``runtime/napcat`` 目录
``QLARK_DATA_DIR``           用户数据目录
``QLARK_NAPCAT_EXE``         NapCat 启动可执行文件（默认 ``<napcat>/node.exe``）
``QLARK_NAPCAT_ENTRY``       入口脚本（默认 ``<napcat>/index.js``）
``QLARK_SKIP_ENV_CHECK``     设为 ``1`` 时跳过环境自检（仅测试用）
===========================  ==========================================
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .version import APP_NAME


# --------------------------------------------------------------------------- #
# 程序目录
# --------------------------------------------------------------------------- #
def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包产物中。"""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """程序目录。

    * 打包后：``exe`` 所在目录
    * 开发期：项目根目录（``src/qlark/paths.py`` → 上溯三层）
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def resource_dir() -> Path:
    """只读资源目录（前端 HTML/CSS/JS 等）。"""
    mei = getattr(sys, "_MEIPASS", None)  # PyInstaller onefile 解包目录
    if is_frozen() and mei:
        return Path(mei)
    return app_dir()


def web_dir() -> Path:
    """前端静态文件目录。"""
    if is_frozen():
        return resource_dir() / "web"
    return Path(__file__).resolve().parent / "web"


# --------------------------------------------------------------------------- #
# NapCat 运行时
# --------------------------------------------------------------------------- #
def runtime_dir() -> Path:
    override = os.environ.get("QLARK_RUNTIME_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return app_dir() / "runtime"


def napcat_dir() -> Path:
    """``runtime/napcat`` —— NapCat 解压根目录，也是 spawn 时的工作目录。"""
    override = os.environ.get("QLARK_NAPCAT_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return runtime_dir() / "napcat"


def napcat_config_dir() -> Path:
    return napcat_dir() / "napcat" / "config"


def napcat_qrcode_path() -> Path:
    """登录二维码落盘位置（登录成功后不会被 NapCat 清理）。"""
    return napcat_dir() / "napcat" / "cache" / "qrcode.png"


def napcat_exe() -> Path:
    override = os.environ.get("QLARK_NAPCAT_EXE")
    if override:
        return Path(override).expanduser().resolve()
    return napcat_dir() / "node.exe"


def napcat_entry() -> Path:
    override = os.environ.get("QLARK_NAPCAT_ENTRY")
    if override:
        return Path(override).expanduser().resolve()
    return napcat_dir() / "index.js"


# --------------------------------------------------------------------------- #
# 用户数据目录
# --------------------------------------------------------------------------- #
def data_dir() -> Path:
    override = os.environ.get("QLARK_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / APP_NAME


def db_path() -> Path:
    return data_dir() / "data" / "qlark.db"


def log_dir() -> Path:
    return data_dir() / "logs"


def cache_dir() -> Path:
    return data_dir() / "cache"


def tmp_dir() -> Path:
    return data_dir() / "tmp"


# --------------------------------------------------------------------------- #
# 初始化
# --------------------------------------------------------------------------- #
def ensure_data_dirs() -> Path:
    """创建用户数据目录结构，返回数据根目录。"""
    root = data_dir()
    for sub in ("", "data", "logs", "cache", "tmp"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def is_writable(path: Path) -> bool:
    """探测目录是否可写（不留下残留文件）。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".qlark_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def resolve_web_index() -> Path:
    """定位 ``index.html``（开发期与打包后目录结构不同）。"""
    candidates = [
        web_dir() / "index.html",
        resource_dir() / "web" / "index.html",
        app_dir() / "src" / "qlark" / "web" / "index.html",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]
