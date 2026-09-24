"""命令行入口。

用法::

    python run.py                 # 启动图形界面（正常使用）
    python run.py --headless      # 不启界面，直接在控制台跑启动/登录流程
    python run.py --self-check    # 只跑环境自检并打印结果
    python run.py --debug         # 打开 WebView2 开发者工具
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Sequence

from .app import Application


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qlark",
        description="QLark（云雀）—— 基于 NapCat 的 QQ 聊天机器人客户端",
    )
    parser.add_argument("--headless", action="store_true", help="不启动界面，在控制台运行")
    parser.add_argument("--self-check", action="store_true", help="只执行环境自检")
    parser.add_argument("--debug", action="store_true", help="开启调试输出 / 开发者工具")
    parser.add_argument(
        "--wait",
        type=float,
        default=0.0,
        help="headless 模式下的最长运行秒数（0 表示一直运行到 Ctrl+C）",
    )
    return parser


def _print_report(report: dict) -> int:
    print("=" * 68)
    print("QLark 环境自检")
    print("=" * 68)
    for item in report.get("items", []):
        mark = "[ OK ]" if item["ok"] else "[FAIL]"
        note = "" if item.get("critical", True) else "（非关键）"
        print(f"{mark} {item['label']}{note}")
        if item.get("detail"):
            print(f"       {item['detail']}")
    print("-" * 68)
    print("结论：" + ("通过" if report.get("ok") else "未通过"))
    return 0 if report.get("ok") else 1


def run_self_check(app: Application) -> int:
    report = app.login.run_env_check().to_dict()
    code = _print_report(report)
    app.shutdown()
    return code


def run_headless(app: Application, *, max_seconds: float = 0.0) -> int:
    print("headless 模式：正在执行启动 / 登录流程（Ctrl+C 退出）")
    log_seq = 0
    last_key: tuple | None = None
    started = time.monotonic()
    app.start_napcat()

    try:
        while True:
            for line in app.logbus.since(log_seq):
                stamp = time.strftime("%H:%M:%S", time.localtime(line.ts))
                print(f"{stamp} [{line.level:<5}] [{line.source:<6}] {line.text}", flush=True)
                log_seq = line.seq

            snap = app.state()
            key = (snap["phase"], snap["qq"], snap["online"], snap["pid"])
            if key != last_key:
                last_key = key
                print(
                    f"---- 阶段：{snap['phase']} | {snap['phase_text']} | "
                    f"qq={snap['qq']} online={snap['online']} pid={snap['pid']}",
                    flush=True,
                )
                if snap.get("last_error"):
                    print(f"---- 错误：{snap['last_error']}", flush=True)

            if max_seconds and time.monotonic() - started > max_seconds:
                print("---- 达到 --wait 上限，退出", flush=True)
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在退出…")
    finally:
        app.shutdown()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    app = Application(debug=args.debug)

    if args.self_check:
        return run_self_check(app)
    if args.headless:
        return run_headless(app, max_seconds=args.wait)
    return app.run()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
