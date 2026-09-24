"""一键打包成免安装目录（需求规格 v1.1 §9.4）。

用法::

    python tools/build_exe.py                # 打包（不含 NapCat 运行时）
    python tools/build_exe.py --with-runtime # 顺带把 runtime/napcat 拷进去
    python tools/build_exe.py --check        # 只做前置检查，不真的打包

产物在 ``dist/QLark/``，双击 ``QLark.exe`` 即可运行。

**为什么默认不拷 NapCat 运行时**：它有 400MB+，而它本来就必须以独立进程运行、
往自己目录写配置。绝大多数情况下用户机器上已经有一份（``tools/fetch_napcat.py``
拉的），重复拷一份既慢又占地方。要交付"开箱即用"的压缩包时再加 ``--with-runtime``。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "QLark"
SPEC = ROOT / "QLark.spec"
RUNTIME = ROOT / "runtime" / "napcat"

#: 打包产物里必须存在的文件（少一个都说明打包漏了东西）
REQUIRED = [
    "QLark.exe",
    "_internal/web/index.html",
    "_internal/web/app.js",
    "_internal/web/app.css",
    "_internal/qlark/storage/schema.sql",
]


def check() -> int:
    """前置检查。任何一条不过就直接退出，别浪费十分钟去打包一个残废产物。"""
    problems: list[str] = []

    try:
        import PyInstaller  # noqa: F401

        print("  [ OK ] PyInstaller 已安装")
    except ImportError:
        problems.append("缺少 PyInstaller：pip install pyinstaller")

    for relative in (
        "run.py",
        "QLark.spec",
        "src/qlark/web/index.html",
        "src/qlark/web/app.js",
        "src/qlark/web/app.css",
        "src/qlark/storage/schema.sql",
        "src/qlark/assets/qlark.ico",
    ):
        path = ROOT / relative
        if path.exists():
            print(f"  [ OK ] {relative}")
        else:
            problems.append(f"缺少文件：{relative}")

    # 前端是纯静态资源，打包不会检查语法；这里替用户先看一眼
    node = shutil.which("node")
    if node:
        result = subprocess.run(
            [node, "--check", str(ROOT / "src" / "qlark" / "web" / "app.js")],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print("  [ OK ] app.js 语法检查通过")
        else:
            problems.append(f"app.js 语法错误：{result.stderr.strip()[:200]}")
    else:
        print("  [skip] 没找到 node，跳过 app.js 语法检查")

    if problems:
        print("\n前置检查未通过：")
        for item in problems:
            print(f"  [FAIL] {item}")
        return 1
    print("\n前置检查通过。")
    return 0


def build(*, with_runtime: bool, clean: bool) -> int:
    if clean:
        for path in (ROOT / "build", ROOT / "dist"):
            if path.exists():
                print(f"清理 {path} …")
                shutil.rmtree(path, ignore_errors=True)

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        str(SPEC),
    ]
    print("执行：" + " ".join(command))
    started = time.monotonic()
    result = subprocess.run(command, cwd=str(ROOT))
    if result.returncode != 0:
        print(f"\n打包失败（退出码 {result.returncode}）")
        return result.returncode
    print(f"\n打包完成，用时 {time.monotonic() - started:.0f} 秒")

    missing = [name for name in REQUIRED if not (DIST / name).exists()]
    if missing:
        print("\n[FAIL] 产物里缺少这些文件，打包不完整：")
        for name in missing:
            print(f"  - {name}")
        return 1
    print("产物完整性检查通过。")

    if with_runtime:
        if not RUNTIME.is_dir():
            print(f"\n[FAIL] 找不到 NapCat 运行时：{RUNTIME}")
            print("       先跑 python tools/fetch_napcat.py")
            return 1
        target = DIST / "runtime" / "napcat"
        print(f"复制 NapCat 运行时 → {target}（这一步比较慢）…")
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(RUNTIME, target)
        size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
        print(f"已复制，{size / 1024 / 1024:.0f} MB")
    else:
        print(
            "\n注意：产物里**不含** NapCat 运行时。"
            "\n      目标机器上需要先跑 python tools\\fetch_napcat.py，"
            "\n      或用 --with-runtime 重新打包。"
        )

    total = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    print(f"\n产物目录：{DIST}")
    print(f"总体积：{total / 1024 / 1024:.1f} MB")
    print("\n下一步：双击 dist\\QLark\\QLark.exe，会弹出首启向导。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 QLark 打包成免安装目录")
    parser.add_argument("--with-runtime", action="store_true", help="把 runtime/napcat 一起拷进产物")
    parser.add_argument("--check", action="store_true", help="只做前置检查")
    parser.add_argument("--no-clean", action="store_true", help="不清理 build/dist（增量打包更快）")
    args = parser.parse_args(argv)

    print("=" * 68)
    print("QLark 打包")
    print("=" * 68)
    code = check()
    if code != 0 or args.check:
        return code
    print()
    return build(with_runtime=args.with_runtime, clean=not args.no_clean)


if __name__ == "__main__":
    raise SystemExit(main())
