"""QLark 的 NapCat 运行时管理。

模块职责：

* :mod:`~qlark.napcat.launch`    —— 启动参数（可被测试注入假 NapCat）
* :mod:`~qlark.napcat.process`   —— 进程生命周期（静默启动 / 杀进程树 / 孤儿清理）
* :mod:`~qlark.napcat.logbus`    —— 日志环形缓冲与落盘
* :mod:`~qlark.napcat.patterns`  —— NapCat 输出特征串
* :mod:`~qlark.napcat.envcheck`  —— 首次运行环境自检
* :mod:`~qlark.napcat.netconfig` —— ``onebot11_<QQ>.json`` 合并写入
* :mod:`~qlark.napcat.qrcode`    —— 登录二维码文件轮询
* :mod:`~qlark.napcat.login`     —— 启动 / 登录状态机
"""

from .launch import LaunchSpec, build_launch_spec
from .logbus import LogBus
from .login import LoginManager, LoginError
from .process import NapCatProcess
from .qrcode import QrCodeWatcher

__all__ = [
    "LaunchSpec",
    "build_launch_spec",
    "LogBus",
    "LoginError",
    "LoginManager",
    "NapCatProcess",
    "QrCodeWatcher",
]
