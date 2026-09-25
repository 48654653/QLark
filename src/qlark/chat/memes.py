"""表情包库：从目录里扫描可用表情，按情绪随机挑一张。

目录结构**两种都支持**（用户很自然会先丢一张图进来试）：

.. code-block:: text

    memes/
    ├── 发怒.png          ← 散放的文件：文件名（去扩展名）就是情绪
    ├── 开心.gif
    ├── angry/            ← 子目录：目录名是情绪，里面可以放多张
    │   ├── 1.png
    │   └── 2.gif
    └── shy/
        └── 捂脸.png

一个情绪有多张图时**随机挑一张** —— 每次都是同一张图会非常假。

目录路径由调用方给（设置项可改），默认取 ``<程序目录>/memes``。
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: 能直接发出去的图片格式
SUPPORTED_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})

#: 单个情绪最多保留多少张候选（防止某个目录里塞了几千张把扫描拖慢）
MAX_PER_EMOTION = 200
#: 最多识别多少个情绪（提示词里要列出来，太多会稀释模型的选择质量）
MAX_EMOTIONS = 100


@dataclass(frozen=True)
class MemeItem:
    """一个可选的表情。"""

    emotion: str
    path: Path

    @property
    def display(self) -> str:
        return self.emotion


class MemeLibrary:
    """一个目录对应一个表情库。每次读取都重新扫描（用户随时会加图）。"""

    def __init__(self, root: Path | str | None) -> None:
        self.root = Path(root).expanduser() if root else None

    # ------------------------------------------------------------------ 扫描
    @property
    def available(self) -> bool:
        return self.root is not None and self.root.is_dir()

    def scan(self) -> dict[str, list[Path]]:
        """返回 ``{情绪: [图片路径, ...]}``。目录不存在或没有可用图片时返回空字典。"""
        if not self.available:
            return {}
        assert self.root is not None

        table: dict[str, list[Path]] = {}
        try:
            entries = sorted(self.root.iterdir())
        except OSError as exc:
            log.warning("读取表情目录失败：%s（%s）", self.root, exc)
            return {}

        for entry in entries:
            try:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    emotion = entry.name.strip()
                    files = self._images_in(entry)
                elif entry.suffix.lower() in SUPPORTED_SUFFIXES:
                    # 散放的文件：文件名就是情绪
                    emotion = entry.stem.strip()
                    files = [entry]
                else:
                    continue
            except OSError as exc:  # 单个条目坏了不该让整次扫描失败
                log.warning("跳过表情条目 %s：%s", entry, exc)
                continue

            if not emotion or not files:
                continue
            bucket = table.setdefault(emotion, [])
            for path in files:
                if len(bucket) >= MAX_PER_EMOTION:
                    break
                if path not in bucket:
                    bucket.append(path)

        if len(table) > MAX_EMOTIONS:
            kept = dict(sorted(table.items())[:MAX_EMOTIONS])
            log.warning(
                "表情情绪过多（%d 个），只取前 %d 个：%s",
                len(table), MAX_EMOTIONS, "、".join(kept),
            )
            table = kept
        return table

    def _images_in(self, folder: Path) -> list[Path]:
        files: list[Path] = []
        for path in sorted(folder.iterdir()):
            try:
                if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
                    files.append(path)
            except OSError:
                continue
        return files

    # ------------------------------------------------------------------ 查询
    def catalog(self) -> dict[str, int]:
        """``{情绪: 张数}``，按名字排序。给界面和提示词用。"""
        return {name: len(paths) for name, paths in sorted(self.scan().items())}

    def emotions(self) -> list[str]:
        return list(self.catalog())

    def is_empty(self) -> bool:
        return not self.catalog()

    def pick(self, emotion: str, *, rng: random.Random | None = None) -> MemeItem | None:
        """按情绪名挑一张。

        **匹配是宽松的**：忽略大小写、忽略首尾空白、也认带斜杠的写法
        （QQ 里表情习惯写成 ``/发怒``）。匹配不到返回 ``None`` —— 调用方应当
        静默跳过并记日志，绝不能因为模型编了个不存在的情绪就报错。
        """
        wanted = _normalize(emotion)
        if not wanted:
            return None
        table = self.scan()
        if not table:
            return None

        exact = {_normalize(name): name for name in table}
        matched = exact.get(wanted)
        if matched is None:
            # 再宽松一点：允许 "哭" 命中 "哭哭"，反过来也行
            for key, name in exact.items():
                if key and (key in wanted or wanted in key):
                    matched = name
                    break
        if matched is None:
            return None

        choices = table[matched]
        source = rng or random
        return MemeItem(emotion=matched, path=source.choice(choices))

    def describe(self) -> str:
        """给日志/界面用的一句话概况。"""
        if not self.available:
            return f"表情目录不存在：{self.root}"
        catalog = self.catalog()
        if not catalog:
            return f"表情目录里没有可用图片：{self.root}"
        return f"{self.root}（{len(catalog)} 个情绪）"


def _normalize(name: str) -> str:
    return str(name or "").strip().lstrip("/").strip().lower()


def resolve_dir(configured: str | None, default_root: Path | None) -> Path | None:
    """决定用哪个目录：设置项优先，没配就用默认位置。"""
    text = str(configured or "").strip()
    if text:
        return Path(text).expanduser()
    return default_root


__all__ = [
    "MAX_EMOTIONS",
    "MAX_PER_EMOTION",
    "SUPPORTED_SUFFIXES",
    "MemeItem",
    "MemeLibrary",
    "resolve_dir",
]
