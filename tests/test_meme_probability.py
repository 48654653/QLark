"""表情与分段的概率：**骰子按设置走，但有效值可能被人设覆盖**。

用户设了全局 90% 的图片概率，却连着几次没图。实测排查结论：

① **人设上的覆盖值压过全局 —— 这是主因。**
   全局 90%，但那个人设上存着 10%（或者干脆没勾「允许发送表情包」），
   那个会话就永远按人设来。**界面上完全看不出来**，所以像"概率坏了"。

② 模型偶尔把情绪名写成列表里没有的词（旧日志 11/47 ≈ 23%），
   取不到图就丢弃。**这个不兜底** —— 随机配一张和这句话无关的图比不配更假。
   该修的是让模型别编名字，不是偷偷补一张。

③ 排查过程中我一度误判成"thinking 吃光预算导致空回复"。那是**测试脚本自己**
   用了 max_tokens=400 + thinking=auto 造成的假象；生产日志里 0 次空回复。
"""

from __future__ import annotations

import pytest

from conftest import FakeOpenAI, wait_for

from test_m2_integration import _inject, _reply_texts, _reset, _sent, ready  # noqa: F401

GROUP = "888888"


def _images(env) -> list[dict]:
    """这一轮实际发出去的图片段。"""
    found: list[dict] = []
    for item in _sent(env.home):
        for segment in (item.get("params") or {}).get("message") or []:
            if segment.get("type") == "image":
                found.append(segment)
    return found


# --------------------------------------------------------------------------- #
# 骰子按设置走
# --------------------------------------------------------------------------- #
def _hits(pipeline, persona, *, meme: bool, n: int = 4000) -> float:
    count = 0
    for _ in range(n):
        allow_split, allow_meme, _, _ = pipeline._roll_expression(persona)
        count += 1 if (allow_meme if meme else allow_split) else 0
    return count / n


def test_meme_dice_follows_global_chance(ready) -> None:
    ready.settings.update(meme_chance=0.9, split_chance=0.0)
    ready.bot.pipeline.personas.update(1, {"meme_enabled": True, "split_enabled": True})
    persona = ready.bot.pipeline.personas.get(1)
    assert _hits(ready.bot.pipeline, persona, meme=True) == pytest.approx(0.9, abs=0.03)


def test_split_dice_follows_global_chance(ready) -> None:
    ready.settings.update(meme_chance=0.0, split_chance=0.9)
    ready.bot.pipeline.personas.update(1, {"meme_enabled": True, "split_enabled": True})
    persona = ready.bot.pipeline.personas.get(1)
    assert _hits(ready.bot.pipeline, persona, meme=False) == pytest.approx(0.9, abs=0.03)


def test_zero_chance_never_fires(ready) -> None:
    ready.settings.update(meme_chance=0.0, split_chance=0.0)
    ready.bot.pipeline.personas.update(1, {"meme_enabled": True, "split_enabled": True})
    persona = ready.bot.pipeline.personas.get(1)
    assert _hits(ready.bot.pipeline, persona, meme=True, n=500) == 0.0


def test_meme_enabled_off_never_fires(ready) -> None:
    ready.settings.update(meme_chance=1.0)
    ready.bot.pipeline.personas.update(1, {"meme_enabled": False})
    persona = ready.bot.pipeline.personas.get(1)
    assert _hits(ready.bot.pipeline, persona, meme=True, n=500) == 0.0


# --------------------------------------------------------------------------- #
# 人设覆盖 —— 这是"设了 90% 却没图"的主因
# --------------------------------------------------------------------------- #
def test_persona_override_beats_global(ready) -> None:
    """**这就是那个坑**：全局 90%，人设上写着 10%，实际就是 10%。"""
    ready.settings.update(meme_chance=0.9)
    ready.bot.pipeline.personas.update(
        1, {"meme_enabled": True, "meme_chance": 0.1, "split_enabled": False}
    )
    persona = ready.bot.pipeline.personas.get(1)
    assert _hits(ready.bot.pipeline, persona, meme=True) == pytest.approx(0.1, abs=0.03)


def test_blank_persona_value_follows_global(ready) -> None:
    ready.settings.update(meme_chance=0.9)
    ready.bot.pipeline.personas.update(
        1, {"meme_enabled": True, "meme_chance": None, "split_enabled": False}
    )
    persona = ready.bot.pipeline.personas.get(1)
    assert _hits(ready.bot.pipeline, persona, meme=True) == pytest.approx(0.9, abs=0.03)


def test_effective_chance_helpers_agree_with_the_dice(ready) -> None:
    """界面要显示的"有效概率"必须和掷骰子用的是同一个值。"""
    ready.settings.update(meme_chance=0.9, split_chance=0.4)

    # 跟随全局
    ready.bot.pipeline.personas.update(
        1, {"meme_enabled": True, "split_enabled": True, "meme_chance": None}
    )
    assert ready.bot.pipeline.effective_chance(1, "meme_chance") == pytest.approx(0.9)
    assert ready.bot.pipeline.effective_chance(1, "split_chance") == pytest.approx(0.4)

    # 覆盖
    ready.bot.pipeline.personas.update(1, {"meme_chance": 0.1})
    assert ready.bot.pipeline.effective_chance(1, "meme_chance") == pytest.approx(0.1)

    # 没开表情时是 0
    ready.bot.pipeline.personas.update(1, {"meme_enabled": False})
    assert ready.bot.pipeline.effective_chance(1, "meme_chance") == 0.0


# --------------------------------------------------------------------------- #
# 模型没按列表取词 —— 不兜底
# --------------------------------------------------------------------------- #
def test_unknown_emotion_is_dropped_not_replaced(ready, fake_openai: FakeOpenAI, tmp_path) -> None:
    """模型编了个名字 → 这一轮不配图。

    **绝不能随机补一张**：配一张和这句话无关的图，比不配更假。
    """
    memes = tmp_path / "memes"
    memes.mkdir()
    (memes / "嫌弃.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    ready.settings.update(meme_chance=1.0, meme_dir=str(memes), split_chance=0.0)
    ready.bot.pipeline.personas.update(1, {"meme_enabled": True, "split_enabled": False})
    fake_openai.replies('{"messages": ["一"], "meme": "无语"}', repeat_last=True)

    _reset(ready.home)
    _inject(
        ready.home,
        message_type="group",
        group_id=GROUP,
        user_id="333333",
        message_id=1,
        text="在吗",
        at_bot=True,
        nickname="毛糙蛋",
    )
    assert wait_for(lambda: _reply_texts(ready.home), timeout=30)

    assert _images(ready) == [], "编了名字的表情不该被随机替换成别的图"
