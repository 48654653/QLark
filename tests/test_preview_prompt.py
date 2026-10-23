"""「总体预览」必须能真的调通。

这个功能崩过一次：``reply_format.build_output_rules`` 改了签名（v2 去骰子），
但 ``app.preview_prompt`` 还按旧参数调用 —— 一打开弹窗就是 TypeError。
`build_preview` 自己有测试，``preview_prompt`` 这个**调用方**却没有，
所以测试全绿而功能是坏的。

所以这里直接调 ``Application.preview_prompt``，把调用方也钉住。
"""

from __future__ import annotations

import pytest


@pytest.fixture
def app(qlark_home, monkeypatch):
    from qlark.app import Application

    monkeypatch.setenv("QLARK_HOME", str(qlark_home.data))
    application = Application()
    yield application
    application.db.close()


def test_preview_prompt_does_not_raise(app) -> None:
    """最基本的：调它不能炸。"""
    res = app.preview_prompt(1)
    assert res.get("ok") is True, res


def test_preview_contains_rules_and_system_prompt(app) -> None:
    res = app.preview_prompt(1)
    text = res["text"]
    assert "【输出规则】" in text, "v2 起规则每轮都注入，预览里必须有"
    assert "messages" in text, "规则里要说明 JSON 的字段"
    assert "人设提示词" in text


def test_preview_reminder_block_is_present(app) -> None:
    """格式提醒也是每轮都发的，预览要和实际一致。"""
    res = app.preview_prompt(1)
    titles = [block["title"] for block in res["blocks"]]
    assert any("格式提醒" in title for title in titles)


def test_preview_note_explains_there_is_no_dice(app) -> None:
    """说明必须讲清楚"没有掷骰子"，否则用户还在找概率设置。"""
    res = app.preview_prompt(1)
    note = res["note"]
    assert "没有掷骰子" in note
    assert "概率" not in note, f"不该再提概率：{note}"


def test_preview_does_not_mention_dice_in_title(app) -> None:
    res = app.preview_prompt(1)
    assert "掷中" not in res["text"], "标题里不该再写「按掷中拼出」"


def test_preview_handles_missing_persona(app) -> None:
    res = app.preview_prompt(999999)
    assert res.get("ok") is False
    assert "不存在" in str(res.get("error"))


def test_preview_ignores_the_removed_prompt_arguments(app) -> None:
    """旧前端还会传那两个已经失效的参数，不能因此报错。"""
    res = app.preview_prompt(1, "旧的分段提示词", "旧的表情提示词")
    assert res.get("ok") is True
    assert "旧的分段提示词" not in res["text"]
