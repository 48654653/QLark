"""界面自动化脚本：在**真实后端**下把界面切到指定视图并保持，便于截图验证。

用法::

    set QLARK_SHOWCASE=logs
    set QLARK_SHOWCASE=login
    python tools/showcase.py

依赖 :meth:`qlark.app.Application.run` 的 ``on_ready`` 回调。仅在开发期使用。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qlark.app import Application  # noqa: E402

SHOWCASE = (os.environ.get("QLARK_SHOWCASE") or "logs").strip().lower()
#: 自动启动流程走完（扫码 + 写配置 + 重启 + READY）大约需要这么久
WARMUP = float(os.environ.get("QLARK_SHOWCASE_WARMUP", "10"))
#: 切到目标视图后保持多久，给外部截图留时间
HOLD = float(os.environ.get("QLARK_SHOWCASE_HOLD", "30"))

SCRIPTS = {
    "logs": "document.querySelector('.nav-item[data-page=\"logs\"]').click(); 'logs'",
    "login": "document.getElementById('loginBtn').click(); 'login'",
    "memory": "document.querySelector('.nav-item[data-page=\"memory\"]').click(); 'memory'",
    "persona": "document.querySelector('.nav-item[data-page=\"persona\"]').click(); 'persona'",
    "model": "document.querySelector('.nav-item[data-page=\"model\"]').click(); 'model'",
}


def main() -> int:
    app = Application()

    def on_ready(window) -> None:
        def worker() -> None:
            time.sleep(WARMUP)
            script = SCRIPTS.get(SHOWCASE)
            if script:
                try:
                    window.evaluate_js(script)
                    print(f"[showcase] switched to {SHOWCASE}", flush=True)
                except Exception as exc:
                    print(f"[showcase] switch failed: {exc}", flush=True)
            time.sleep(HOLD)

        threading.Thread(target=worker, daemon=True).start()

    app.run(on_ready=on_ready)
    return 0


if __name__ == "__main__":
    sys.exit(main())
