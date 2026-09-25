"""随机分段回复 + 表情包回复。

对应需求：
* 人设级两个开关（分段 / 表情），**各自独立**
* 概率、最大条数、间隔都由代码掷骰子控制 —— **绝不交给模型**
* 输出 JSON；格式不对 → 重新生成（带错误原因）→ 抢救 → 兜底文案
* 记忆里存自然文本 + ``[表情：x]``，**不存 JSON**
"""

from __future__ import annotations

import json

import pytest

from conftest import FakeOpenAI, _get, wait_for
from qlark.chat.memes import MemeLibrary
from qlark.chat.reply_format import (
    build_output_rules,
    build_preview,
    compose_memory_text,
    parse_reply,
    salvage,
)
from qlark.chat.store import memory_key

from test_m2_integration import _inject, _reply_texts, _reset, ready  # noqa: F401

GROUP = "888888"


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def make_png(path, color=(200, 30, 30)):
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (60, 60), color).save(path, format="PNG")
    return path


def _sent(home) -> list[dict]:
    return _get(f"http://127.0.0.1:{home.port}/_qlark/sent").get("sent", [])


def _sent_segments(home) -> list[list[dict]]:
    return [
        item["params"]["message"]
        for item in _sent(home)
        if isinstance((item.get("params") or {}).get("message"), list)
    ]


def _reply_texts_from(home) -> list[str]:
    texts: list[str] = []
    for segments in _sent_segments(home):
        for seg in segments:
            if seg.get("type") == "text":
                texts.append(str((seg.get("data") or {}).get("text") or ""))
    return texts


def _enable(env, **persona_fields) -> None:
    env.bot.pipeline.personas.update(1, persona_fields)


def _say(env, text: str = "你好", *, message_id: int = 1, expect: int = 1) -> None:
    """发一条消息并等到回复出来。

    ``expect`` 是期望发出的**消息条数**：分段回复是隔一段时间才发第二条的，
    只等"有回复"会在第一条到达后就返回，第二条还没出去。
    """
    _reset(env.home)
    _inject(
        env.home,
        message_type="group",
        group_id=GROUP,
        user_id="333333",
        message_id=message_id,
        text=text,
        at_bot=True,
        nickname="毛糙蛋",
    )
    assert wait_for(lambda: _reply_texts(env.home), timeout=30), "没有回复"
    if expect > 1:
        assert wait_for(
            lambda: len(_sent_segments(env.home)) >= expect, timeout=15
        ), f"只发出了 {len(_sent_segments(env.home))} 条，期望 {expect} 条"


# --------------------------------------------------------------------------- #
# 表情库
# --------------------------------------------------------------------------- #
def test_library_scans_flat_files(tmp_path) -> None:
    make_png(tmp_path / "哭哭.png")
    make_png(tmp_path / "害羞.png")
    library = MemeLibrary(tmp_path)
    assert sorted(library.emotions()) == ["哭哭", "害羞"]
    assert library.catalog() == {"哭哭": 1, "害羞": 1}


def test_library_scans_folders_and_mixes(tmp_path) -> None:
    """子目录名当情绪，散放的文件用文件名当情绪 —— 两种都支持。"""
    make_png(tmp_path / "发怒" / "1.png")
    make_png(tmp_path / "发怒" / "2.png", (30, 200, 30))
    make_png(tmp_path / "开心.png")

    library = MemeLibrary(tmp_path)
    assert library.catalog() == {"发怒": 2, "开心": 1}


def test_library_ignores_non_images_and_hidden(tmp_path) -> None:
    make_png(tmp_path / "好.png")
    (tmp_path / "说明.txt").write_text("不是图片", encoding="utf-8")
    (tmp_path / ".DS_Store").write_bytes(b"junk")
    (tmp_path / "坏.txt").mkdir()
    (tmp_path / "坏.txt" / "a.txt").write_text("x", encoding="utf-8")

    assert MemeLibrary(tmp_path).catalog() == {"好": 1}


def test_library_handles_missing_directory(tmp_path) -> None:
    library = MemeLibrary(tmp_path / "不存在")
    assert library.available is False
    assert library.catalog() == {}
    assert library.is_empty() is True
    assert library.pick("任意") is None
    assert "不存在" in library.describe()


def test_library_pick_prefers_exact_then_loose(tmp_path) -> None:
    make_png(tmp_path / "哭哭.png")
    make_png(tmp_path / "生气.png")
    library = MemeLibrary(tmp_path)

    assert library.pick("哭哭").path.name == "哭哭.png"
    assert library.pick("/哭哭").path.name == "哭哭.png", "QQ 习惯写成 /哭哭"
    assert library.pick("  哭哭  ").path.name == "哭哭.png"
    assert library.pick("哭").path.name == "哭哭.png", "宽松匹配：哭 → 哭哭"
    assert library.pick("不存在") is None


def test_library_pick_randomises_within_an_emotion(tmp_path) -> None:
    """一个情绪多张图时应当随机挑，每次都是同一张会很假。"""
    import random

    for index in range(10):
        make_png(tmp_path / "多变" / f"{index}.png", (index * 20, 100, 100))

    library = MemeLibrary(tmp_path)
    picked = {library.pick("多变", rng=random.Random(seed)).path.name for seed in range(24)}
    assert len(picked) > 1, f"永远挑同一张：{picked}"


# --------------------------------------------------------------------------- #
# 输出规则提示词
# --------------------------------------------------------------------------- #
def test_output_rules_empty_when_both_off() -> None:
    """两个都关时**一个字都不提** —— 这是"关掉之后不会被带跑"的第一道保障。"""
    assert build_output_rules(split=False, max_messages=2, meme=False, emotions=[]) == ""


def test_output_rules_split_only() -> None:
    rules = build_output_rules(split=True, max_messages=2, meme=False, emotions=[])
    assert "【输出规则】" in rules
    assert "messages" in rules
    assert "最多 2 条" in rules
    assert "不要输出 meme 键" in rules


def test_output_rules_are_directive_not_permissive() -> None:
    """**骰子已经掷过了，规则必须是命令式。**

    踩过的坑：第一版写的是"默认只写 1 条 / 多数时候不要填"，模型老老实实照办 ——
    骰子明明掷中了，实际触发率还是接近 0。随机该由代码负责，提示词一旦开了口子，
    频率就失控。这条测试就是防止再写回去。
    """
    split_rules = build_output_rules(split=True, max_messages=2, meme=False, emotions=[])
    assert "本轮请写成 2 条" in split_rules
    for bad in ("多数时候请只写 1 条", "默认只写 1 条", "想补一句的时候"):
        assert bad not in split_rules, f"又写回了许可式措辞：{bad!r}"

    meme_rules = build_output_rules(split=False, max_messages=2, meme=True, emotions=["开心"])
    assert "本轮请配一个表情" in meme_rules
    for bad in ("多数时候不要填", "想配一个表情时才填"):
        assert bad not in meme_rules, f"又写回了许可式措辞：{bad!r}"


def test_output_rules_split_limit_one_is_single() -> None:
    """上限设成 1 时就是纯单条，不该再叫它拆。"""
    rules = build_output_rules(split=True, max_messages=1, meme=False, emotions=[])
    assert "只能写 1 条" in rules
    assert "本轮请写成 2 条" not in rules


def test_output_rules_meme_only() -> None:
    rules = build_output_rules(split=False, max_messages=2, meme=True, emotions=["哭哭", "害羞"])
    assert "只能写 1 条" in rules
    assert "哭哭、害羞" in rules
    assert "本轮请配一个表情" in rules


def test_output_rules_both() -> None:
    rules = build_output_rules(split=True, max_messages=3, meme=True, emotions=["开心"])
    assert "最多 3 条" in rules
    assert "开心" in rules


def test_output_rules_max_is_a_range_not_a_fixed_two() -> None:
    """上限调大后，规则里要跟着变成 2~N，而不是写死两条。"""
    rules = build_output_rules(split=True, max_messages=5, meme=False, emotions=[])
    assert "最多 5 条" in rules


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
SPEC = {"expect_json": True, "allow_split": True, "allow_meme": True, "max_messages": 2}


def test_parse_plain_json() -> None:
    parsed = parse_reply('{"messages": ["在的", "怎么啦"]}', **SPEC)
    assert parsed.ok and parsed.messages == ["在的", "怎么啦"]
    assert parsed.meme is None


def test_parse_with_meme() -> None:
    parsed = parse_reply('{"messages": ["哼"], "meme": "生气"}', emotions=["生气"], **SPEC)
    assert parsed.meme == "生气"


def test_parse_meme_is_validated_against_catalog() -> None:
    """模型瞎编一个目录里没有的情绪时忽略，不能让它把整条回复带崩。"""
    parsed = parse_reply('{"messages": ["哼"], "meme": "不存在"}', emotions=["生气"], **SPEC)
    assert parsed.ok is True
    assert parsed.meme is None


def test_parse_meme_takes_first_of_list() -> None:
    parsed = parse_reply('{"messages": ["哼"], "meme": ["生气", "开心"]}', emotions=["生气", "开心"], **SPEC)
    assert parsed.meme == "生气"


@pytest.mark.parametrize(
    "text",
    [
        '```json\n{"messages": ["在"]}\n```',
        '好的：{"messages": ["在"]}',
        '{"messages": ["在"],}',
        '{"messages": ["在",], "meme": "开心",}',
    ],
)
def test_parse_repairs_common_breakage(text: str) -> None:
    parsed = parse_reply(text, emotions=["开心"], **SPEC)
    assert parsed.ok, f"没修好：{text!r}（{parsed.error}）"
    assert parsed.messages[0] == "在"


def test_parse_rejects_when_json_expected_but_missing() -> None:
    parsed = parse_reply("就是一句普通的话", **SPEC)
    assert parsed.ok is False
    assert parsed.error


def test_parse_accepts_plain_text_when_json_not_expected() -> None:
    """两个功能都关时，普通文本是**正常**的，不该被当成格式错误。"""
    parsed = parse_reply(
        "就是一句普通的话",
        expect_json=False,
        allow_split=False,
        allow_meme=False,
        max_messages=2,
    )
    assert parsed.ok is True
    assert parsed.messages == ["就是一句普通的话"]


def test_parse_unwraps_spontaneous_json_even_when_not_expected() -> None:
    """模型自发输出 JSON 时也要拆开 —— 这是防泄漏的结构性保障。"""
    parsed = parse_reply(
        '{"messages": ["莫名其妙的 JSON"]}',
        expect_json=False,
        allow_split=False,
        allow_meme=False,
        max_messages=2,
    )
    assert parsed.messages == ["莫名其妙的 JSON"], "JSON 泄漏到聊天里了"


def test_parse_merges_when_split_disabled() -> None:
    parsed = parse_reply(
        '{"messages": ["第一句", "第二句"]}',
        expect_json=True,
        allow_split=False,
        allow_meme=False,
        max_messages=2,
    )
    assert parsed.messages == ["第一句\n第二句"], "关了分段就不该发多条"


def test_parse_respects_max_messages() -> None:
    parsed = parse_reply(
        '{"messages": ["1", "2", "3", "4"]}',
        expect_json=True,
        allow_split=True,
        allow_meme=False,
        max_messages=2,
    )
    assert parsed.messages == ["1", "2"]


def test_parse_rejects_empty_messages() -> None:
    assert parse_reply('{"messages": []}', **SPEC).ok is False
    assert parse_reply('{"meme": "开心"}', emotions=["开心"], **SPEC).ok is False


def test_salvage_recovers_quoted_text() -> None:
    """少个逗号这种高频手误，靠抢救就能救回来，不用退化成兜底文案。"""
    rescued = salvage('{"messages": ["第一句" "第二句"]}')
    assert rescued == ["第一句", "第二句"]


def test_salvage_skips_key_names() -> None:
    rescued = salvage('{"messages": ["真的内容"], "meme": "开心"}')
    assert "messages" not in rescued and "meme" not in rescued
    assert rescued[0] == "真的内容"


def test_salvage_returns_empty_for_garbage() -> None:
    assert salvage("") == []
    assert salvage("{}") == []


def test_compose_memory_text() -> None:
    assert compose_memory_text(["在的"], None) == "在的"
    assert compose_memory_text(["在的", "怎么啦"], None) == "在的\n怎么啦"
    assert compose_memory_text(["哼"], "生气") == "哼\n[表情：生气]"
    assert compose_memory_text([], "生气") == "[表情：生气]"


# --------------------------------------------------------------------------- #
# 端到端：掷骰子
# --------------------------------------------------------------------------- #
@pytest.fixture
def memes(tmp_path):
    """准备一个临时表情目录。"""
    make_png(tmp_path / "开心.png", (30, 200, 30))
    make_png(tmp_path / "生气.png", (200, 30, 30))
    return tmp_path


def test_probability_zero_never_splits(ready, fake_openai: FakeOpenAI) -> None:
    ready.settings.update(split_chance=0.0, meme_chance=0.0, split_delay_ms=0)
    _enable(ready, split_enabled=True, meme_enabled=True)
    fake_openai.replies('{"messages": ["一", "二"]}', repeat_last=True)

    _say(ready)

    assert len(_sent_segments(ready.home)) == 1, "概率为 0 却分了两条"


def test_probability_one_always_splits(ready, fake_openai: FakeOpenAI) -> None:
    ready.settings.update(split_chance=1.0, meme_chance=0.0, split_delay_ms=0)
    _enable(ready, split_enabled=True, meme_enabled=False)
    fake_openai.replies('{"messages": ["快到中午了", "你不会刚睡醒吧？"]}', repeat_last=True)

    _say(ready)

    assert _reply_texts_from(ready.home) == ["快到中午了", "你不会刚睡醒吧？"]


def test_split_respects_max_messages(ready, fake_openai: FakeOpenAI) -> None:
    ready.settings.update(split_chance=1.0, meme_chance=0.0, split_max_messages=2, split_delay_ms=0)
    _enable(ready, split_enabled=True)
    fake_openai.replies('{"messages": ["1", "2", "3", "4"]}', repeat_last=True)

    _say(ready)
    assert len(_sent_segments(ready.home)) == 2


def test_split_sends_with_delay(ready, fake_openai: FakeOpenAI) -> None:
    """瞬间连发一眼就是机器人 —— 中间必须有间隔。"""
    ready.settings.update(split_chance=1.0, meme_chance=0.0, split_delay_ms=400)
    _enable(ready, split_enabled=True)
    fake_openai.replies('{"messages": ["第一句", "第二句"]}', repeat_last=True)

    _say(ready, expect=2)

    stamps = [item["at"] for item in _sent(ready.home)]
    assert len(stamps) == 2
    gap = stamps[1] - stamps[0]
    assert gap >= 0.25, f"两条之间只隔了 {gap:.3f}s，太快了"


def test_disabled_persona_never_splits(ready, fake_openai: FakeOpenAI) -> None:
    """概率拉满，但人设没勾选 —— 依然不分。开关优先于概率。"""
    ready.settings.update(split_chance=1.0, meme_chance=0.0, split_delay_ms=0)
    _enable(ready, split_enabled=False)
    fake_openai.replies('{"messages": ["一", "二"]}', repeat_last=True)

    _say(ready)
    assert len(_sent_segments(ready.home)) == 1


# --------------------------------------------------------------------------- #
# 端到端：表情
# --------------------------------------------------------------------------- #
def test_meme_is_attached_when_rolled(ready, fake_openai: FakeOpenAI, memes) -> None:
    ready.settings.update(split_chance=0.0, meme_chance=1.0, meme_dir=str(memes), split_delay_ms=0)
    _enable(ready, split_enabled=False, meme_enabled=True)
    fake_openai.replies('{"messages": ["哼"], "meme": "生气"}', repeat_last=True)

    _say(ready)

    segments = [seg for group in _sent_segments(ready.home) for seg in group]
    images = [seg for seg in segments if seg.get("type") == "image"]
    assert len(images) == 1, f"没有发出表情图：{[s.get('type') for s in segments]}"
    # as_uri() 会把中文百分号编码，断言前先解码
    from urllib.parse import unquote

    assert "生气.png" in unquote(str(images[0]["data"]["file"])), images[0]


def test_meme_absent_when_probability_zero(ready, fake_openai: FakeOpenAI, memes) -> None:
    ready.settings.update(split_chance=0.0, meme_chance=0.0, meme_dir=str(memes), split_delay_ms=0)
    _enable(ready, meme_enabled=True)
    fake_openai.replies('{"messages": ["哼"], "meme": "生气"}', repeat_last=True)

    _say(ready)

    segments = [seg for group in _sent_segments(ready.home) for seg in group]
    assert not [seg for seg in segments if seg.get("type") == "image"], "概率为 0 却发了表情"


def test_unknown_emotion_does_not_break_the_reply(ready, fake_openai: FakeOpenAI, memes) -> None:
    """模型编了个目录里没有的情绪 → 不配图，但话照常说。"""
    ready.settings.update(split_chance=0.0, meme_chance=1.0, meme_dir=str(memes), split_delay_ms=0)
    _enable(ready, meme_enabled=True)
    fake_openai.replies('{"messages": ["哼"], "meme": "外星情绪"}', repeat_last=True)

    _say(ready)

    assert _reply_texts_from(ready.home) == ["哼"]
    segments = [seg for group in _sent_segments(ready.home) for seg in group]
    assert not [seg for seg in segments if seg.get("type") == "image"]


def test_empty_meme_dir_skips_meme(ready, fake_openai: FakeOpenAI, tmp_path) -> None:
    empty = tmp_path / "空目录"
    empty.mkdir()
    ready.settings.update(split_chance=0.0, meme_chance=1.0, meme_dir=str(empty), split_delay_ms=0)
    _enable(ready, meme_enabled=True)
    fake_openai.replies('{"messages": ["哼"], "meme": "生气"}', repeat_last=True)

    _say(ready)
    assert _reply_texts_from(ready.home) == ["哼"]


def test_gates_are_independent(ready, fake_openai: FakeOpenAI, memes) -> None:
    """分段关、表情开 → 只发一条，但可以带表情。"""
    ready.settings.update(split_chance=1.0, meme_chance=1.0, meme_dir=str(memes), split_delay_ms=0)
    _enable(ready, split_enabled=False, meme_enabled=True)
    fake_openai.replies('{"messages": ["一条", "两条"], "meme": "开心"}', repeat_last=True)

    _say(ready)

    texts = _reply_texts_from(ready.home)
    assert texts == ["一条\n两条"], f"关了分段却发了多条：{texts}"
    segments = [seg for group in _sent_segments(ready.home) for seg in group]
    assert [seg for seg in segments if seg.get("type") == "image"], "表情被分段开关连累了"


# --------------------------------------------------------------------------- #
# 端到端：记忆里存什么
# --------------------------------------------------------------------------- #
def test_memory_stores_readable_text_not_json(ready, fake_openai: FakeOpenAI, memes) -> None:
    ready.settings.update(split_chance=1.0, meme_chance=1.0, meme_dir=str(memes), split_delay_ms=0)
    _enable(ready, split_enabled=True, meme_enabled=True)
    fake_openai.replies('{"messages": ["快到中午了", "你不会刚睡醒吧？"], "meme": "开心"}', repeat_last=True)

    _say(ready)

    rows = ready.bot.pipeline.store.memory_messages(memory_key(f"group:{GROUP}", 1))
    assistant = next(r for r in rows if r["role"] == "assistant")
    assert "{" not in assistant["content"] and '"messages"' not in assistant["content"], (
        f"JSON 进记忆了：{assistant['content']!r}"
    )
    assert assistant["content"] == "快到中午了\n你不会刚睡醒吧？\n[表情：开心]"


# --------------------------------------------------------------------------- #
# 端到端：格式错误的三级兜底
# --------------------------------------------------------------------------- #
def test_broken_json_is_regenerated(ready, fake_openai: FakeOpenAI) -> None:
    """第一次给坏的，第二次给好的 —— 应当用第二次的结果。"""
    ready.settings.update(split_chance=1.0, meme_chance=0.0, reply_retry_max=1, split_delay_ms=0)
    _enable(ready, split_enabled=True)
    fake_openai.replies(
        "这不是 JSON",
        '{"messages": ["重新生成好的"]}',
        repeat_last=True,
    )

    _say(ready)

    assert _reply_texts_from(ready.home) == ["重新生成好的"]
    # 第二次请求里应当带上"上次错在哪"
    requests = fake_openai.requests()
    assert len(requests) >= 2
    joined = json.dumps(requests[-1], ensure_ascii=False)
    assert "不是合法的 JSON" in joined, "重新生成时没告诉模型错在哪"


def test_retry_can_be_disabled(ready, fake_openai: FakeOpenAI) -> None:
    ready.settings.update(split_chance=1.0, meme_chance=0.0, reply_retry_max=0, split_delay_ms=0)
    _enable(ready, split_enabled=True)
    fake_openai.replies('{"messages": ["少个逗号" "第二句"]}', repeat_last=True)

    _say(ready)

    # 不重试，但抢救助上了
    assert _reply_texts_from(ready.home) == ["少个逗号", "第二句"]


def test_unrecoverable_json_falls_back(ready, fake_openai: FakeOpenAI) -> None:
    ready.settings.update(split_chance=1.0, meme_chance=0.0, reply_retry_max=1, split_delay_ms=0)
    _enable(ready, split_enabled=True)
    fake_openai.replies("完完全全不是 JSON", repeat_last=True)

    _say(ready)

    assert _reply_texts_from(ready.home) == ["服务器宕机了喵"]


def test_plain_reply_is_unaffected_when_both_off(ready, fake_openai: FakeOpenAI) -> None:
    """两个都关时，普通文本回复**不该**走重试 —— 它本来就是对的。"""
    ready.settings.update(split_chance=1.0, meme_chance=1.0, reply_retry_max=1, split_delay_ms=0)
    _enable(ready, split_enabled=False, meme_enabled=False)
    fake_openai.reset()

    _say(ready, "随便说句话")

    assert fake_openai.requests(), "没有调用模型"
    assert len(fake_openai.requests()) == 1, "普通文本被当成格式错误重试了"
    assert _reply_texts_from(ready.home), "没有回复"


def test_rules_absent_from_prompt_when_both_off(ready, fake_openai: FakeOpenAI) -> None:
    """两个都关时，提示词里**不能出现**输出规则 —— 否则等于又给了模型口子。"""
    ready.settings.update(split_chance=1.0, meme_chance=1.0)
    _enable(ready, split_enabled=False, meme_enabled=False)
    fake_openai.reset()

    _say(ready)

    payload = json.dumps(fake_openai.requests()[-1], ensure_ascii=False)
    assert "【输出规则】" not in payload
    assert "meme" not in payload


# --------------------------------------------------------------------------- #
# 人设级数值：留空跟随全局，填了就覆盖
# --------------------------------------------------------------------------- #
def test_persona_chance_overrides_global(ready, fake_openai: FakeOpenAI) -> None:
    """全局分段概率 0，但人设上设了 100% —— 人设优先。"""
    ready.settings.update(split_chance=0.0, meme_chance=0.0, split_delay_ms=0)
    _enable(ready, split_enabled=True, split_chance=1.0)
    fake_openai.replies('{"messages": ["一", "二"]}', repeat_last=True)

    _say(ready)
    assert len(_sent_segments(ready.home)) == 2, "人设上的概率没生效"


def test_blank_persona_chance_follows_global(ready, fake_openai: FakeOpenAI) -> None:
    """留空必须跟随全局，**不能**被当成 0（0 是"永不触发"）。"""
    ready.settings.update(split_chance=1.0, meme_chance=0.0, split_delay_ms=0)
    _enable(ready, split_enabled=True, split_chance=None)
    fake_openai.replies('{"messages": ["一", "二"]}', repeat_last=True)

    _say(ready)
    assert len(_sent_segments(ready.home)) == 2, "留空没有跟随全局"


def test_persona_zero_chance_means_never(ready, fake_openai: FakeOpenAI) -> None:
    """填 0 和留空是两回事：填 0 就是永不触发。"""
    ready.settings.update(split_chance=1.0, meme_chance=0.0, split_delay_ms=0)
    _enable(ready, split_enabled=True, split_chance=0.0)
    fake_openai.replies('{"messages": ["一", "二"]}', repeat_last=True)

    _say(ready)
    assert len(_sent_segments(ready.home)) == 1, "填 0 却触发了"


def test_persona_delay_is_used(ready, fake_openai: FakeOpenAI) -> None:
    ready.settings.update(split_chance=1.0, meme_chance=0.0, split_delay_ms=0)
    _enable(ready, split_enabled=True, split_delay_ms=400)
    fake_openai.replies('{"messages": ["第一句", "第二句"]}', repeat_last=True)

    _say(ready, expect=2)

    stamps = [item["at"] for item in _sent(ready.home)]
    gap = stamps[1] - stamps[0]
    assert gap >= 0.25, f"人设上的间隔没生效（只隔了 {gap:.3f}s）"


def test_persona_store_round_trips_new_fields(ready) -> None:
    """存进去再读回来必须一致，NULL 不能变成 0。"""
    store = ready.bot.pipeline.personas
    assert store.update(1, {
        "split_chance": 0.4, "split_delay_ms": 800, "meme_chance": 0.3,
        "split_prompt": "自定义分段", "meme_prompt": "自定义表情",
    })["ok"]
    p = store.get(1)
    assert p["split_chance"] == pytest.approx(0.4)
    assert p["split_delay_ms"] == 800
    assert p["meme_chance"] == pytest.approx(0.3)
    assert p["split_prompt"] == "自定义分段"
    assert p["meme_prompt"] == "自定义表情"

    store.update(1, {"split_chance": None, "split_prompt": ""})
    p = store.get(1)
    assert p["split_chance"] is None, "留空应当存成 NULL，而不是 0（0 = 永不触发）"
    # get() 返回的是原始行（NULL 就是 None）；界面读的是 list_public()，
    # 那一层会把 None 归一化成空串
    assert p["split_prompt"] is None
    public = next(item for item in store.list_public() if item["uid"] == 1)
    assert public["split_prompt"] == ""
    assert public["split_chance"] is None


def test_persona_store_validates_ranges(ready) -> None:
    errors = ready.bot.pipeline.personas.validate({"split_chance": 5, "split_delay_ms": -1})
    assert "split_chance" in errors and "split_delay_ms" in errors


# --------------------------------------------------------------------------- #
# 自定义提示词
# --------------------------------------------------------------------------- #
def test_custom_split_prompt_replaces_builtin() -> None:
    rules = build_output_rules(
        split=True, max_messages=3, meme=False, emotions=[],
        split_prompt="给我拆成 {max_messages} 段，每段不超过 {chars} 字",
    )
    assert "给我拆成 3 段，每段不超过 40 字" in rules
    assert "像真人打字时" not in rules, "内置文案没被替换掉"


def test_custom_meme_prompt_gets_emotion_list() -> None:
    rules = build_output_rules(
        split=False, max_messages=2, meme=True, emotions=["嫌弃", "害羞"],
        meme_prompt="从 {emotions} 里挑一个最贴的",
    )
    assert "从 嫌弃、害羞 里挑一个最贴的" in rules


def test_custom_prompt_with_stray_braces_does_not_crash() -> None:
    """用户可能在提示词里写示例 JSON，花括号不该把整轮回复搞崩。"""
    rules = build_output_rules(
        split=True, max_messages=2, meme=False, emotions=[],
        split_prompt='照这个格式：{"messages": ["a", "b"]}',
    )
    assert '{"messages": ["a", "b"]}' in rules


def test_persona_custom_prompt_reaches_the_model(ready, fake_openai: FakeOpenAI) -> None:
    ready.settings.update(split_chance=1.0, meme_chance=0.0, split_delay_ms=0)
    _enable(ready, split_enabled=True, split_prompt="这是我自己写的分段规则喵")
    fake_openai.replies('{"messages": ["一"]}', repeat_last=True)

    _say(ready)

    payload = json.dumps(fake_openai.requests()[-1], ensure_ascii=False)
    assert "这是我自己写的分段规则喵" in payload


def test_preview_prompt_shows_everything() -> None:
    """总体预览要把人设提示词、【输出规则】、摘要占位、示例用户消息都列出来。"""
    res = build_preview(
        system_prompt="你是米蛋，一只傲娇的猫娘。",
        rules=build_output_rules(
            split=True, max_messages=2, meme=True, emotions=["嫌弃"],
            split_prompt="自定义分段规则",
        ),
        enabled_note="多段连发概率 25%、发送图片概率 20%",
    )

    titles = [b["title"] for b in res["blocks"]]
    assert any("人设提示词" in t for t in titles)
    assert any("输出规则" in t for t in titles)
    assert any("长期记忆摘要" in t for t in titles)
    assert any("用户消息" in t for t in titles)
    assert "自定义分段规则" in res["text"]
    assert "你是米蛋" in res["text"]
    assert "昵称：" in res["text"], "示例用户消息应当体现三行块格式"
    assert "多段连发概率" in res["note"]


def test_preview_prompt_handles_missing_system_prompt() -> None:
    res = build_preview(system_prompt="", rules="", enabled_note="")
    assert "还没写提示词" in res["text"]
    # 规则为空时那一块不该出现（两个功能都关的情况）
    assert not any("输出规则" in b["title"] for b in res["blocks"])
