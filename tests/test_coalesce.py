"""免@ 窗口里的消息合并。

有人习惯把一句话拆成好几条发（"在" "吗" "问个事"）。每条都回会变成
「咋了 / 嗯？ / 说」三连 —— 既费 token 又一眼是机器人。

规则（用户定的）：
  · 免@ 的消息先攒起来，**静默一段时间后合并成一条**再回
  · 每来一条碎片，计时**重置**
  · 从第一条算起有**硬上限**，防止对方一直打字导致永远不回
  · @ 机器人**立刻回**，但要把已经攒下的碎片一起吸收进来
  · 合并后每个碎片**一行**进记忆
  · 全部可关闭
"""

from __future__ import annotations

import time

import pytest

from conftest import FakeOpenAI, wait_for
from qlark.chat.coalesce import Coalescer

from test_m2_integration import _inject, _reset, ready  # noqa: F401

GROUP = "888888"
USER = "333333"


# --------------------------------------------------------------------------- #
# 合并器本身
# --------------------------------------------------------------------------- #
def test_burst_becomes_one_batch() -> None:
    """连发几条、每条都在打断静默 → 最终只发一次。"""
    got: list[tuple[str, list[str]]] = []
    coalescer = Coalescer(
        quiet_seconds=0.25, max_seconds=5, on_flush=lambda k, v: got.append((k, list(v)))
    )
    for index in range(4):
        coalescer.add("g#a", f"第{index}条")
        time.sleep(0.08)

    assert got == [], "还没静默够，不该发"
    assert wait_for(lambda: got, timeout=5), "静默之后应该发出去"
    assert len(got) == 1, "四条应该合成一次"
    assert got[0][1] == ["第0条", "第1条", "第2条", "第3条"]


def test_hard_cap_forces_a_flush() -> None:
    """对方一直打字不停 → 到硬上限必须发，不能永远不吭声。"""
    got: list[tuple[str, list[str]]] = []
    coalescer = Coalescer(
        quiet_seconds=0.5, max_seconds=0.4, on_flush=lambda k, v: got.append((k, list(v)))
    )
    # max 会被抬到不小于 quiet，所以这里用 0.5/0.6 的组合来测
    coalescer.configure(quiet_seconds=0.5, max_seconds=0.6)

    started = time.monotonic()
    for _ in range(30):
        coalescer.add("g#b", "x")
        time.sleep(0.05)
        if got:
            break

    assert got, "一直打字也必须在上限时发出来"
    assert time.monotonic() - started < 1.5, "不该等满静默时间"


def test_take_does_not_fire_the_callback() -> None:
    """@ 的时候要把攒着的碎片取走，不能同时触发一次普通回复。"""
    got: list[object] = []
    coalescer = Coalescer(
        quiet_seconds=10, max_seconds=20, on_flush=lambda k, v: got.append((k, v))
    )
    coalescer.add("g#c", "一")
    coalescer.add("g#c", "二")

    assert coalescer.take("g#c") == ["一", "二"]
    assert coalescer.count("g#c") == 0
    time.sleep(0.1)
    assert got == []


def test_discard_all_clears_everything() -> None:
    got: list[object] = []
    coalescer = Coalescer(
        quiet_seconds=0.2, max_seconds=1, on_flush=lambda k, v: got.append((k, v))
    )
    coalescer.add("g#d", "一")
    coalescer.add("g#e", "二")
    assert coalescer.discard_all() == 2
    time.sleep(0.4)
    assert got == [], "丢掉的批次不该再发出去"


def test_keys_are_independent() -> None:
    got: list[tuple[str, list[str]]] = []
    coalescer = Coalescer(
        quiet_seconds=0.2, max_seconds=2, on_flush=lambda k, v: got.append((k, list(v)))
    )
    coalescer.add("g#甲", "a")
    coalescer.add("g#乙", "b")
    assert wait_for(lambda: len(got) >= 2, timeout=5), "两个人的批次应该各发各的"
    bodies = {key: items for key, items in got}
    assert bodies["g#甲"] == ["a"]
    assert bodies["g#乙"] == ["b"]


# --------------------------------------------------------------------------- #
# 接进流水线
# --------------------------------------------------------------------------- #
def _say(env, text: str, *, user_id: str = USER, message_id: int, at: bool = False) -> None:
    _reset(env.home)
    _inject(
        env.home,
        message_type="group",
        group_id=GROUP,
        user_id=user_id,
        message_id=message_id,
        text=text,
        at_bot=at,
        nickname="毛糙蛋",
    )


def _texts(env) -> list[str]:
    from test_m2_integration import _reply_texts

    return _reply_texts(env.home)


def _arm_window(env, *, message_id: int = 900) -> None:
    """先 @ 一次，把免@ 窗口打开。"""
    _say(env, "在吗", message_id=message_id, at=True)
    assert wait_for(lambda: _texts(env), timeout=30), "开窗的 @ 没回"


def _fast(env, *, quiet: float = 0.3, cap: float = 3.0) -> None:
    env.settings.update(
        keepalive_enabled=True,
        keepalive_seconds=120,
        coalesce_enabled=True,
        coalesce_quiet_seconds=quiet,
        coalesce_max_seconds=cap,
    )


def test_fragments_are_merged_into_one_reply(ready, fake_openai: FakeOpenAI) -> None:
    """免@ 窗口里连发三条 → 只回一次，且三条都在同一个请求里。"""
    _fast(ready)
    _arm_window(ready)
    fake_openai.reset()

    for index, text in enumerate(("那个", "我", "想问一下")):
        _say(ready, text, message_id=100 + index)
        time.sleep(0.08)

    assert wait_for(lambda: _texts(ready), timeout=30), "合并后应该回一次"
    assert len(fake_openai.requests()) == 1, (
        f"三条碎片应该只调一次模型，实际 {len(fake_openai.requests())} 次"
    )
    prompt = str(fake_openai.requests()[0]["messages"][-1]["content"])
    for text in ("那个", "我", "想问一下"):
        assert text in prompt, f"碎片{text!r}没进提示词"


def test_fragments_land_on_separate_lines(ready, fake_openai: FakeOpenAI) -> None:
    """合并后每个碎片一行 —— 保留"他是一句一句发的"这个信息。"""
    _fast(ready)
    _arm_window(ready)
    fake_openai.reset()

    for index, text in enumerate(("一", "二", "三")):
        _say(ready, text, message_id=110 + index)
        time.sleep(0.08)

    assert wait_for(lambda: _texts(ready), timeout=30)
    content = str(fake_openai.requests()[0]["messages"][-1]["content"])
    body = content.split("消息：", 1)[-1]
    assert "\n一\n二\n三" in "\n" + body or body.strip() == "一\n二\n三", repr(body)


def test_at_absorbs_pending_fragments(ready, fake_openai: FakeOpenAI) -> None:
    """攒了两条碎片之后突然 @ —— 应该一次回，不能先回个"嗯？"再回正式的。"""
    _fast(ready, quiet=5.0, cap=10.0)   # 静默设得很长，保证 @ 时碎片还在缓冲里
    _arm_window(ready)
    fake_openai.reset()

    _say(ready, "碎片甲", message_id=120)
    _say(ready, "碎片乙", message_id=121)
    assert not _texts(ready), "静默期还没到，不该回"

    _say(ready, "@你一下", message_id=122, at=True)
    assert wait_for(lambda: _texts(ready), timeout=30), "@ 应该立刻回"
    assert wait_for(lambda: len(fake_openai.requests()) == 1, timeout=5)
    time.sleep(0.3)
    assert len(fake_openai.requests()) == 1, "碎片不该再单独触发第二次"

    prompt = str(fake_openai.requests()[0]["messages"][-1]["content"])
    for text in ("碎片甲", "碎片乙", "@你一下"):
        assert text in prompt, f"{text!r} 没被吸收进来"


def test_coalescing_can_be_disabled(ready, fake_openai: FakeOpenAI) -> None:
    """关掉之后，窗口内每条都回（老行为）。"""
    _fast(ready)
    ready.settings.update(coalesce_enabled=False)
    _arm_window(ready)
    fake_openai.reset()

    _say(ready, "一", message_id=130)
    assert wait_for(lambda: _texts(ready), timeout=30), "关掉之后应该立刻回"

    _reset(ready.home)
    _say(ready, "二", message_id=131)
    assert wait_for(lambda: _texts(ready), timeout=30)
    assert len(fake_openai.requests()) == 2, "关掉合并之后应该各回各的"


def test_at_is_still_immediate_when_coalescing_is_on(ready, fake_openai: FakeOpenAI) -> None:
    """@ 不走合并 —— 明确召唤就该马上有反应。"""
    _fast(ready, quiet=5.0, cap=10.0)
    fake_openai.reset()

    started = time.monotonic()
    _say(ready, "在吗", message_id=140, at=True)
    assert wait_for(lambda: _texts(ready), timeout=30)
    assert time.monotonic() - started < 5, "不该等到静默时间才回"


def test_private_chat_is_not_coalesced(ready, fake_openai: FakeOpenAI) -> None:
    """私聊本来就不用 @，也不该被合并拖慢。"""
    _fast(ready, quiet=5.0, cap=10.0)
    fake_openai.reset()

    _reset(ready.home)
    _inject(
        ready.home,
        message_type="private",
        user_id="20000002",
        message_id=150,
        text="在吗",
    )
    started = time.monotonic()
    assert wait_for(lambda: _texts(ready), timeout=30)
    assert time.monotonic() - started < 5, "私聊不该等合并"


def test_pause_discards_pending_fragments(ready, fake_openai: FakeOpenAI) -> None:
    """暂停时要把攒着的碎片丢掉，恢复后不能把几分钟前的旧话回出去。"""
    _fast(ready, quiet=5.0, cap=10.0)
    _arm_window(ready)
    fake_openai.reset()

    _say(ready, "攒着的一句", message_id=160)
    assert ready.bot.pipeline._coalescer.count(f"group:{GROUP}#{USER}") == 1

    ready.bot.pipeline.set_paused(True)
    assert ready.bot.pipeline._coalescer.count(f"group:{GROUP}#{USER}") == 0
    ready.bot.pipeline.set_paused(False)

    time.sleep(0.3)
    assert fake_openai.requests() == [], "丢弃的碎片不该再发出去"


# --------------------------------------------------------------------------- #
# 合并规则（纯函数）
# --------------------------------------------------------------------------- #
def _parsed(text: str = "", *, images=None, reply_id=None, mentions=False):
    from qlark.onebot.segments import ImageRef, ParsedMessage

    return ParsedMessage(
        text=text,
        images=[ImageRef(file=f, url=f"https://x/{f}") for f in (images or [])],
        reply_id=reply_id,
        mentions_bot=mentions,
    )


def test_merge_takes_images_by_union_and_last_quote() -> None:
    """图片取**并集**（几张图都要给模型看），引用取**最后一条**非空的。"""
    from qlark.chat.pipeline import _merge_batch
    from qlark.onebot.events import parse_message_event

    event = parse_message_event(
        {
            "post_type": "message",
            "message_type": "group",
            "group_id": 1,
            "user_id": 2,
            "message_id": 3,
            "raw_message": "x",
            "message": [{"type": "text", "data": {"text": "x"}}],
            "self_id": 10000001,
            "time": 1,
        },
        self_id="10000001",
    )
    assert event is not None

    first = (event, _parsed("看这张", images=["a.jpg"], reply_id="111"))
    second = (event, _parsed("还有这张", images=["b.jpg"]))
    third = (event, _parsed("再引一条", reply_id="222"))

    _, merged = _merge_batch([first, second, third])
    assert [ref.file for ref in merged.images] == ["a.jpg", "b.jpg"], "图片要取并集"
    assert merged.reply_id == "222", "引用取最后一条非空的"
    assert merged.text == "看这张\n还有这张\n再引一条", "每个碎片一行"


def test_merge_single_item_is_untouched() -> None:
    """只有一条时不要瞎折腾（走的就是原对象）。"""
    from qlark.chat.pipeline import _merge_batch
    from qlark.onebot.events import parse_message_event

    event = parse_message_event(
        {
            "post_type": "message",
            "message_type": "group",
            "group_id": 1,
            "user_id": 2,
            "message_id": 3,
            "raw_message": "x",
            "message": [{"type": "text", "data": {"text": "x"}}],
            "self_id": 10000001,
            "time": 1,
        },
        self_id="10000001",
    )
    parsed = _parsed("单条")
    merged_event, merged = _merge_batch([(event, parsed)])
    assert merged is parsed
    assert merged_event is event
