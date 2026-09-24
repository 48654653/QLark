"""版本与产品名常量。"""

from __future__ import annotations

APP_NAME = "QLark"
APP_NAME_CN = "云雀"
__version__ = "1.0.0"
__build__ = "m1"

WINDOW_TITLE = f"{APP_NAME} {APP_NAME_CN} · v{__version__}"

#: OneBot HTTP 服务端默认端口（与 NapCat 网络配置一致）—— QLark 主动调它发动作
DEFAULT_ONEBOT_PORT = 3000
#: QLark 事件服务默认端口 —— NapCat 主动把消息事件推到这里
DEFAULT_EVENT_PORT = 3001
#: NapCat WebUI 默认端口（仅作占用检测，QLark 不使用 WebUI API）
DEFAULT_WEBUI_PORT = 6099
#: OneBot 本地访问 token（必须设置，否则等于把 QQ 账号交出去）
DEFAULT_ONEBOT_TOKEN = "qlark-local"
