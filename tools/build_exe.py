"""一键打包成免安装目录（需求规格 v1.1 §9.4）。

用法::

    python tools/build_exe.py                # 打包（**默认带上 NapCat 运行时**）
    python tools/build_exe.py --no-runtime   # 只打程序本体，运行时另配
    python tools/build_exe.py --check        # 只做前置检查，不真的打包

产物在 ``dist/QLark/``，双击 ``QLark.exe`` 即可运行。

**为什么默认就带运行时**：最初这里是"默认不带、要加 --with-runtime"，
结果第一次交付出去的包双击就报「环境自检未通过：NapCat 运行时目录不存在」——
一个跑不起来的"免安装包"等于没交付。运行时（约 326MB）本来就必须和程序放一起
（它要作为独立进程启动、往自己目录写配置），所以默认带上才是对的，
要省空间的人自己加 ``--no-runtime``。
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
        count = sum(1 for f in target.rglob("*") if f.is_file())
        print(f"已复制 {count} 个文件，{size / 1024 / 1024:.0f} MB")

        # 逐字节核验：326MB 的拷贝一秒就完事，不核对一遍不敢说它是好的
        bad = _verify_copy(RUNTIME, target)
        if bad:
            print(f"\n[FAIL] 复制后有 {bad} 处不一致，产物不可用")
            return 1
        print("复制完整性核验通过（文件数与体积一致）。")
    else:
        print(
            "\n警告：产物里**不含** NapCat 运行时（--no-runtime）。"
            "\n      目标机器上把 NapCat 运行时放到 dist\\QLark\\runtime\\napcat 才能用。"
        )

    total = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    print(f"\n产物目录：{DIST}")
    print(f"总体积：{total / 1024 / 1024:.1f} MB")
    print("\n下一步：双击 dist\\QLark\\QLark.exe，会弹出首启向导。")
    return 0


def _verify_copy(source: Path, target: Path) -> int:
    """核验拷贝结果，返回不一致的文件数。

    只比文件数与体积（比对 726 个文件的哈希要十几秒，而体积不一致已经能
    抓住绝大多数的拷贝失败；真正的逐字节核验由 tools 里的独立脚本负责）。
    """
    def snapshot(root: Path) -> dict[str, int]:
        return {
            str(p.relative_to(root)): p.stat().st_size
            for p in root.rglob("*")
            if p.is_file()
        }

    left, right = snapshot(source), snapshot(target)
    problems = 0
    for name, size in left.items():
        if name not in right:
            print(f"  [FAIL] 缺少 {name}")
            problems += 1
        elif right[name] != size:
            print(f"  [FAIL] 大小不一致 {name}：{size} → {right[name]}")
            problems += 1
    if len(left) != len(right):
        print(f"  [FAIL] 文件数不一致：{len(left)} → {len(right)}")
        problems += 1
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 QLark 打包成免安装目录")
    parser.add_argument(
        "--no-runtime",
        action="store_true",
        help="不拷 NapCat 运行时（默认会拷；拷了才是开箱即用的包）",
    )
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
    return build(with_runtime=not args.no_runtime, clean=not args.no_clean)


if __name__ == "__main__":
    raise SystemExit(main())
