"""一键准备 NapCat 运行时（对应技术文档步骤 1~4）。

做四件事：

1. 下载 ``NapCat.Shell.Windows.Node.zip``（约 111 MB，支持镜像加速与自动回落）
2. 解压到 ``runtime/napcat/``，自动处理 zip 内多套一层目录的情况
3. 从本机已安装的 QQ 里补齐 ``crypto.dll`` / ``ssl.dll``
   （**官方 Node 包不含这两个文件**，属上游打包缺陷）
4. 校验必需文件是否齐全

用法::

    python tools/fetch_napcat.py                # 完整准备
    python tools/fetch_napcat.py --check-only   # 只校验，不下载
    python tools/fetch_napcat.py --dll-only     # 只补两个 DLL
    python tools/fetch_napcat.py --force        # 覆盖已存在的运行时
    python tools/fetch_napcat.py --version 4.18.28
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qlark import paths  # noqa: E402

DEFAULT_VERSION = "4.18.28"
GITHUB = "https://github.com/NapNeko/NapCatQQ/releases/download/v{ver}/NapCat.Shell.Windows.Node.zip"
MIRROR = "https://ghfile.geekertao.top/" + GITHUB

#: 运行时根目录下必须存在的文件
REQUIRED = ("node.exe", "index.js", "wrapper.node", "package.json", "config.json")
#: 需要从 QQ 补齐的两个 DLL
DLLS = ("crypto.dll", "ssl.dll")
#: 小于这个体积说明下成了 OneKey 包（约 1 MB）
MIN_ZIP_BYTES = 80 * 1024 * 1024

C_OK = "  [OK] "
C_BAD = "  [!!] "
C_INFO = "  ...  "


def log(msg: str = "") -> None:
    print(msg, flush=True)


def step(title: str) -> None:
    log(f"\n=== {title} ===")


# --------------------------------------------------------------------------- #
# 定位
# --------------------------------------------------------------------------- #
def find_qq_roots() -> list[Path]:
    """可能装了官方 QQ 的目录。"""
    roots: list[Path] = []
    try:
        import winreg

        candidates = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
        ]
        for hive, subkey in candidates:
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    uninstall, _ = winreg.QueryValueEx(key, "UninstallString")
                path = Path(str(uninstall).strip('"'))
                if path.parent.is_dir():
                    roots.append(path.parent)
            except OSError:
                continue
    except ImportError:  # pragma: no cover - 非 Windows
        pass

    for base in ("C:/Program Files/Tencent", "D:/Program Files/Tencent", "E:/Program Files/Tencent"):
        candidate = Path(base)
        if candidate.is_dir():
            for child in candidate.glob("QQ*"):
                if child.is_dir():
                    roots.append(child)

    # 去重且保序
    seen: set[str] = set()
    unique: list[Path] = []
    for item in roots:
        key = str(item).lower()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def find_qq_dlls() -> dict[str, Path]:
    """在 QQ 安装目录里找 ``crypto.dll`` / ``ssl.dll``（取版本号最新的那份）。"""
    found: dict[str, Path] = {}
    for root in find_qq_roots():
        versions_dir = root / "versions"
        search_root = versions_dir if versions_dir.is_dir() else root
        for dll in DLLS:
            if dll in found:
                continue
            candidates = sorted(
                search_root.glob(f"*/resources/app/{dll}"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if candidates:
                found[dll] = candidates[0]
    return found


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def verify(napcat_dir: Path) -> tuple[bool, list[str]]:
    missing = [name for name in (*REQUIRED, *DLLS) if not (napcat_dir / name).is_file()]
    return (not missing), missing


def report(napcat_dir: Path) -> bool:
    ok, missing = verify(napcat_dir)
    log(f"NapCat 根目录：{napcat_dir}")
    for name in (*REQUIRED, *DLLS):
        path = napcat_dir / name
        if path.is_file():
            log(f"{C_OK}{name:<16} {path.stat().st_size / 1024 / 1024:8.2f} MB")
        else:
            log(f"{C_BAD}{name:<16} 缺失")
    return ok


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #
def download(url: str, dest: Path) -> bool:
    import requests

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log(f"{C_INFO}GET {url}")
    try:
        with requests.get(url, stream=True, timeout=30) as response:
            response.raise_for_status()
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            last_report = 0.0
            with open(tmp, "wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    done += len(chunk)
                    now = time.time()
                    if now - last_report > 0.5:
                        last_report = now
                        if total:
                            pct = done * 100 / total
                            log(
                                f"\r{C_INFO}已下载 {done / 1048576:6.1f} / {total / 1048576:.1f} MB"
                                f"  ({pct:5.1f}%)",
                            )
                        else:
                            log(f"\r{C_INFO}已下载 {done / 1048576:6.1f} MB")
            log("")
    except Exception as exc:
        log(f"\n{C_BAD}下载失败：{exc}")
        tmp.unlink(missing_ok=True)
        return False

    if tmp.stat().st_size < MIN_ZIP_BYTES:
        log(
            f"{C_BAD}文件只有 {tmp.stat().st_size / 1048576:.1f} MB，"
            "可能下成了 OneKey 引导包（约 1 MB）。请检查版本号是否存在。"
        )
        tmp.unlink(missing_ok=True)
        return False

    tmp.replace(dest)
    return True


def fetch_zip(version: str, dest: Path) -> bool:
    urls = [
        MIRROR.format(ver=version),  # 实测约 2 倍速，优先
        GITHUB.format(ver=version),
    ]
    for url in urls:
        if download(url, dest):
            log(f"{C_OK}已下载 {dest}（{dest.stat().st_size / 1048576:.1f} MB）")
            return True
        log(f"{C_INFO}换下一个源重试…")
    return False


# --------------------------------------------------------------------------- #
# 解压
# --------------------------------------------------------------------------- #
def extract(zip_path: Path, napcat_dir: Path) -> bool:
    staging = napcat_dir.parent / "_extract"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    log(f"{C_INFO}正在解压（约 300 MB，需要一会儿）…")
    try:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(staging)
    except Exception as exc:
        log(f"{C_BAD}解压失败：{exc}")
        return False

    # zip 内可能多套一层目录：找到 node.exe 所在的那一层
    candidates = sorted(staging.rglob("node.exe"), key=lambda p: len(p.parts))
    if not candidates:
        log(f"{C_BAD}解压后没找到 node.exe，包结构异常")
        return False
    source = candidates[0].parent
    log(f"{C_INFO}包内结构：{source.relative_to(staging) or '.'}")

    shutil.rmtree(napcat_dir, ignore_errors=True)
    napcat_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(napcat_dir))
    shutil.rmtree(staging, ignore_errors=True)
    log(f"{C_OK}已解压到 {napcat_dir}")
    return True


# --------------------------------------------------------------------------- #
# 补 DLL
# --------------------------------------------------------------------------- #
def copy_dlls(napcat_dir: Path) -> bool:
    sources = find_qq_dlls()
    if not sources:
        log(f"{C_BAD}没找到已安装的 QQ，无法补齐 {', '.join(DLLS)}")
        log("       安装官方 QQ 后重试：winget install --id Tencent.QQ.NT --exact")
        log("       （不需要登录 QQ，装完取文件即可）")
        return False

    ok = True
    for name in DLLS:
        target = napcat_dir / name
        origin = sources.get(name)
        if origin is None:
            log(f"{C_BAD}{name}：未在 QQ 安装目录中找到")
            ok = False
            continue
        if target.is_file() and target.stat().st_size == origin.stat().st_size:
            log(f"{C_OK}{name}：已就绪（{target.stat().st_size / 1024 / 1024:.2f} MB）")
            continue
        try:
            shutil.copy2(origin, target)
            log(f"{C_OK}{name}：已从 {origin} 复制（{target.stat().st_size / 1024 / 1024:.2f} MB）")
        except OSError as exc:
            log(f"{C_BAD}{name}：复制失败 {exc}")
            ok = False
    return ok


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fetch_napcat",
        description="一键准备 NapCat 运行时（下载 + 解压 + 补两个 DLL + 校验）",
    )
    parser.add_argument("--version", default=DEFAULT_VERSION, help=f"NapCat 版本（默认 {DEFAULT_VERSION}）")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的运行时")
    parser.add_argument("--check-only", action="store_true", help="只校验，不做任何修改")
    parser.add_argument("--dll-only", action="store_true", help="只补齐 crypto.dll / ssl.dll")
    args = parser.parse_args(argv)

    napcat_dir = paths.napcat_dir()
    zip_path = paths.runtime_dir() / "_dl" / "Node.zip"

    log("QLark · NapCat 运行时准备")
    log(f"程序目录：{paths.app_dir()}")
    log(f"目标目录：{napcat_dir}")

    if args.check_only:
        step("校验")
        return 0 if report(napcat_dir) else 1

    if args.dll_only:
        step("补齐 DLL")
        ok = copy_dlls(napcat_dir)
        step("校验")
        return 0 if (report(napcat_dir) and ok) else 1

    # 已经装好就不重复下载
    already_ok, _ = verify(napcat_dir)
    if already_ok and not args.force:
        log(f"\n{C_OK}运行时已就绪，无需重新准备（用 --force 可强制重装）")
        step("校验")
        report(napcat_dir)
        return 0

    step(f"下载 NapCat {args.version}")
    if not zip_path.is_file() or args.force:
        if not fetch_zip(args.version, zip_path):
            log(f"\n{C_BAD}下载失败。可手动下载后放到：{zip_path}")
            log(f"       {GITHUB.format(ver=args.version)}")
            return 1
    else:
        log(f"{C_OK}复用已下载的包：{zip_path}（{zip_path.stat().st_size / 1048576:.1f} MB）")

    step("解压")
    if not extract(zip_path, napcat_dir):
        return 1

    step("补齐 crypto.dll / ssl.dll")
    dll_ok = copy_dlls(napcat_dir)

    step("校验")
    files_ok = report(napcat_dir)

    if files_ok and dll_ok:
        log("\n准备完成。下一步：")
        log("  1) 校验环境：  python run.py --self-check")
        log("  2) 启动程序：  python run.py")
        return 0

    log("\n准备未完成，请按上面的 [!!] 项处理。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
