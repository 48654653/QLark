"""前端配置白名单必须覆盖后端字段。

这个坑踩过两次（都是同一类）：
  · 人设：``split_enabled`` / ``meme_enabled`` 存进去了，但 ``normalizePersonas``
    没列它们 —— 界面上"勾选 → 保存 → 自动变回未勾选"
  · 模型：v2.1 的 ``voice_enabled`` / ``tts_model`` / ``keepalive_*`` /
    ``quote_context_enabled`` 也被漏掉 —— 勾了语音、点应用，立刻弹回不允许

原因：保存成功后会**再拉一次配置**、走 ``normalizeModelConfig`` 过滤，
白名单里没有的字段变成 undefined，渲染出来就是"未勾选 / 空值"。
后端完全正确，前端看不出来。

所以这里做静态契约检查：**后端接受的字段，前端白名单必须一个不落**。
纯读文件，不起 GUI，很快。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "src" / "qlark" / "web" / "app.js"

#: 这些字段由别的方式处理（比如 api_key 只有在"改过"时才提交、回显用 api_key_masked）
SPECIAL_CASES = {"api_key"}


def _function_body(name: str) -> str:
    source = APP_JS.read_text(encoding="utf-8")
    match = re.search(rf"function {name}\(raw\) \{{(.*?)\n  \}}", source, re.S)
    assert match, f"app.js 里找不到 {name}()"
    return match.group(1)


def _body_from(name: str) -> str:
    """从 ``function <name>(`` 截到同缩进的第一个 ``\\n  }``（参数个数不限）。"""
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index(f"function {name}(")
    end = source.index("\n  }", start)
    return source[start:end]


@pytest.fixture(scope="module")
def model_body() -> str:
    return _function_body("normalizeModelConfig")


@pytest.fixture(scope="module")
def persona_body() -> str:
    return _function_body("normalizePersonas")


def test_every_model_field_is_in_the_frontend_whitelist(model_body: str) -> None:
    from qlark.model_config import MODEL_FIELDS

    missing = [
        name
        for name in MODEL_FIELDS
        if name not in SPECIAL_CASES and name not in model_body
    ]
    assert not missing, (
        "这些字段后端接受、前端白名单没列 —— 应用之后会被还原："
        + "、".join(missing)
    )


def test_every_persona_column_is_in_the_frontend_whitelist(persona_body: str) -> None:
    """人设的字段从 ``personas.list_public()`` 的输出里取，避免又手工维护一份清单。"""
    from qlark.storage.db import COLUMN_MIGRATIONS

    # list_public 的字段和数据库列一一对应，用迁移表当"后端认哪些列"的依据
    columns = [name for name, _ in COLUMN_MIGRATIONS.get("personas", [])]
    assert columns, "personas 的迁移表是空的，这个测试就失去意义了"
    missing = [name for name in columns if name not in persona_body]
    assert not missing, (
        "这些人设字段后端存得进去、前端读不回来 —— 保存后会弹回原样："
        + "、".join(missing)
    )


def test_voice_switch_survives_a_round_trip(model_body: str) -> None:
    """把用户实际遇到的那一次单独钉住。"""
    for name in (
        "voice_enabled",
        "voice_max_chars",
        "tts_model",
        "keepalive_enabled",
        "keepalive_seconds",
        "quote_context_enabled",
    ):
        assert name in model_body, f"{name} 不在前端白名单里，勾上之后会被还原"


def test_whitelist_defaults_are_sane(model_body: str) -> None:
    """新加的开关默认值要跟后端一致，否则"没配过"和"配成 false"会混淆。

    这里只查字符串层面：``voice_enabled: !!s.voice_enabled`` 这种写法表示
    默认关；``=== undefined) ? true :`` 这种写法表示默认开。
    """
    # 语音默认关（要在界面里主动打开），保活和引用默认开
    assert re.search(r"voice_enabled: !!s\.voice_enabled", model_body), (
        "语音应该默认关，用 !!s.voice_enabled 直取"
    )
    assert "keepalive_enabled" in model_body and "undefined" in model_body, (
        "保活应该默认开（undefined 时取 true）"
    )


# --------------------------------------------------------------------------- #
# 运行统计：后端给多少字段，前端就得接多少
# --------------------------------------------------------------------------- #
def _bot_counters() -> set[str]:
    source = APP_JS.read_text(encoding="utf-8")
    match = re.search(r"var BOT_COUNTERS = \[(.*?)\];", source, re.S)
    assert match, "app.js 里找不到 BOT_COUNTERS 白名单"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def test_every_pipeline_stat_reaches_the_frontend() -> None:
    """``PipelineStats.to_dict()`` 里的字段必须被前端接住。

    第三次踩同一类坑：「回复形态」卡片上的配表情 / 语音 / 分段一直是 0%，
    后端 memes / voices / splits 明明在涨 —— 因为 ``normalizeBot`` 是白名单，
    没列的字段被悄悄丢掉，界面上就成了 0。这类问题后端日志完全看不出来。
    """
    from qlark.chat.pipeline import PipelineStats

    backend = set(PipelineStats().to_dict())
    body = _function_body("normalizeBot")
    counters = _bot_counters()
    missing = sorted(backend - counters - set(re.findall(r"(\w+):", body)))
    assert not missing, (
        "这些统计字段后端有、前端 normalizeBot 没接住，界面上会一直显示 0："
        + "、".join(missing)
    )


def test_reply_shape_counters_are_wired() -> None:
    """把用户实际遇到的那一次单独钉住。"""
    counters = _bot_counters()
    for name in ("memes", "voices", "splits", "replied"):
        assert name in counters, f"{name} 不在 BOT_COUNTERS 里，回复形态卡片会显示 0"


def test_every_memory_item_field_reaches_the_frontend(tmp_path) -> None:
    """``list_memories()`` 的字段必须被 ``normalizeMemoryItem`` 接住。

    同一类坑的第三次：``rounds`` / ``uncompressed_rounds`` 是压缩门槛的计量单位，
    前端丢了会退化成"N 条原文"，用户就会把 54 条当成 54 轮，误判成压缩坏了。
    """
    from qlark.chat.store import MessageStore
    from qlark.storage.db import Database

    db = Database(tmp_path / "memory.db")
    try:
        store = MessageStore(db)
        store.ensure_default_persona()
        store.append("group:1:p1", "user", "问一句")
        store.append("group:1:p1", "assistant", "答一句")
        item = store.list_memories()[0]
    finally:
        db.close()

    body = _function_body("normalizeMemoryItem")
    missing = sorted(name for name in item if name not in body)
    assert not missing, (
        "这些记忆字段后端给了、前端 normalizeMemoryItem 没接住："
        + "、".join(missing)
    )


def test_mark_bad_does_not_clobber_element_classes() -> None:
    """标红只能加/去 ``bad`` 类，不能整个覆盖 className。

    真实事故：触发词表（``.kw-table`` 容器）被当成标红对象，``markBad`` 把它改成
    ``class="input"`` —— 高度立刻按输入框算，而表格里的行照旧画出去，
    整页从此"塌成一团"（点一下开关就复现）。改容器类名的地方一律走 classList。
    """
    body = _body_from("markBad")
    assert not re.search(r"\.className\s*=", body), (
        "markBad 又在覆盖 className 了：非 input 元素（例如触发词表的容器）"
        "会被改成 class=\"input\"，布局直接塌掉"
    )
    assert "classList" in body


def test_every_fun_config_field_reaches_the_frontend(tmp_path) -> None:
    """``get_fun_config()`` 的字段必须被 ``normalizeFunConfig`` 接住。"""
    from qlark.fun import config as fun_config
    from qlark.settings import Settings
    from qlark.storage.db import Database

    db = Database(tmp_path / "fun.db")
    try:
        snap = fun_config.snapshot(Settings(db))
    finally:
        db.close()

    body = _function_body("normalizeFunConfig")
    # ok 只是调用结果标记，前端不需要存进配置对象
    missing = sorted(name for name in snap if name != "ok" and name not in body)
    assert not missing, (
        "这些趣味功能字段后端给了、前端 normalizeFunConfig 没接住："
        + "、".join(missing)
    )

