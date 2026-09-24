"""在命令行里设置模型配置（M3 的「模型配置页」做好之前的临时入口）。

直接写进 QLark 的数据库，等价于以后在界面里点「应用」。

用法::

    python tools/set_model.py --show
    python tools/set_model.py --base-url https://api.deepseek.com --api-key sk-xxx --model deepseek-chat
    python tools/set_model.py --max-tokens 2048 --temperature 0.8
    python tools/set_model.py --test                 # 用当前配置真发一次最小请求
    python tools/set_model.py --clear-api-key

> 设置完**不需要重启 QLark**：消息流水线每次调用前会重新读取配置。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qlark import paths  # noqa: E402
from qlark.llm.client import LLMError, OpenAIChatClient, normalize_base_url  # noqa: E402
from qlark.settings import Settings  # noqa: E402
from qlark.storage.db import Database  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:  # pragma: no cover
    pass


def mask(value: str | None) -> str:
    text = str(value or "")
    if not text:
        return "（未设置）"
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}{'*' * (len(text) - 8)}{text[-4:]}"


def show(settings: Settings) -> None:
    print("=" * 60)
    print("QLark 模型配置")
    print("=" * 60)
    base_url = settings.get("base_url") or ""
    print(f"Base URL   : {base_url or '（未设置）'}")
    if base_url:
        try:
            print(f"  实际端点 : {normalize_base_url(base_url)}")
        except LLMError as exc:
            print(f"  实际端点 : 无法解析（{exc}）")
    print(f"API Key    : {mask(settings.get('api_key'))}")
    print(f"模型名     : {settings.get('model') or '（未设置）'}")
    print(f"最大输出   : {settings.get('max_tokens')}")
    print(f"温度       : {settings.get('temperature')}")
    print(f"记忆轮数   : {settings.get('memory_rounds')}")
    print(f"数据目录   : {paths.data_dir()}")
    print("-" * 60)
    print(f"OneBot 动作通道 : {settings.onebot_base_url}")
    print(f"QLark 事件通道  : {settings.event_base_url}")
    print(f"QQ 账号         : {settings.qq_account or '（未登录）'}")
    configured = bool(base_url and settings.get("api_key") and settings.get("model"))
    print("-" * 60)
    print("状态：" + ("已配置，可以对话" if configured else "未配置完整，机器人无法回复"))


def run_test(settings: Settings) -> int:
    client = OpenAIChatClient(
        str(settings.get("base_url") or ""),
        str(settings.get("api_key") or ""),
        str(settings.get("model") or ""),
        timeout=30,
        max_tokens=1,  # 只验证连通性，尽量少花 Token
    )
    print("正在发送最小测试请求（max_tokens=1）…")
    try:
        result = client.complete([{"role": "user", "content": "1"}], retries=0)
    except LLMError as exc:
        print(f"[失败] {exc.hint()}：{exc}")
        return 1
    print("[正常] 连接正常")
    print(f"       模型回显：{result.model}")
    print(f"       用量：{result.prompt_tokens} + {result.completion_tokens} tokens")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="set_model", description="设置 QLark 的模型配置（OpenAI 兼容）"
    )
    parser.add_argument("--show", action="store_true", help="显示当前配置")
    parser.add_argument("--base-url", help="例如 https://api.deepseek.com")
    parser.add_argument("--api-key", help="API Key")
    parser.add_argument("--model", help="模型名，例如 deepseek-chat")
    parser.add_argument("--max-tokens", type=int, help="最大输出 Token")
    parser.add_argument("--temperature", type=float, help="温度 0~2")
    parser.add_argument("--memory-rounds", type=int, help="记忆轮数（默认 20）")
    parser.add_argument("--clear-api-key", action="store_true", help="清空 API Key")
    parser.add_argument("--test", action="store_true", help="用当前配置发一次最小请求")
    args = parser.parse_args(argv)

    paths.ensure_data_dirs()
    db = Database(paths.db_path())
    settings = Settings(db)

    try:
        if args.clear_api_key:
            settings.set("api_key", "")
            print("已清空 API Key")

        updates: dict[str, object] = {}
        if args.base_url is not None:
            value = args.base_url.strip()
            try:
                normalize_base_url(value)
            except LLMError as exc:
                print(f"Base URL 不合法：{exc}")
                return 2
            updates["base_url"] = value
        if args.api_key is not None:
            updates["api_key"] = args.api_key.strip()
        if args.model is not None:
            updates["model"] = args.model.strip()
        if args.max_tokens is not None:
            if args.max_tokens < 1:
                print("最大输出 Token 必须大于 0")
                return 2
            updates["max_tokens"] = args.max_tokens
        if args.temperature is not None:
            if not 0.0 <= args.temperature <= 2.0:
                print("温度必须在 0~2 之间")
                return 2
            updates["temperature"] = args.temperature
        if args.memory_rounds is not None:
            if args.memory_rounds < 1:
                print("记忆轮数必须大于 0")
                return 2
            updates["memory_rounds"] = args.memory_rounds

        if updates:
            settings.update(**updates)
            print("已保存：" + "、".join(updates))

        show(settings)

        if args.test:
            print()
            return run_test(settings)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
