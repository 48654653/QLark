"""首次运行环境自检。

技术文档 §9.4 规定的首启动作。缺任何一项关键依赖，"双击即开"都不成立：

===============================  ====================================================
检查项                            缺失后果
===============================  ====================================================
NapCat 运行时                      没有 ``node.exe`` 就无从启动
VC++ 运行库                        ``wrapper.node`` 加载失败，程序直接起不来
``crypto.dll`` / ``ssl.dll``      同上（**官方 Node 包里根本没有这两个文件**）
Edge WebView2 运行时               pywebview 无法渲染界面
OneBot / WebUI 端口               被占用则网络配置起不来
用户数据目录可写                   配置与记忆无法落盘
QQ 登录凭据                        缺失则无法免扫码快速登录（非关键，可降级扫码）
===============================  ====================================================
"""

from __future__ import annotations

import logging
import os
import socket
import time
from pathlib import Path

from .. import paths
from ..core.models import EnvItem, EnvReport
from ..version import DEFAULT_ONEBOT_PORT, DEFAULT_WEBUI_PORT

log = logging.getLogger(__name__)

#: NapCat 根目录下必须存在的文件
_NAPCAT_REQUIRED = ("node.exe", "index.js", "wrapper.node", "package.json", "config.json")
#: 官方 Node 包缺失、必须从 QQ 安装目录补齐的两个 DLL
_NAPCAT_DLLS = ("crypto.dll", "ssl.dll")
#: VC++ 运行库（System32 下）
_VC_RUNTIME = ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll")

_WEBVIEW2_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


# --------------------------------------------------------------------------- #
# 单项探测
# --------------------------------------------------------------------------- #
def _runtime_hint(root: Path) -> str:
    """告诉用户**该怎么办**，而不只是"哪里不对"。

    打包版尤其需要：用户拿到的是一个文件夹，机器上很可能没有 Python，
    所以"跑一下 tools/fetch_napcat.py"这种建议对他们毫无用处。
    """
    if paths.is_frozen():
        return (
            "这是打包版，运行时需要和程序放在一起。两种办法："
            "① 把已有的 NapCat 运行时整个目录拷到上面这个路径；"
            "② 在源码目录重新运行 python tools\\build_exe.py（默认就会带上运行时）。"
        )
    return "先运行 python tools\\fetch_napcat.py 一键下载并补齐依赖。"


def check_napcat_runtime() -> EnvItem:
    root = paths.napcat_dir()
    if not root.is_dir():
        return EnvItem(
            key="napcat_runtime",
            label="NapCat 运行时",
            ok=False,
            detail=f"目录不存在：{root}。{_runtime_hint(root)}",
            fixable=False,
        )
    missing = [name for name in _NAPCAT_REQUIRED if not (root / name).is_file()]
    if missing:
        return EnvItem(
            key="napcat_runtime",
            label="NapCat 运行时",
            ok=False,
            detail="缺少文件：" + "、".join(missing) + "。" + _runtime_hint(root),
            fixable=False,
        )
    size_mb = (root / "wrapper.node").stat().st_size / 1024 / 1024
    return EnvItem(
        key="napcat_runtime",
        label="NapCat 运行时",
        ok=True,
        detail=f"{root}（wrapper.node {size_mb:.0f} MB）",
    )


def check_napcat_dlls() -> EnvItem:
    root = paths.napcat_dir()
    missing = [name for name in _NAPCAT_DLLS if not (root / name).is_file()]
    if missing:
        return EnvItem(
            key="napcat_dlls",
            label="crypto.dll / ssl.dll",
            ok=False,
            detail="缺少 " + "、".join(missing) + "（官方 Node 包不含，需从 QQ 安装目录补齐）",
            fixable=True,
        )
    return EnvItem(
        key="napcat_dlls",
        label="crypto.dll / ssl.dll",
        ok=True,
        detail="已就绪",
    )


def check_vc_runtime() -> EnvItem:
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    missing = [name for name in _VC_RUNTIME if not (system32 / name).is_file()]
    if missing:
        return EnvItem(
            key="vc_runtime",
            label="VC++ 运行库",
            ok=False,
            detail="缺少 " + "、".join(missing) + "（需安装 vc_redist.x64.exe）",
            fixable=True,
        )
    return EnvItem(key="vc_runtime", label="VC++ 运行库", ok=True, detail="已就绪")


def _read_webview2_version() -> str | None:
    try:
        import winreg  # type: ignore
    except ImportError:  # pragma: no cover - 非 Windows
        return None

    candidates = [
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_GUID}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_GUID}"),
        (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_GUID}"),
    ]
    for root, subkey in candidates:
        for access in (winreg.KEY_READ | getattr(winreg, "KEY_WOW64_32KEY", 0), winreg.KEY_READ):
            try:
                with winreg.OpenKey(root, subkey, 0, access) as handle:
                    value, _ = winreg.QueryValueEx(handle, "pv")
                    if value:
                        return str(value)
            except OSError:
                continue
    return None


def check_webview2() -> EnvItem:
    version = _read_webview2_version()
    if version:
        return EnvItem(key="webview2", label="Edge WebView2 运行时", ok=True, detail=f"版本 {version}")
    return EnvItem(
        key="webview2",
        label="Edge WebView2 运行时",
        ok=False,
        detail="未检测到，界面无法渲染。Win11 与多数 Win10 已内置，缺失时需安装 WebView2 Runtime",
        fixable=False,
    )


def port_is_free(host: str, port: int) -> bool:
    """端口是否可绑定。

    注意：**不能设 SO_REUSEADDR** —— 在 Windows 上它会允许绑定到已被占用的端口，
    导致误判为"空闲"。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def check_ports(
    host: str = "127.0.0.1",
    port: int | None = None,
    *,
    event_port: int | None = None,
) -> EnvItem:
    target = int(port or DEFAULT_ONEBOT_PORT)
    busy: list[str] = []
    if not port_is_free(host, target):
        busy.append(f"{target}（OneBot 动作通道）")
    if event_port is not None and not port_is_free(host, int(event_port)):
        busy.append(f"{event_port}（QLark 事件通道）")
    if not port_is_free(host, DEFAULT_WEBUI_PORT):
        busy.append(f"{DEFAULT_WEBUI_PORT}（WebUI）")
    if busy:
        return EnvItem(
            key="ports",
            label="网络端口",
            ok=False,
            detail="被占用：" + "、".join(busy) + "。可能是上次残留的 NapCat 进程",
            fixable=True,
        )
    ports = [str(target)]
    if event_port is not None:
        ports.append(str(event_port))
    ports.append(str(DEFAULT_WEBUI_PORT))
    return EnvItem(
        key="ports",
        label="网络端口",
        ok=True,
        detail="、".join(ports) + " 均空闲",
    )


def check_data_dir() -> EnvItem:
    root = paths.data_dir()
    if paths.is_writable(root):
        return EnvItem(key="data_dir", label="用户数据目录", ok=True, detail=str(root))
    return EnvItem(
        key="data_dir",
        label="用户数据目录",
        ok=False,
        detail=f"不可写：{root}",
        fixable=False,
    )


def credential_root() -> Path:
    """QQ 凭据根目录（``-q`` 快速登录读的就是这里）。"""
    override = os.environ.get("QLARK_TENCENT_FILES_DIR")
    if override:
        return Path(override).expanduser()
    return Path(os.path.expanduser("~")) / "Documents" / "Tencent Files"


def detect_credential_accounts(root: Path | None = None) -> list[str]:
    """列出本机已有登录凭据的 QQ 号。

    这些目录**不受重装 NapCat 影响**，是"能否免扫码快速登录"的判据。
    """
    base = root if root is not None else credential_root()
    if not base.is_dir():
        return []
    try:
        return sorted(
            item.name for item in base.iterdir() if item.is_dir() and item.name.isdigit()
        )
    except OSError:
        return []


def check_qq_credential(qq: str | None, *, root: Path | None = None) -> EnvItem:
    """QQ 登录凭据是否可用（决定能否免扫码）。非关键项。"""
    detected = detect_credential_accounts(root)

    if qq:
        base = root if root is not None else credential_root()
        credential_dir = base / str(qq)
        if credential_dir.is_dir():
            return EnvItem(
                key="qq_credential",
                label="QQ 登录凭据",
                ok=True,
                detail=f"已存在（{qq}），启动时免扫码快速登录",
                critical=False,
            )
        return EnvItem(
            key="qq_credential",
            label="QQ 登录凭据",
            ok=False,
            detail=f"未找到 {credential_dir}，本次启动需要扫码登录",
            critical=False,
        )

    if detected:
        return EnvItem(
            key="qq_credential",
            label="QQ 登录凭据",
            ok=False,
            detail=(
                f"QLark 尚未记录要使用哪个账号；检测到本机有 {len(detected)} 个已登录过的 QQ"
                f"（{'、'.join(detected)}），首次启动扫码其中一个即可，之后免扫码"
            ),
            critical=False,
        )
    return EnvItem(
        key="qq_credential",
        label="QQ 登录凭据",
        ok=False,
        detail="未检测到本机登录凭据，首次启动需要扫码登录",
        critical=False,
    )


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
class EnvChecker:
    """执行一次完整自检。"""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int | None = None,
        event_port: int | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.event_port = event_port

    def run(self, *, qq: str | None = None, with_ports: bool = True) -> EnvReport:
        items: list[EnvItem] = [
            check_napcat_runtime(),
            check_napcat_dlls(),
            check_vc_runtime(),
            check_webview2(),
            check_data_dir(),
        ]
        if with_ports:
            items.append(check_ports(self.host, self.port, event_port=self.event_port))
        items.append(check_qq_credential(qq))

        report = EnvReport(items=items, checked_at=time.time())
        for item in report.failures():
            if item.critical:
                log.warning("环境自检未通过：%s —— %s", item.label, item.detail)
            else:
                log.info("环境自检提示：%s —— %s", item.label, item.detail)
        return report
