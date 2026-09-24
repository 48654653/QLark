"""NapCat 启动输出特征串。

全部来自《QLark-NapCat部署技术文档》的实测记录，**不要在未实测的情况下改动**。
"""

from __future__ import annotations

import re

#: 快速登录已发起：``[info] 正在快速登录  2157649443``
RE_QUICK_LOGIN = re.compile(r"正在快速登录\s+(\d{5,12})")

#: 登录成功（快速登录与扫码登录共用这一条）
RE_WORKER_LOGGED_IN = re.compile(r"Worker进程已登录成功")

#: 落到二维码登录：``没有 -q 指令指定快速登录，将使用二维码登录方式``
RE_NO_QUICK_LOGIN = re.compile(r"没有\s*-q\s*指令指定快速登录|将使用二维码登录方式")

#: 二维码就绪提示
RE_QRCODE_READY = re.compile(r"请扫描下面的二维码|二维码登录")

#: OneBot HTTP 服务端就绪：``[OneBot] [HTTP Server Adapter] Start On 127.0.0.1:3000``
RE_ONEBOT_START = re.compile(r"\[HTTP Server Adapter\]\s*Start On\s+([\d.]+):(\d+)")

#: NapCat 版本
RE_NAPCAT_VERSION = re.compile(r"Version:\s*([\w.\-]+)")

#: 快速登录候选列表（**内容会浮动，不可作为登录依据**，仅用于日志提示）
RE_QUICK_CANDIDATES = re.compile(r"可用于快速登录 of QQ")

#: 启动失败的常见特征
RE_MODULE_NOT_FOUND = re.compile(r"specified module could not be found|wrapper\.node", re.I)
RE_TOKEN_FAILED = re.compile(r"token verify failed", re.I)

#: 日志级别前缀：``[info]`` / ``[warn]`` / ``[error]`` / ``[debug]``
RE_LEVEL_PREFIX = re.compile(r"^\[(info|warn|warning|error|debug|trace)\]\s*", re.I)

_LEVEL_MAP = {
    "info": "info",
    "warn": "warn",
    "warning": "warn",
    "error": "error",
    "debug": "debug",
    "trace": "debug",
}


def parse_level(text: str, default: str = "info") -> str:
    """从 NapCat 输出行里推断日志级别。"""
    match = RE_LEVEL_PREFIX.match(text)
    if match:
        return _LEVEL_MAP.get(match.group(1).lower(), default)
    lowered = text.lower()
    if "error" in lowered or "失败" in text or "错误" in text:
        return "error"
    if "warn" in lowered or "警告" in text:
        return "warn"
    return default
