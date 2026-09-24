"""启动参数。

真实启动：``cd <napcat> && node.exe ./index.js -q <QQ号>``

测试时通过环境变量把可执行文件与入口脚本换成"假 NapCat"，这样状态机可以在
没有 111MB 运行时、没有 QQ 账号的机器上被完整验证。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .. import paths


@dataclass(frozen=True)
class LaunchSpec:
    """一次 NapCat 进程的启动描述。"""

    executable: Path
    args: tuple[str, ...]
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)

    def argv(self) -> list[str]:
        return [str(self.executable), *self.args]

    def describe(self) -> str:
        return f"{self.executable.name} {' '.join(self.args)}".strip()


def build_launch_spec(qq: str | None = None, *, extra_env: dict[str, str] | None = None) -> LaunchSpec:
    """按当前配置构造启动描述。

    Args:
        qq: 已保存的 QQ 号。为 ``None`` 时不带 ``-q``，NapCat 会走二维码登录。
    """
    args: list[str] = [str(paths.napcat_entry())]
    if qq:
        args += ["-q", str(qq)]
    return LaunchSpec(
        executable=paths.napcat_exe(),
        args=tuple(args),
        cwd=paths.napcat_dir(),
        env=dict(extra_env or {}),
    )
