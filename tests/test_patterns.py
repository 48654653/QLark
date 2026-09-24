"""NapCat 输出特征串的解析测试。

用例里的日志行全部照抄自技术文档的实测记录。
"""

from __future__ import annotations

from qlark.napcat import patterns


def test_quick_login_line() -> None:
    line = "16:05:43 [info] 正在快速登录  2157649443"
    match = patterns.RE_QUICK_LOGIN.search(line)
    assert match is not None
    assert match.group(1) == "2157649443"


def test_worker_logged_in_variants() -> None:
    assert patterns.RE_WORKER_LOGGED_IN.search(
        "[info] [NapCat] [Fork] Worker进程已登录成功，切换到正常重试策略"
    )
    assert patterns.RE_WORKER_LOGGED_IN.search("[info] [NapCat] [Fork] Worker进程已登录成功")


def test_no_quick_login_line() -> None:
    assert patterns.RE_NO_QUICK_LOGIN.search(
        "[info] 没有 -q 指令指定快速登录，将使用二维码登录方式"
    )


def test_onebot_start_line() -> None:
    line = "[OneBot] [HTTP Server Adapter] Start On 127.0.0.1:3000"
    match = patterns.RE_ONEBOT_START.search(line)
    assert match is not None
    assert match.group(1) == "127.0.0.1"
    assert match.group(2) == "3000"


def test_module_not_found_line() -> None:
    line = "Error: The specified module could not be found. ...\\wrapper.node"
    assert patterns.RE_MODULE_NOT_FOUND.search(line)


def test_account_config_line() -> None:
    """登录后 NapCat 打印的配置加载行里带着 QQ 号 —— 扫码模式靠它认账号。"""
    line = (
        "03:56:32 [debug] [Core] [Config] 配置文件"
        "E:\\QLark\\runtime\\napcat\\napcat\\config\\napcat_2157649443.json加载 "
        '{"fileLog":false}'
    )
    match = patterns.RE_ACCOUNT_CONFIG.search(line)
    assert match is not None
    assert match.group(1) == "2157649443"


def test_account_config_line_does_not_match_other_files() -> None:
    assert patterns.RE_ACCOUNT_CONFIG.search("配置文件 napcat.json 加载") is None
    assert patterns.RE_ACCOUNT_CONFIG.search("配置文件 onebot11_2157649443.json 加载") is None


def test_parse_level() -> None:
    assert patterns.parse_level("[info] 普通输出") == "info"
    assert patterns.parse_level("[warn] 警告") == "warn"
    assert patterns.parse_level("[warning] 警告") == "warn"
    assert patterns.parse_level("[error] 出错了") == "error"
    assert patterns.parse_level("[debug] 细节") == "debug"
    assert patterns.parse_level("登录失败") == "error"
    assert patterns.parse_level("随便一句话") == "info"
