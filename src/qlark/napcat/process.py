"""NapCat 进程生命周期管理。

三条实测结论决定了这里的每个设计：

1. ``node.exe ./index.js`` **天然无控制台**，可直接静默 spawn（约束"不能有黑窗口"）。
2. NapCat 有**自愈机制**：杀掉 worker 子进程后父进程会自动重拉。所以停止必须杀
   **父进程树**（``taskkill /PID <父PID> /T /F``）。
3. 启动输出（含大幅二维码字符画）**必须被持续读取**，否则管道缓冲区写满后进程阻塞。
   所以 spawn 之后立刻起一个专职读线程。
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from ..core.models import LogSource
from .launch import LaunchSpec
from .logbus import LogBus

log = logging.getLogger(__name__)

#: 不弹黑窗口
CREATE_NO_WINDOW = 0x08000000
#: 独立进程组，便于整组终止
CREATE_NEW_PROCESS_GROUP = 0x00000200

_IS_WINDOWS = os.name == "nt"


class NapCatProcess:
    """管理一个 NapCat 进程（父进程 + 其 worker 子树）。"""

    def __init__(
        self,
        spec: LaunchSpec,
        logbus: LogBus,
        *,
        on_line: Callable[[str], None] | None = None,
        on_exit: Callable[[int | None], None] | None = None,
    ) -> None:
        self.spec = spec
        self.logbus = logbus
        self._on_line = on_line
        self._on_exit = on_exit
        self._proc: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._lock = threading.RLock()
        self._started_at: float = 0.0
        self._stopping = False
        self._exit_handled = True

    # ------------------------------------------------------------------ 属性
    @property
    def pid(self) -> int | None:
        with self._lock:
            return self._proc.pid if self._proc is not None else None

    @property
    def started_at(self) -> float:
        return self._started_at

    @property
    def returncode(self) -> int | None:
        with self._lock:
            return self._proc.returncode if self._proc is not None else None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    @property
    def uptime(self) -> int:
        if not self._started_at:
            return 0
        return max(0, int(time.time() - self._started_at))

    # ------------------------------------------------------------------ 启动
    def start(self) -> int:
        """静默启动 NapCat，返回父进程 PID。"""
        with self._lock:
            if self.running:
                assert self._proc is not None
                return self._proc.pid

            exe = self.spec.executable
            if not exe.exists():
                raise FileNotFoundError(
                    f"找不到 NapCat 可执行文件：{exe}\n"
                    "请先在「runtime/napcat」下放好 NapCat 运行时（见技术文档步骤 1~4）。"
                )
            if not self.spec.cwd.is_dir():
                raise NotADirectoryError(f"NapCat 工作目录不存在：{self.spec.cwd}")

            env = os.environ.copy()
            env.update(self.spec.env)
            # Node 与 Python 都认这两个；保证输出实时可读、且按 UTF-8 编码。
            # 真实 NapCat 是 Node，stdout 恒为 UTF-8；这里显式声明是为了让任何
            # 语言的子进程都与我们的解码方式一致。
            env.setdefault("PYTHONUNBUFFERED", "1")
            env.setdefault("PYTHONIOENCODING", "utf-8")

            creationflags = 0
            startupinfo = None
            if _IS_WINDOWS:
                creationflags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE

            self._stopping = False
            self._exit_handled = False
            argv = self.spec.argv()
            self.logbus.add(
                f"启动 NapCat：{self.spec.describe()}（cwd={self.spec.cwd}）",
                source=LogSource.QLARK.value,
            )
            self._proc = subprocess.Popen(
                argv,
                cwd=str(self.spec.cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
                startupinfo=startupinfo,
                close_fds=True,
            )
            self._started_at = time.time()
            self._reader = threading.Thread(
                target=self._read_loop,
                name="napcat-stdout",
                daemon=True,
            )
            self._reader.start()
            return self._proc.pid

    # ------------------------------------------------------------------ 读线程
    def _read_loop(self) -> None:
        """持续消费 stdout。

        这是**必须**的：NapCat 启动时会打印大幅二维码字符画，不读就会把管道写满，
        进程随即阻塞（技术文档坑 23）。
        """
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                self.logbus.add(line, source=LogSource.NAPCAT.value)
                if self._on_line is not None:
                    try:
                        self._on_line(line)
                    except Exception:  # 回调异常不能拖死读线程
                        log.exception("NapCat 输出回调异常")
        except Exception:  # pragma: no cover - 进程被杀时读管道会抛异常
            pass
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            code = proc.poll()
            if not self._exit_handled:
                self._exit_handled = True
                self.logbus.add(
                    f"NapCat 进程已退出（returncode={code}{'，主动停止' if self._stopping else ''}）",
                    level="info" if self._stopping else "warn",
                    source=LogSource.QLARK.value,
                )
                if self._on_exit is not None:
                    try:
                        self._on_exit(code)
                    except Exception:
                        log.exception("NapCat 退出回调异常")

    # ------------------------------------------------------------------ 停止
    def stop(self, timeout: float = 12.0) -> None:
        """终止 NapCat **父进程树**。硬杀即可，无需优雅退出（技术文档 §9.2）。"""
        with self._lock:
            proc = self._proc
            if proc is None:
                return
            self._stopping = True
            pid = proc.pid
            if proc.poll() is not None:
                self._proc = None
                return

        # 先请求终止整棵树
        kill_tree(pid)

        deadline = time.time() + timeout
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.1)

        if proc.poll() is None:
            self.logbus.add(
                f"进程树未在 {timeout:.0f}s 内退出，强制 kill（pid={pid}）",
                level="warn",
                source=LogSource.QLARK.value,
            )
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass

        reader = self._reader
        if reader is not None and reader.is_alive():
            reader.join(timeout=3.0)

        with self._lock:
            self._proc = None
            self._reader = None
            self._started_at = 0.0


# --------------------------------------------------------------------------- #
# 进程树
# --------------------------------------------------------------------------- #
def kill_tree(pid: int) -> bool:
    """杀掉以 ``pid`` 为根的整棵进程树。

    只杀 worker 子进程没有意义——NapCat 父进程会自动把它拉回来。
    """
    if not _IS_WINDOWS:  # pragma: no cover - 项目只支持 Windows
        try:
            os.kill(pid, 9)
            return True
        except OSError:
            return False

    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
        return result.returncode == 0
    except Exception:
        log.exception("taskkill 执行失败 pid=%s", pid)
        return False


def _safe_psutil():  # pragma: no cover - 依赖存在性由调用方判断
    try:
        import psutil  # type: ignore

        return psutil
    except ImportError:
        return None


def iter_napcat_processes(
    napcat_dir: Path,
    exe: Path,
) -> list[tuple[int, str]]:
    """找出属于本项目 NapCat 运行时的残留进程（用于孤儿清理）。

    **只匹配本项目的进程**：可执行文件路径等于我们的 ``node.exe``，或工作目录
    等于 ``runtime/napcat`` 且进程名是 ``node.exe``。绝不按进程名盲杀 —— 机器上
    很可能还有别的 Node 程序在跑。
    """
    psutil = _safe_psutil()
    if psutil is None:
        return []

    target_exe = str(exe).lower()
    target_dir = str(napcat_dir).lower()
    found: list[tuple[int, str]] = []
    own_pid = os.getpid()

    for proc in psutil.process_iter(["pid", "name", "exe", "cwd", "cmdline"]):
        try:
            if proc.pid == own_pid:
                continue
            info = proc.info
            name = (info.get("name") or "").lower()
            if name not in ("node.exe", "node"):
                continue
            exe_path = (info.get("exe") or "").lower()
            cwd_path = ""
            try:
                cwd_path = (proc.cwd() or "").lower()
            except Exception:
                cwd_path = ""

            matched = False
            if exe_path and exe_path == target_exe:
                matched = True
            elif cwd_path and cwd_path == target_dir:
                matched = True

            if matched:
                cmdline = " ".join(info.get("cmdline") or [])[:200]
                found.append((proc.pid, cmdline))
        except Exception:
            continue
    return found


def kill_orphans(napcat_dir: Path, exe: Path, *, exclude_pids: set[int] | None = None) -> list[int]:
    """清理上次崩溃残留的 NapCat 进程。

    残留进程会占住 3000 / 6099 端口，导致本次启动失败。
    """
    exclude = exclude_pids or set()
    killed: list[int] = []
    for pid, _cmdline in iter_napcat_processes(napcat_dir, exe):
        if pid in exclude:
            continue
        if kill_tree(pid):
            killed.append(pid)
    return killed


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    psutil = _safe_psutil()
    if psutil is not None:
        try:
            return psutil.pid_exists(pid)
        except Exception:
            return False
    try:  # pragma: no cover
        os.kill(pid, 0)
        return True
    except OSError:
        return False
