"""开发期启动脚本：``python run.py``。

把 ``src`` 加进 ``sys.path`` 后调用包入口，免去安装步骤。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from qlark.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
