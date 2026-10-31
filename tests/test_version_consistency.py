"""版本号三处必须一致：``version.py`` / ``pyproject.toml`` / ``README.md``。

真实事故：代码早就 v2.3.2 了，README 顶部还写着「版本：v2.2」——
推上去以后被用户当场发现。三处各写各的，迟早会漂；这里钉死。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _version_py() -> str:
    src = _read("src/qlark/version.py")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', src)
    assert match, "version.py 里找不到 __version__"
    return match.group(1)


def _version_pyproject() -> str:
    data = tomllib.loads(_read("pyproject.toml"))
    return str(data["project"]["version"])


def _version_readme() -> str:
    match = re.search(r"版本：\*\*v([0-9][^*]*)\*\*", _read("README.md"))
    assert match, "README 里找不到「- 版本：**vX.Y.Z**」这一行"
    return match.group(1).strip()


def test_versions_agree() -> None:
    py = _version_py()
    assert py == _version_pyproject(), f"version.py={py} 与 pyproject.toml={_version_pyproject()} 不一致"
    assert py == _version_readme(), f"version.py={py} 与 README 顶部={_version_readme()} 不一致"
