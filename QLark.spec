# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（需求规格 v1.1 §9.4）。

设计取舍（都不是随手定的）：

**onedir 而不是 onefile。**
onefile 每次启动都要把 ~150MB 解压到临时目录 —— 一个聊天机器人要频繁重启
（切账号、改端口），那个延迟很难受。而且 NapCat 运行时（400MB+）**无论如何
都得放在 exe 外面**（它要作为独立进程启动、还要往自己目录里写配置），
所以 onefile 省不掉"一个文件夹"这件事。

**``console=False``。**
需求里的硬要求：双击启动不能弹黑框。代价是**看不到任何早期崩溃信息** ——
所以 :mod:`qlark.logging_setup` 必须在任何可能失败的操作之前把日志落到文件，
出问题时让用户去看 ``%LOCALAPPDATA%\\QLark\\logs``。

**图标从 ``src/qlark/assets`` 取**（``tools/make_icon.py`` 生成）。
"""

from pathlib import Path

ROOT = Path(SPECPATH).resolve()  # noqa: F821 - SPECPATH 由 PyInstaller 注入
SRC = ROOT / "src"

#: 前端资源要放在 ``web/`` 下，``qlark.paths.web_dir()`` 就是按这个找的
datas = [
    (str(SRC / "qlark" / "web"), "web"),
    (str(SRC / "qlark" / "storage" / "schema.sql"), "qlark/storage"),
    (str(SRC / "qlark" / "assets"), "qlark/assets"),
]

#: 这些是"能少一个是一个"的重量级依赖。列进来是因为它们在开发环境里装着，
#: PyInstaller 会顺着 import 把它们整个拖进去，包体积能翻好几倍。
EXCLUDES = [
    "tkinter",
    "matplotlib",
    "numpy",
    "pandas",
    "scipy",
    "pytest",
    "_pytest",
    "IPython",
    "notebook",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "sqlite3.test",
    "unittest",
    "pydoc_data",
]

#: pywebview 的 Windows 后端是动态导入的，PyInstaller 静态分析看不到
HIDDEN = [
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "clr_loader",
    "pythonnet",
]

a = Analysis(  # noqa: F821
    [str(ROOT / "run.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="QLark",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX 压缩过的 exe 更容易被杀软误报（§9.4 已经提过这个风险）
    console=False,      # 不能有黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(SRC / "qlark" / "assets" / "qlark.ico"),
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="QLark",
)
