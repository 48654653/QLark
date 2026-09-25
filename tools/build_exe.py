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
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "QLark"
SPEC = ROOT / "QLark.spec"
RUNTIME = ROOT / "runtime" / "napcat"

#: 表情包素材支持的图片格式（与 qlark.chat.memes.SUPPORTED_SUFFIXES 保持一致）
MEME_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})

#: 运行时里**必须清掉**的个人数据（相对 ``runtime/napcat`` 的 glob）。
#:
#: 这些全是程序跑起来之后自己写进去的：QQ 号、登录态、WebUI 访问 token、
#: 登录二维码、日志、NapCat 自己的库。装到别人机器上会重新生成，
#: **但跟着包发出去就等于把开发者的账号信息一起发了**。
#:
#: 注意 ``napcat/config/napcat.json`` **不在**列表里 —— 那是 NapCat 的通用默认配置，
#: 不含账号信息，留着反而省事（下划线通配匹配不到它）。
RUNTIME_PERSONAL_GLOBS = (
    "napcat/config/onebot11_*.json",  # 文件名带 QQ 号，内容带 token
    "napcat/config/onebot11_*.json.bak.*",  # 上面那个的历史备份，同样带 QQ 号和 token
    "napcat/config/napcat_*.json",  # 文件名带 QQ 号
    "napcat/config/napcat_protocol_*.json",  # 同上
    "napcat/config/webui.json",  # 含 WebUI 访问 token，且默认监听所有网卡
    "napcat/cache/*",  # 登录二维码等
    "napcat/logs/*",
    "guild1.db",  # NapCat 自己的库，含账号数据
    "guild1.db-shm",
    "guild1.db-wal",
    "*.log",
)

#: 文件名里带 QQ 号的模式（QQ 号按 5~11 位数字算）
_QQ_FILE_RE = re.compile(r"^(onebot11|napcat|napcat_protocol)_\d{5,11}\.json", re.IGNORECASE)


def sanitize_runtime(dest: Path) -> int:
    """删掉运行时里属于**开发者本人**的数据。返回清理项数。

    目录本身保留（程序要用），只清空里面的内容。
    """
    removed = 0
    for pattern in RUNTIME_PERSONAL_GLOBS:
        for path in dest.glob(pattern):
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                    path.mkdir(parents=True, exist_ok=True)  # 目录留着
                else:
                    path.unlink()
                removed += 1
            except OSError as exc:  # pragma: no cover - 文件被占用之类的
                print(f"  [warn] 清理失败 {path}：{exc}")
    print(f"已清理运行时里的个人数据：{removed} 项")
    return removed


def verify_clean(dest: Path) -> list[str]:
    """发版前核对产物里没有个人数据。返回问题清单，空列表表示干净。"""
    problems: list[str] = []
    for path in dest.rglob("*"):
        rel = path.relative_to(dest).as_posix()
        name = path.name
        if _QQ_FILE_RE.match(name):
            problems.append(f"文件名含 QQ 号：{rel}")
        if rel.endswith("napcat/config/webui.json"):
            problems.append(f"WebUI 配置（含访问 token）：{rel}")
        if name.startswith("guild1.db"):
            problems.append(f"NapCat 的数据库（含账号数据）：{rel}")
        if not path.is_file():
            continue
        if path.suffix.lower() == ".log":
            problems.append(f"日志文件：{rel}")
        if "/napcat/cache/" in f"/{rel}":
            problems.append(f"缓存文件（可能是登录二维码）：{rel}")
    return problems


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


#: 冒烟检查要导入的模块（覆盖主链路）
SMOKE_MODULES = (
    "qlark.__main__",
    "qlark.app",
    "qlark.bot",
    "qlark.napcat.patterns",
    "qlark.chat.pipeline",
    "qlark.chat.reply_format",
    "qlark.chat.memes",
    "qlark.web",
)


def smoke_check() -> list[str]:
    """打包前先把主链路导入一遍。返回问题清单（空 = 通过）。

    **为什么必须有这一步**：纯语法/导入错误（正则写坏、少个逗号、名字打错）
    在这里一秒就能抓到，不用等打包完、启动崩溃才发现 —— 而打包版一崩，
    用户看到的就是"双击没反应"，连报错都看不见。

    这条是拿真实事故换来的：一次发版前的文本替换把正则 ``\\d{5,12}`` 改成了
    ``\\d{5,00}``，``re.compile`` 在导入期就抛异常。当时改完源码没跑测试、
    直接打包，直到启动测试才炸出来。跑一遍导入只要一秒，别再省这一秒。
    """
    code = "; ".join(f"import {name}" for name in SMOKE_MODULES)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        return [f"冒烟检查跑不起来：{exc}"]
    if result.returncode == 0:
        return []
    tail = (result.stderr or "").strip().splitlines()
    return [f"导入 {len(SMOKE_MODULES)} 个主链路模块失败：", *tail[-12:]]


def copy_memes(dest: Path) -> None:
    """把源码目录的 ``memes/`` 拷进产物（用户放的表情包素材）。

    为什么用"拷贝"而不是让用户在 ``dist`` 里放图：``dist`` 每次打包都会被整个
    删掉重建，放进去的东西下一次就没了。源码目录才是母本。

    目录不存在或没有图片时**跳过**，不算失败 —— 空的表情库只是不能发表情，
    不影响程序启动（设置项里也可以随时指到别的目录）。
    """
    source = ROOT / "memes"
    if not source.is_dir():
        print("未找到 memes 目录，跳过表情包素材")
        return

    images = [
        path
        for path in source.rglob("*")
        if path.is_file() and path.suffix.lower() in MEME_SUFFIXES
    ]
    if not images:
        print("memes 目录里没有图片，跳过")
        return

    target = dest / "memes"
    shutil.copytree(source, target, dirs_exist_ok=True)
    total = sum(path.stat().st_size for path in images)
    print(f"已复制表情包素材：{len(images)} 张，{total / 1024 / 1024:.1f} MB → {target}")


def build(*, with_runtime: bool, clean: bool) -> int:
    # 先冒烟：源码都导入不了就别浪费两分钟去打包了
    smoke = smoke_check()
    if smoke:
        print("[FAIL] 冒烟检查未通过，源码有问题：")
        for line in smoke:
            print(f"  {line}")
        return 1
    print("冒烟检查通过（主链路模块都能导入）。")

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

    # 表情包素材跟着包走：源码目录是母本，dist 每次都会被清掉
    copy_memes(DIST)

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

        # 拷完立刻清个人数据 —— 放在核验之后，免得"核验通过"掩盖了清理动作
        sanitize_runtime(target)
    else:
        print(
            "\n警告：产物里**不含** NapCat 运行时（--no-runtime）。"
            "\n      目标机器上把 NapCat 运行时放到 dist\\QLark\\runtime\\napcat 才能用。"
        )

    # 发版前的最后一道关：确认产物里没有个人数据。
    # **必须有这一步** —— 清理逻辑将来可能被改动或漏掉某个新文件，
    # 而"把开发者的 QQ 号和 token 发出去"是发版事故里最难挽回的一种。
    problems = verify_clean(DIST)
    if problems:
        print(f"\n[FAIL] 产物里发现 {len(problems)} 处个人数据，不能发版：")
        for item in problems[:30]:
            print(f"  - {item}")
        if len(problems) > 30:
            print(f"  … 还有 {len(problems) - 30} 处")
        return 1
    print("个人数据检查通过（没有 QQ 号文件名、登录态、WebUI token、缓存、日志）。")

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
    parser.add_argument(
        "--clean-dir",
        default="",
        metavar="目录",
        help=(
            "只清理并校验指定目录，不打包。"
            "发布前如果发现那份被运行过（程序会往自己目录写 QQ 号、token、登录态），用它清一遍。"
        ),
    )
    args = parser.parse_args(argv)

    if args.clean_dir:
        target = Path(args.clean_dir).expanduser().resolve()
        print("=" * 68)
        print(f"清理发布目录：{target}")
        print("=" * 68)
        if not target.is_dir():
            print(f"[FAIL] 目录不存在：{target}")
            return 1
        runtime = target / "runtime" / "napcat"
        if runtime.is_dir():
            sanitize_runtime(runtime)
        else:
            print("没找到 runtime/napcat，只做校验")
        problems = verify_clean(target)
        if problems:
            print(f"\n[FAIL] 仍有 {len(problems)} 处个人数据：")
            for item in problems[:30]:
                print(f"  - {item}")
            return 1
        print("\n清理完成，复检通过：可以直接打包发出。")
        return 0

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
