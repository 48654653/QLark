"""启动 / 登录状态机。

流程（对应需求规格 v1.1 §8.1）：

.. code-block:: text

    环境自检 → 清理孤儿进程 → 已保存账号？
        ├ 有 → 删二维码残留 → spawn ... -q <QQ号> → 快速登录
        │        └ 失败（NapCat 自己回落到二维码）→ 扫码
        └ 无 → spawn（不带 -q）→ 扫码
    → 登录成功 → 写 OneBot 网络配置
        ├ 有改动 → 重启 NapCat → 再登录
        └ 无改动 → 跳过
    → 轮询 3000 端口 → get_status / get_login_info 验证 → READY

关键点：**扫码成功不等于可用**。协议端默认一个端口都不开（技术文档 §3.3），必须写完
``onebot11_<QQ>.json`` 并重启 NapCat，机器人才能收发消息。
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from typing import Any, Callable

from .. import paths
from ..core.models import EnvReport, Phase, QrSnapshot
from ..onebot.client import OneBotClient, OneBotError
from ..settings import Settings
from . import netconfig, patterns
from .envcheck import EnvChecker
from .launch import build_launch_spec
from .logbus import LogBus
from .process import NapCatProcess, kill_orphans
from .qrcode import QrCodeWatcher

log = logging.getLogger(__name__)

#: 已有账号时等待快速登录的上限
QUICK_LOGIN_TIMEOUT = 60.0
#: 扫码登录的等待上限（用户要掏手机）
SCAN_LOGIN_TIMEOUT = 300.0
#: 等待 OneBot 端口就绪的上限
ONEBOT_READY_TIMEOUT = 60.0
#: 进程意外退出后的自动重启次数上限
MAX_AUTO_RESTARTS = 3
#: 登录成功信号出现后，再等配置文件落盘的宽限时间
ACCOUNT_FILE_GRACE = 8.0
#: 登录成功后、重启 NapCat 前等待多久，让会话 / 凭据落盘。
#: 实测：登录成功 0.6 秒后就杀进程，紧接着的快速登录会被 QQ 要求"手Q验证"，
#: 用户被迫二次扫码。
LOGIN_SETTLE_SECONDS = 5.0
#: 扫码模式下向 OneBot 探测"当前登录的是谁"的间隔与超时
ACCOUNT_PROBE_INTERVAL = 2.0
ACCOUNT_PROBE_TIMEOUT = 1.5


class LoginError(RuntimeError):
    """启动 / 登录流程中的可预期失败。"""


def settle_seconds() -> float:
    """重启前的落盘等待时间（``QLARK_LOGIN_SETTLE`` 可覆盖，供测试提速）。"""
    raw = os.environ.get("QLARK_LOGIN_SETTLE")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return LOGIN_SETTLE_SECONDS


class _Aborted(RuntimeError):
    """用户主动停止，不算错误。"""


class LoginManager:
    """把 NapCat 从"未启动"带到"可收发消息"的全过程编排。"""

    def __init__(
        self,
        settings: Settings,
        logbus: LogBus,
        *,
        on_change: Callable[[], None] | None = None,
        on_ready: Callable[[], None] | None = None,
        on_stopped: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self.logbus = logbus
        self._on_change = on_change or (lambda: None)
        self._on_ready = on_ready or (lambda: None)
        self._on_stopped = on_stopped or (lambda: None)

        self.phase: Phase = Phase.IDLE
        self.last_error: str | None = None
        self.env: EnvReport = EnvReport()

        self._proc: NapCatProcess | None = None
        self._qr = QrCodeWatcher(paths.napcat_qrcode_path())
        self._line_queue: queue.Queue[str] = queue.Queue()
        self._stop_event = threading.Event()
        self._busy = threading.Lock()
        self._state_lock = threading.RLock()

        self._thread: threading.Thread | None = None
        self._monitor_thread: threading.Thread | None = None
        self._monitor_stop = threading.Event()

        self._logged_in_signal = False
        self._scan_mode = False
        self._onebot_line_port: int | None = None
        self._account_baseline: dict[str, float] = {}
        self._last_probe: float = 0.0
        self._qq: str | None = None
        self._nickname: str | None = None
        self._online = False
        self._auto_restarts = 0

    # ================================================================== 状态
    def _set_phase(self, phase: Phase, *, error: str | None = None) -> None:
        with self._state_lock:
            self.phase = phase
            self.last_error = error
        self.logbus.add(f"阶段：{phase.value}", level="debug")
        self._on_change()

    def _fail(self, message: str) -> None:
        log.warning("启动流程失败：%s", message)
        self.logbus.add(message, level="error")
        self._notify_stopped()
        self._set_phase(Phase.ERROR, error=message)

    def _notify_ready(self) -> None:
        try:
            self._on_ready()
        except Exception:
            log.exception("on_ready 回调异常")

    def _notify_stopped(self) -> None:
        try:
            self._on_stopped()
        except Exception:
            log.exception("on_stopped 回调异常")

    def snapshot(self) -> dict[str, Any]:
        """给 :class:`AppState` 用的字段。"""
        with self._state_lock:
            proc = self._proc
            return {
                "phase": self.phase.value,
                "last_error": self.last_error,
                "qq": self._qq,
                "nickname": self._nickname,
                "online": self._online,
                "onebot_ready": self._online,
                "napcat_running": bool(proc is not None and proc.running),
                "pid": proc.pid if proc is not None else None,
                "uptime_sec": proc.uptime if proc is not None else 0,
                "qrcode": self._qr.current,
                "env": self.env,
            }

    @property
    def qrcode(self) -> QrSnapshot | None:
        return self._qr.current

    @property
    def is_running(self) -> bool:
        proc = self._proc
        return bool(proc is not None and proc.running)

    # ================================================================== 环境自检
    def run_env_check(self, *, with_ports: bool = True, qq: str | None = None) -> EnvReport:
        checker = EnvChecker(
            host=self.settings.onebot_host,
            port=self.settings.onebot_port,
            event_port=self.settings.event_port,
        )
        self.env = checker.run(
            qq=qq if qq is not None else self.settings.qq_account,
            with_ports=with_ports,
        )
        self._on_change()
        return self.env

    def _clean_orphans(self) -> None:
        """清理上次崩溃残留的 NapCat 进程 —— 它们会占住 3000/6099 端口。"""
        try:
            killed = kill_orphans(paths.napcat_dir(), paths.napcat_exe())
        except Exception:
            log.exception("孤儿进程清理失败")
            return
        if killed:
            self.logbus.add(
                f"已清理上次残留的 NapCat 进程：{killed}", level="warn"
            )

    # ================================================================== 对外控制
    def start(self, *, force_scan: bool = False, wait: bool = False) -> None:
        """异步启动（``wait=True`` 时阻塞，仅供测试）。"""
        self._stop_event.clear()
        self._start_monitor()
        self._thread = threading.Thread(
            target=self._run_sequence,
            kwargs={"force_scan": force_scan},
            name="qlark-bringup",
            daemon=True,
        )
        self._thread.start()
        if wait and self._thread is not None:
            self._thread.join()

    def stop(self, *, wait: bool = True) -> None:
        self._stop_event.set()
        self._monitor_stop.set()
        self._notify_stopped()
        self._stop_process()
        if wait:
            for thread in (self._thread, self._monitor_thread):
                if thread is not None and thread.is_alive():
                    thread.join(timeout=10)
        # 监控线程必须确认退出，否则下一次 start() 会因为"旧线程还活着"而不再新建，
        # 导致重启后再也没有掉线监控。
        self._monitor_thread = None
        self._monitor_stop.clear()
        self._online = False
        if self.phase not in (Phase.ERROR, Phase.ENV_ERROR):
            self._set_phase(Phase.STOPPED)
        else:
            self._on_change()

    def restart(self) -> None:
        """重启 NapCat（用于让 NapCat 侧配置生效）。"""
        self.stop(wait=True)
        self.start(force_scan=False)

    def relogin(self) -> None:
        """忘记已保存账号，强制走扫码。"""
        self.settings.qq_account = None
        self.settings.qq_nickname = None
        self.stop(wait=True)
        self.start(force_scan=True)

    def switch_account(self, qq: str) -> None:
        """切换到指定账号。

        本机已登录过该账号（凭据目录存在）时**免扫码**；否则 NapCat 会回落到二维码。
        """
        value = str(qq).strip()
        if not value.isdigit() or not 5 <= len(value) <= 12:
            raise LoginError(f"账号不合法：{qq}")
        self.settings.qq_account = value
        self.settings.qq_nickname = None
        self.logbus.add(f"正在切换到账号 {value}…", level="info")
        self.stop(wait=True)
        self.start(force_scan=False)

    def logout(self) -> None:
        self.stop(wait=True)
        self.settings.qq_account = None
        self.settings.qq_nickname = None
        self._qq = None
        self._nickname = None
        self.logbus.add("已退出登录（本地账号记录已清除）", level="warn")
        self._set_phase(Phase.IDLE)

    # ================================================================== 主流程
    def _run_sequence(self, *, force_scan: bool) -> None:
        if not self._busy.acquire(blocking=False):
            self.logbus.add("已有启动流程在执行，忽略本次请求", level="warn")
            return
        try:
            self._bring_up(force_scan=force_scan)
        except _Aborted:
            self.logbus.add("启动流程已取消", level="info")
        except LoginError as exc:
            self._fail(str(exc))
        except Exception as exc:  # 兜底：任何意外都要显示给用户，不能静默
            log.exception("启动流程异常")
            self._fail(f"启动流程异常：{exc}")
        finally:
            self._busy.release()

    def _bring_up(self, *, force_scan: bool) -> None:
        self._logged_in_signal = False
        self._scan_mode = False
        self._onebot_line_port = None
        self._auto_restarts = 0

        self._set_phase(Phase.ENV_CHECK)
        self._clean_orphans()
        if os.environ.get("QLARK_SKIP_ENV_CHECK") == "1":
            # 仅供测试：跳过环境门禁，让状态机可以在没有真实 NapCat 的机器上跑通
            self.run_env_check(with_ports=True)
            self.logbus.add("已跳过环境自检门禁（QLARK_SKIP_ENV_CHECK=1）", level="warn")
        else:
            report = self.run_env_check(with_ports=True)
            if not report.ok:
                missing = "；".join(
                    f"{item.label}（{item.detail}）" for item in report.failures() if item.critical
                )
                self._set_phase(Phase.ENV_ERROR, error=f"环境自检未通过 —— {missing}")
                return

        saved_qq = None if force_scan else self.settings.qq_account
        if force_scan:
            self.logbus.add("按要求强制使用扫码登录", level="info")

        self._set_phase(Phase.STARTING)
        self._spawn(saved_qq)

        qq = self._await_login(expected_qq=saved_qq)
        self._after_login(qq)

    # ------------------------------------------------------------------ 进程
    def _spawn(self, qq: str | None) -> None:
        # 坑 21：必须先删掉残留二维码，否则界面会显示上次的过期图
        self._qr.clear()
        # 记录启动前的账号文件基线：只有"本次新写/被改写"的文件才算登录成功
        self._account_baseline = netconfig.account_file_mtimes()
        spec = build_launch_spec(qq)
        proc = NapCatProcess(
            spec,
            self.logbus,
            on_line=self._on_line,
            on_exit=self._on_exit,
        )
        pid = proc.start()
        self._proc = proc
        self._qr.arm(proc.started_at)
        self.logbus.add(f"NapCat 已启动，PID={pid}", level="info")
        self._on_change()

    def _stop_process(self) -> None:
        proc = self._proc
        if proc is None:
            return
        proc.stop()
        self._proc = None
        self._qr.reset()
        self._online = False

    def _on_line(self, line: str) -> None:
        self._line_queue.put(line)

    def _on_exit(self, code: int | None) -> None:
        """进程退出回调（读线程里触发）。"""
        self._online = False
        if self._stop_event.is_set():
            self._on_change()
            return
        self._on_change()

    # ------------------------------------------------------------------ 登录
    def _handle_line(self, line: str) -> None:
        if patterns.RE_WORKER_LOGGED_IN.search(line):
            self._logged_in_signal = True
            return
        if patterns.RE_NO_QUICK_LOGIN.search(line):
            if not self._scan_mode:
                self._scan_mode = True
                self.logbus.add("NapCat 回落到二维码登录", level="info")
                self._set_phase(Phase.WAITING_SCAN)
            return
        match = patterns.RE_ONEBOT_START.search(line)
        if match:
            self._onebot_line_port = int(match.group(2))
            return
        # 启动期的致命错误，早点提示用户
        if patterns.RE_MODULE_NOT_FOUND.search(line):
            raise LoginError(
                "NapCat 加载 wrapper.node 失败：缺少 VC++ 运行库或 crypto.dll / ssl.dll"
            )

    def _await_login(self, *, expected_qq: str | None) -> str:
        quick = bool(expected_qq)
        timeout = QUICK_LOGIN_TIMEOUT if quick else SCAN_LOGIN_TIMEOUT
        deadline = time.monotonic() + timeout
        signal_at: float | None = None
        self._set_phase(Phase.QUICK_LOGIN if quick else Phase.WAITING_SCAN)

        while True:
            if self._stop_event.is_set():
                raise _Aborted()
            if time.monotonic() > deadline:
                raise LoginError(
                    "登录超时。若扫码后仍无响应，请查看日志面板中的 NapCat 输出。"
                    if not quick
                    else "快速登录超时，请点击「重新登录」改用扫码方式。"
                )

            proc = self._proc
            if proc is None or not proc.running:
                if self._stop_event.is_set():
                    raise _Aborted()
                raise LoginError("NapCat 进程意外退出，请查看日志面板。")

            # 1) 消费日志
            try:
                line = self._line_queue.get(timeout=0.25)
            except queue.Empty:
                line = None
            if line is not None:
                self._handle_line(line)

            if self._logged_in_signal and signal_at is None:
                signal_at = time.monotonic()

            # 2) 二维码（实时刷新）
            before = self._qr.current
            snapshot = self._qr.poll()
            if snapshot is not None:
                if not self._scan_mode:
                    self._scan_mode = True
                    self.logbus.add("已生成登录二维码，请用手机 QQ 扫码", level="info")
                    self._set_phase(Phase.WAITING_SCAN)
                elif before is None or before.mtime != snapshot.mtime:
                    self._on_change()

            # 3) 账号落盘信号（与启动前的基线比对，排除上次登录的残留文件）
            accounts = netconfig.detect_accounts(baseline=self._account_baseline)
            if accounts:
                if expected_qq and expected_qq in accounts:
                    return expected_qq
                if not expected_qq:
                    return accounts[0]

            # 4) 直接问接口（扫码模式下最可靠：账号首次注册之后文件就不再变化了）
            if not expected_qq and time.monotonic() - self._last_probe > ACCOUNT_PROBE_INTERVAL:
                self._last_probe = time.monotonic()
                probed = self._probe_account()
                if probed:
                    self.logbus.add(f"从 OneBot 接口探测到已登录账号：{probed}", level="debug")
                    return probed

            # 5) 快速登录：日志信号足够（账号已知）
            if expected_qq and self._logged_in_signal:
                return expected_qq

            # 6) 扫码：日志信号有了，但账号还没确认，宽限一会儿
            if signal_at is not None and time.monotonic() - signal_at > ACCOUNT_FILE_GRACE:
                if accounts:
                    return accounts[0]
                raise LoginError(
                    "登录信号已收到，但无法确认登录的是哪个 QQ。"
                    "请到「日志」页查看 NapCat 的输出；若反复出现，请把日志发给我们。"
                )

    def _after_login(self, qq: str) -> None:
        self._qq = qq
        self.settings.qq_account = qq
        self._set_phase(Phase.LOGGED_IN)
        self.logbus.add(f"登录成功：{qq}", level="info")

        already_configured = netconfig.is_configured_for(
            qq,
            action_port=self.settings.onebot_port,
            event_port=self.settings.event_port,
        )
        if not already_configured:
            # 写配置前必须等一会儿，原因有两个：
            # ① 刚登录就硬杀进程，会话/凭据来不及落盘，紧接着的快速登录会被 QQ
            #    要求"手Q验证"，用户被迫扫第二次码（已实测）；
            # ② 给 NapCat 时间把它自己的 onebot11_<QQ>.json 写完，
            #    我们才能做"合并不覆盖"，不丢它原有的字段。
            self._set_phase(Phase.NETWORK_SETUP)
            self.logbus.add(
                f"等待 {settle_seconds():.0f} 秒让 NapCat 写完配置并落盘登录状态…",
                level="debug",
            )
            if self._stop_event.wait(settle_seconds()):
                raise _Aborted()

        result = netconfig.ensure_network_config(
            qq,
            host=self.settings.onebot_host,
            port=self.settings.onebot_port,
            event_host=self.settings.event_host,
            event_port=self.settings.event_port,
            token=self.settings.onebot_token,
        )

        if result.changed:
            self._set_phase(Phase.NETWORK_SETUP)
            self.logbus.add(f"OneBot 网络配置：{result.reason}", level="info")
            if result.backup:
                self.logbus.add(f"原配置已备份到 {result.backup.name}", level="debug")
            # 配置文件不支持热重载，必须重启 NapCat 才能让端口起来
            self._set_phase(Phase.RESTARTING)
            self._stop_process()
            self.settings.restart_required = False
            self._spawn(qq)
            self._logged_in_signal = False
            self._scan_mode = False
            self._await_login(expected_qq=qq)
            self._set_phase(Phase.LOGGED_IN)
        else:
            self.logbus.add(f"OneBot 网络配置：{result.reason}", level="debug")

        self._wait_onebot_ready()
        self._verify_online()
        self._set_phase(Phase.READY)
        self.logbus.add("QLark 已就绪，可以开始收发消息", level="info")
        self._notify_ready()

    def _wait_onebot_ready(self) -> None:
        client = self._client()
        deadline = time.monotonic() + ONEBOT_READY_TIMEOUT
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                raise _Aborted()
            proc = self._proc
            if proc is None or not proc.running:
                raise LoginError("等待 OneBot 端口就绪时 NapCat 已退出")
            try:
                client.call("get_status")
                self.logbus.add(
                    f"OneBot 服务端已就绪：{self.settings.onebot_base_url}", level="info"
                )
                return
            except OneBotError:
                time.sleep(0.8)
        raise LoginError(
            f"OneBot 端口 {self.settings.onebot_port} 在 {ONEBOT_READY_TIMEOUT:.0f}s 内未就绪"
        )

    def _verify_online(self) -> None:
        client = self._client()
        try:
            info = client.get_login_info()
        except OneBotError as exc:
            raise LoginError(f"OneBot 鉴权或接口调用失败：{exc}") from exc
        data = info.get("data") or {}
        if data.get("user_id"):
            self._qq = str(data["user_id"])
            self.settings.qq_account = self._qq
        if data.get("nickname"):
            self._nickname = str(data["nickname"])
            self.settings.qq_nickname = self._nickname
        self._online = True

    def _client(self, timeout: float | None = None) -> OneBotClient:
        return OneBotClient(
            self.settings.onebot_base_url,
            self.settings.onebot_token,
            timeout=timeout or 10.0,
        )

    def _probe_account(self) -> str | None:
        """直接问 OneBot "现在登录的是哪个账号"。

        这是扫码模式下**最可靠**的判据：只要该账号的 OneBot 网络配置已经存在，
        端口就会在登录后起来，NapCat 会如实回答 ``get_login_info``。

        为什么不能只靠配置文件：``napcat_<QQ>.json`` **只在账号首次注册时写入**，
        之后的登录不会重写它 —— 只盯文件变化会把"第二次扫码登录"判成失败。
        """
        try:
            info = self._client(timeout=ACCOUNT_PROBE_TIMEOUT).get_login_info()
        except Exception:
            return None  # 端口没起来 / 未登录，都当作"还探测不到"
        data = info.get("data") or {}
        uid = data.get("user_id")
        return str(uid) if uid else None

    # ------------------------------------------------------------------ 监控
    def _start_monitor(self) -> None:
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            return
        self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, name="qlark-monitor", daemon=True
        )
        self._monitor_thread.start()
    def _monitor_loop(self) -> None:
        """就绪之后持续盯着进程与链路；掉线自动恢复。"""
        last_ping = 0.0
        while not self._monitor_stop.wait(1.5):
            proc = self._proc
            running = proc is not None and proc.running

            if self.phase is Phase.READY and not running:
                self._online = False
                self._on_change()
                if self._auto_restarts >= MAX_AUTO_RESTARTS:
                    self._fail(
                        f"NapCat 反复退出（已自动重启 {self._auto_restarts} 次），已停止重试。"
                    )
                    continue
                self._auto_restarts += 1
                self.logbus.add(
                    f"检测到 NapCat 退出，正在自动重启（{self._auto_restarts}/{MAX_AUTO_RESTARTS}）",
                    level="warn",
                )
                self._stop_event.clear()
                threading.Thread(
                    target=self._run_sequence, kwargs={"force_scan": False}, daemon=True
                ).start()
                time.sleep(5)

            if self.phase is Phase.READY and running and time.monotonic() - last_ping > 10:
                last_ping = time.monotonic()
                try:
                    status = self._client().get_status()
                    online = bool((status.get("data") or {}).get("online"))
                    if online != self._online:
                        self._online = online
                        self._on_change()
                except OneBotError:
                    if self._online:
                        self._online = False
                        self.logbus.add("OneBot 探活失败，链路可能已断开", level="warn")
                        self._on_change()

    # ------------------------------------------------------------------ 测试辅助
    def wait_until(self, predicate: Callable[[], bool], timeout: float = 30.0) -> bool:
        """轮询等待条件成立（仅供测试与调试）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return predicate()
