"""``onebot11_<QQ号>.json`` 的读取与合并写入。

三条硬约束（全部来自实测）：

1. **不能走 WebUI API** —— 它的鉴权是基于时间的 HMAC 动态密钥，静态 token 调不通。
   直接读写配置文件才是可行路径。
2. **必须合并写入，不能整体覆盖** —— 文件里还有 ``timeout``、``musicSignUrl`` 等字段，
   覆盖会丢掉用户自己的配置。
3. **改完必须重启 NapCat** —— 配置文件不支持热重载。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import paths
from ..version import DEFAULT_EVENT_PORT, DEFAULT_ONEBOT_PORT, DEFAULT_ONEBOT_TOKEN

log = logging.getLogger(__name__)

#: 我们在 ``httpServers`` 里的条目名，用于幂等识别
SERVER_NAME = "qlark"

#: 网络段为空的初始形态
EMPTY_NETWORK: dict[str, Any] = {
    "httpServers": [],
    "httpSseServers": [],
    "httpClients": [],
    "websocketServers": [],
    "websocketClients": [],
    "plugins": [],
}

_NETWORK_KEYS = tuple(EMPTY_NETWORK.keys())

#: 账号相关配置文件名：``napcat_<QQ>.json`` 与 ``napcat_protocol_<QQ>.json``
_ACCOUNT_FILE_RE = re.compile(r"^napcat_(?:protocol_)?(\d{5,12})$")


def parse_account_stem(stem: str) -> str | None:
    """从文件名（去扩展名）解析出 QQ 号，不是账号文件则返回 ``None``。

    ``napcat.json``（基础设置）必须被排除掉。
    """
    match = _ACCOUNT_FILE_RE.match(stem)
    return match.group(1) if match else None


# --------------------------------------------------------------------------- #
# JSON5 容错读取
# --------------------------------------------------------------------------- #
def strip_json5(text: str) -> str:
    """把 JSON5 文本规整成标准 JSON。

    NapCat 自 v4.4.14 起用 JSON5 解析配置，所以文件里**可能**有注释和尾随逗号。
    这里用状态机剥离，避免误伤字符串里出现的 ``//``。
    """
    if text.startswith("\ufeff"):
        text = text[1:]

    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    quote = ""
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                in_string = False
            i += 1
            continue

        if ch in ('"', "'"):
            in_string = True
            quote = ch
            out.append(ch)
            i += 1
            continue

        if ch == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                while i < n and text[i] not in "\r\n":
                    i += 1
                continue
            if nxt == "*":
                i += 2
                while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                    i += 1
                i += 2
                continue

        out.append(ch)
        i += 1

    result = "".join(out)
    # 尾随逗号： ``{"a":1,}`` → ``{"a":1}``
    result = re.sub(r",(\s*[}\]])", r"\1", result)
    return result


def load_json_tolerant(path: Path) -> dict[str, Any]:
    """读取配置；文件不存在返回空字典，内容损坏则抛 :class:`ValueError`。"""
    if not path.is_file():
        return {}
    raw = path.read_text(encoding="utf-8", errors="replace")
    if not raw.strip():
        return {}
    try:
        return json.loads(strip_json5(raw))
    except ValueError as exc:
        raise ValueError(f"配置文件不是合法 JSON/JSON5：{path}（{exc}）") from exc


# --------------------------------------------------------------------------- #
# 路径与判定
# --------------------------------------------------------------------------- #
def config_dir(napcat_dir: Path | None = None) -> Path:
    root = napcat_dir or paths.napcat_dir()
    return root / "napcat" / "config"


def onebot_config_path(qq: str, napcat_dir: Path | None = None) -> Path:
    return config_dir(napcat_dir) / f"onebot11_{qq}.json"


def account_file_mtimes(napcat_dir: Path | None = None) -> dict[str, float]:
    """记录当前所有账号相关配置文件的修改时间（用于"本次是否新写过"的精确比对）。

    同时跟踪两类文件，取较大者：

    * ``napcat_<QQ>.json`` —— **只在账号首次注册时写入**，后续登录不会重写
    * ``napcat_protocol_<QQ>.json`` —— 每次登录都会重新生成

    只跟踪前者会导致"第二次扫码登录"被漏判。
    """
    directory = config_dir(napcat_dir)
    if not directory.is_dir():
        return {}
    result: dict[str, float] = {}
    for item in directory.glob("napcat_*.json"):
        qq = parse_account_stem(item.stem)
        if qq is None:
            continue
        try:
            mtime = item.stat().st_mtime
        except OSError:
            continue
        if mtime > result.get(qq, 0.0):
            result[qq] = mtime
    return result


def detect_accounts(
    napcat_dir: Path | None = None,
    *,
    baseline: dict[str, float] | None = None,
    since: float = 0.0,
) -> list[str]:
    """推断**本次**已登录的账号。

    ``config/napcat_<QQ>.json`` 系列文件是登录成功的信号，但它们**不会被清理**，
    且 ``napcat_<QQ>.json`` 只在首次注册时写入：上次登录留下的文件会让"正在扫码"
    的流程误判为已登录（返回旧账号），而"第二次扫码"又会因为文件没变化而被漏判。

    两种判据：

    * ``baseline`` —— 进程启动前的 ``{qq: mtime}`` 快照，只认**新出现或 mtime 变化**的
      文件。精确判据。
    * ``since`` —— 只认 mtime 晚于该时刻的文件。存在时间容差，仅作兜底。

    .. note::
       扫码模式下**最可靠**的判据是直接问 OneBot 接口要 ``get_login_info``
       （见 :meth:`qlark.napcat.login.LoginManager._probe_account`）。本函数只覆盖
       "账号首次注册、OneBot 网络还没配置"的那一次。
    """
    directory = config_dir(napcat_dir)
    if not directory.is_dir():
        return []

    preexisting = baseline or {}
    found: dict[str, float] = {}
    for item in directory.glob("napcat_*.json"):
        qq = parse_account_stem(item.stem)
        if qq is None:
            continue
        try:
            mtime = item.stat().st_mtime
        except OSError:
            continue

        if baseline is not None:
            if mtime <= preexisting.get(qq, 0.0):
                continue  # 启动前就存在且没有变化 → 上一次登录的残留
        elif since and mtime < since - 1.0:
            continue
        found[qq] = max(found.get(qq, 0.0), mtime)
    return sorted(found)


def _enabled_entries(cfg: dict[str, Any]) -> list[str]:
    network = cfg.get("network")
    if not isinstance(network, dict):
        return []
    hits: list[str] = []
    for key in _NETWORK_KEYS:
        entries = network.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and entry.get("enable"):
                hits.append(f"{key}:{entry.get('name') or entry.get('url') or entry.get('port')}")
    return hits


def network_is_configured(cfg: dict[str, Any]) -> bool:
    """是否已有任一启用中的网络模型。"""
    return bool(_enabled_entries(cfg))


def describe_network(cfg: dict[str, Any]) -> str:
    hits = _enabled_entries(cfg)
    return "、".join(hits) if hits else "（无启用项）"


# --------------------------------------------------------------------------- #
# 合并写入
# --------------------------------------------------------------------------- #
@dataclass
class NetConfigResult:
    path: Path
    changed: bool
    created: bool = False
    backup: Path | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "changed": self.changed,
            "created": self.created,
            "backup": str(self.backup) if self.backup else None,
            "reason": self.reason,
        }


def build_server_entry(host: str, port: int, token: str) -> dict[str, Any]:
    """动作通道：QLark 主动 POST 到 NapCat 发消息（技术文档步骤 7 的实测配置）。"""
    return {
        "name": SERVER_NAME,
        "enable": True,
        "port": int(port),
        "host": host,
        "enableCors": True,
        "enableWebsocket": True,
        "messagePostFormat": "array",
        "token": token,
        "debug": False,
        "reportSelfMessage": False,
    }


def build_client_entry(host: str, port: int, token: str) -> dict[str, Any]:
    """事件通道：NapCat 主动把消息事件 POST 到 QLark。

    **没有这一条就收不到任何消息** —— HTTP 服务端模式是单工的，只能我们主动调它。
    """
    return {
        "name": SERVER_NAME,
        "enable": True,
        "url": f"http://{host}:{int(port)}",
        "messagePostFormat": "array",
        "reportSelfMessage": False,
        "token": token,
        "debug": False,
    }


def entry_port(entry: dict[str, Any]) -> int | None:
    """从条目里取端口：服务端看 ``port``，客户端从 ``url`` 解析。"""
    raw_port = entry.get("port")
    if raw_port is not None:
        try:
            return int(raw_port)
        except (TypeError, ValueError):
            return None
    url = entry.get("url")
    if url:
        try:
            from urllib.parse import urlparse

            parsed = urlparse(str(url))
            return parsed.port
        except (TypeError, ValueError):
            return None
    return None


def _entry_matches(entry: dict[str, Any], wanted: dict[str, Any]) -> bool:
    """判断已有条目是不是"我们要替换的那一条"。"""
    name = wanted.get("name")
    if name and entry.get("name") == name:
        return True
    if "url" in wanted:
        return entry.get("url") == wanted.get("url")
    return entry.get("host") == wanted.get("host") and entry_port(entry) == entry_port(wanted)


def _merge_entries(
    cfg: dict[str, Any],
    wanted: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, Any], list[str]]:
    """把各项条目合并进 ``network``，返回 (新配置, 变更原因列表)。

    只改 ``network`` 下我们关心的几个数组，其余字段原样保留。
    """
    merged = json.loads(json.dumps(cfg))  # 深拷贝，避免污染调用方
    network = merged.setdefault("network", {})
    if not isinstance(network, dict):
        network = {}
        merged["network"] = network
    for key, default in EMPTY_NETWORK.items():
        if not isinstance(network.get(key), list):
            network[key] = list(default)

    reasons: list[str] = []
    for key, entries in wanted.items():
        bucket = network[key]
        for item in entries:
            index = next(
                (i for i, existing in enumerate(bucket) if isinstance(existing, dict) and _entry_matches(existing, item)),
                None,
            )
            if index is None:
                bucket.append(item)
                reasons.append(f"新增 {key} 条目 {item.get('name')}")
            elif bucket[index] != item:
                bucket[index] = item
                reasons.append(f"更新 {key} 条目 {item.get('name')}")
    return merged, reasons


def qlark_entries_ok(
    cfg: dict[str, Any],
    *,
    action_port: int = DEFAULT_ONEBOT_PORT,
    event_port: int = DEFAULT_EVENT_PORT,
) -> bool:
    """两条通道是否都已按我们的端口配好（缺任何一条都要重写并重启）。

    注意：M1 时代只写 ``httpServers``，那种旧配置在这里会被判为"未配好"，
    于是自动补上 ``httpClients`` 并重启一次 —— 这正是升级时想要的行为。
    """
    network = cfg.get("network")
    if not isinstance(network, dict):
        return False

    def enabled(key: str) -> list[dict[str, Any]]:
        return [
            entry
            for entry in (network.get(key) or [])
            if isinstance(entry, dict) and entry.get("enable") and entry.get("name") == SERVER_NAME
        ]

    servers = enabled("httpServers")
    clients = enabled("httpClients")
    if not servers or not clients:
        return False
    if entry_port(servers[0]) != int(action_port):
        return False
    return entry_port(clients[0]) == int(event_port)


def ensure_network_config(
    qq: str,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_ONEBOT_PORT,
    event_host: str = "127.0.0.1",
    event_port: int = DEFAULT_EVENT_PORT,
    token: str = DEFAULT_ONEBOT_TOKEN,
    napcat_dir: Path | None = None,
    write: bool = True,
) -> NetConfigResult:
    """确保 ``onebot11_<QQ>.json`` 里动作通道与事件通道都已启用。

    Args:
        write: 为 ``False`` 时只做判定，不落盘（用于预检）。
    """
    path = onebot_config_path(qq, napcat_dir)
    created = not path.is_file()
    try:
        cfg = load_json_tolerant(path)
    except ValueError as exc:
        log.error("读取 OneBot 配置失败：%s", exc)
        return NetConfigResult(path=path, changed=False, reason=str(exc))

    wanted = {
        "httpServers": [build_server_entry(host, port, token)],
        "httpClients": [build_client_entry(event_host, event_port, token)],
    }
    merged, reasons = _merge_entries(cfg, wanted)

    if not reasons:
        return NetConfigResult(path=path, changed=False, reason="网络配置已就绪，无需改动")

    reason = "；".join(reasons)
    if not write:
        return NetConfigResult(path=path, changed=True, created=created, reason=reason)

    path.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if path.is_file():
        backup = path.with_suffix(f".json.bak.{int(time.time())}")
        try:
            shutil.copy2(path, backup)
        except OSError:
            backup = None

    _atomic_write_json(path, merged)
    log.info("OneBot 网络配置已写入：%s（%s）", path, reason)
    return NetConfigResult(path=path, changed=True, created=created, backup=backup, reason=reason)


def is_configured_for(
    qq: str,
    napcat_dir: Path | None = None,
    *,
    action_port: int = DEFAULT_ONEBOT_PORT,
    event_port: int = DEFAULT_EVENT_PORT,
) -> bool:
    """该账号的动作通道与事件通道是否都已按当前设置配好。

    用来判断"登录成功后要不要写配置 + 重启"：已配好就跳过等待，直接进就绪流程。
    """
    try:
        cfg = load_json_tolerant(onebot_config_path(qq, napcat_dir))
    except ValueError:
        return False
    return qlark_entries_ok(cfg, action_port=action_port, event_port=event_port)


def _atomic_write_json(path: Path, data: dict[str, Any], *, attempts: int = 6) -> None:
    """原子写：先写临时文件再替换，避免断电/崩溃留下半截 JSON。

    Windows 上 ``os.replace`` 在目标文件被别的进程打开时会直接抛
    ``PermissionError(WinError 5)``。这不是异常情况——**NapCat 自己就会读这个文件**，
    杀毒软件、索引服务也可能临时占用。所以要退避重试，不能一次失败就放弃。
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(data, ensure_ascii=False, indent=2)
    tmp.write_text(text, encoding="utf-8")

    last_error: OSError | None = None
    for index in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.1 * (index + 1))
    tmp.unlink(missing_ok=True)
    raise OSError(f"写入 OneBot 配置失败：{path}（{last_error}）")
