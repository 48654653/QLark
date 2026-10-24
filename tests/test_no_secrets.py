"""仓库里不许出现真实凭据。

这条是拿一次真实失误换来的：做「趣味功能」时把用户的和风 **API Host**
写成了输入框的 placeholder，又原样写进了测试文件（连 API Key 一起）。
提交上去等于把账号的认证信息发到公开仓库 —— 和风文档说得很清楚：
API Host 本身就是身份认证的一部分。

规则很简单：**源码里只允许出现一眼可辨的假值**。这个测试扫两类形状：

* 32 位十六进制（和风 API Key 的形态）
* ``xxxxxx.re.qweatherapi.com`` / ``xxxxxx.qweatherapi.com``（真实 API Host 的形态）

命中就失败，并在信息里给出文件与行号。假值走 ``ALLOWED`` 白名单。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 只扫文本类文件
SUFFIXES = frozenset(
    {".py", ".js", ".html", ".css", ".md", ".toml", ".json", ".json5", ".spec", ".txt", ".bat", ".yml", ".yaml"}
)

#: 不扫的目录（产物、依赖、运行时、本地数据）
SKIP_DIRS = frozenset({".git", ".venv", "venv", "dist", "build", "runtime", "node_modules", "__pycache__", ".idea"})

#: 明文允许的**假**值
ALLOWED = frozenset(
    {
        "demo.re.qweatherapi.com",
        "demo1234.re.qweatherapi.com",
        "xxxxxx.re.qweatherapi.com",
        "xxxxxx.qweatherapi.com",
    }
)

PATTERNS = (
    ("32 位十六进制（看起来像 API Key）", re.compile(r"\b[0-9a-f]{32}\b")),
    ("和风 API Host", re.compile(r"\b[a-z0-9]{6,16}\.(?:re\.)?qweatherapi\.com\b")),
    ("看起来像 OpenAI Key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
)


def _iter_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
        except OSError:
            continue
        files.append(path)
    return files


def test_repository_contains_no_real_credentials() -> None:
    findings: list[str] = []
    for path in _iter_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            for label, pattern in PATTERNS:
                for match in pattern.finditer(line):
                    value = match.group(0)
                    if value in ALLOWED:
                        continue
                    rel = path.relative_to(ROOT).as_posix()
                    findings.append(f"{rel}:{line_no}  {label} → {value}")

    assert not findings, (
        "源码/测试里出现了疑似真实凭据，提交前必须换成假值：\n  "
        + "\n  ".join(findings[:20])
    )
