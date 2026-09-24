"""登录二维码文件轮询。

实测坑（技术文档 7.4 / 9.5）：

* ``cache/qrcode.png`` **登录成功后不会被删除**，会残留上次的过期图 —— 所以启动
  NapCat 前必须先删文件，之后还要用 ``mtime > 进程启动时间`` 把旧图排除掉。
* 二维码**每约 121 秒被覆盖重写**同名文件 —— 所以必须监听 mtime 变化并重新读取，
  不能静态展示一次。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from ..core.models import QrSnapshot
from .logbus import png_to_data_url

log = logging.getLogger(__name__)

#: mtime 容差：兼容文件系统时间戳精度与进程启动写日志的先后差
_MTIME_TOLERANCE = 2.0
#: 单张二维码体积上限（正常约 600 字节）
_MAX_BYTES = 2 * 1024 * 1024


class QrCodeWatcher:
    """监听二维码文件并输出可直接给前端用的快照。"""

    def __init__(self, path: Path, *, armed_at: float = 0.0) -> None:
        self.path = path
        self._armed_at = armed_at
        self._snapshot: QrSnapshot | None = None
        self._last_mtime: float = 0.0

    # ------------------------------------------------------------------ 控制
    def clear(self) -> bool:
        """删除残留的二维码文件（启动 NapCat **之前**必须调用）。"""
        self._snapshot = None
        self._last_mtime = 0.0
        try:
            if self.path.is_file():
                self.path.unlink()
                log.info("已清理残留二维码：%s", self.path)
                return True
        except OSError as exc:
            log.warning("清理二维码失败：%s", exc)
        return False

    def arm(self, after: float) -> None:
        """以 ``after`` 为界，只认这个时间之后生成的二维码。"""
        self._armed_at = after
        self._snapshot = None
        self._last_mtime = 0.0

    def reset(self) -> None:
        self._snapshot = None
        self._last_mtime = 0.0

    # ------------------------------------------------------------------ 读取
    @property
    def current(self) -> QrSnapshot | None:
        return self._snapshot

    def poll(self) -> QrSnapshot | None:
        """检查一次文件；有变化时返回新快照，否则返回缓存（可能为 ``None``）。"""
        try:
            stat = self.path.stat()
        except OSError:
            return None

        if not self.path.is_file():
            return None
        if stat.st_size <= 0 or stat.st_size > _MAX_BYTES:
            return None
        # 坑 21：登录成功后残留的旧图，mtime 早于本次进程启动
        if stat.st_mtime < self._armed_at - _MTIME_TOLERANCE:
            return None
        if stat.st_mtime == self._last_mtime and self._snapshot is not None:
            return self._snapshot

        data = self._read_bytes()
        if data is None:
            return self._snapshot

        self._last_mtime = stat.st_mtime
        self._snapshot = QrSnapshot(data_url=png_to_data_url(data), mtime=stat.st_mtime)
        return self._snapshot

    def _read_bytes(self, attempts: int = 3) -> bytes | None:
        """读文件；文件正在被重写时可能拿到半截内容，重试即可。"""
        for index in range(attempts):
            try:
                data = self.path.read_bytes()
                if data.startswith(b"\x89PNG"):
                    return data
            except OSError:
                pass
            if index + 1 < attempts:
                time.sleep(0.15)
        return None
