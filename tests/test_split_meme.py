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

from conftest import FakeOpenAI, _get, wait_for, wait_memory
from qlark.chat.memes import MemeLibrary
from qlark.chat.reply_format import (
    MEME_PRESETS,
    MEME_RULE_DEFAULT,
    SPLIT_PRESETS,
    SPLIT_RULE_DEFAULT,
    build_output_rules,
    build_preview,
    compose_memory_text,
    parse_reply,
    preset_text,
    salvage,
)
from qlark.chat.store import memory_key

from test_m2_integration import _inject, _reply_texts, _reset, ready  # noqa: F401

GROUP = "888888"

#: v2 的解析参数：**永远是 JSON**，最多 3 条，语音默认不开。
#: 原来这里还带着 ``allow_split`` / ``allow_meme`` 两个骰子开关，已经删掉了。
SPEC = dict(expect_json=True, max_messages=3)


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
    """记忆里存**一行**普通文本，且**不记表情**。"""
    assert compose_memory_text(["嗯"]) == "嗯"
    assert compose_memory_text(["快到中午了", "你不会刚睡醒吧？"]) == "快到中午了，你不会刚睡醒吧？"
    assert compose_memory_text([]) == ""
    assert compose_memory_text(["  ", ""]) == ""


def test_compose_memory_text_avoids_double_punctuation() -> None:
    """上一句已经有句末标点就直接接，别拼出「。，」这种。"""
    assert compose_memory_text(["好的。", "我这就去。"]) == "好的。我这就去。"
    assert compose_memory_text(["真的吗！", "太好了"]) == "真的吗！太好了"
    assert compose_memory_text(["诶…", "算了"]) == "诶…算了"


def test_memory_never_records_the_meme_marker() -> None:
    """**这条是防回归的关键。**

    以前存成 ``…\\n[表情：嫌弃]``，结果没掷中表情的轮次里，模型看着历史照着模仿，
    把 ``[表情：xxx]`` 当正文吐出来发给用户。历史里出现过什么格式，模型就会学。
    """
    text = compose_memory_text(["哼", "才不是呢"])
    assert "[表情" not in text
    assert "\n" not in text, "多行内容同样会被模仿，必须拼成一行"


# --------------------------------------------------------------------------- #
# 端到端：掷骰子
# --------------------------------------------------------------------------- #
@pytest.fixture
def memes(tmp_path):
    """准备一个临时表情目录。"""
    make_png(tmp_path / "开心.png", (30, 200, 30))
    make_png(tmp_path / "生气.png", (200, 30, 30))
    return tmp_path


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


def test_memory_stores_the_raw_json(ready, fake_openai: FakeOpenAI, memes) -> None:
    """v2.1：记忆里存**模型原始输出的 JSON**，不再简化成自然语言。

    两个理由：
      ① 后台调试时能看到真实输出，不用猜它当时到底怎么回的
      ② 模型回看历史时看到的示范也是 JSON，**格式自洽** ——
         如果历史里全是散文，它会慢慢被带跑，越来越不肯输出 JSON

    （旧版正好相反：刻意存自然语言、把表情标注也去掉，怕模型模仿。
      那是因为骰子时代"要不要 JSON"本身是随机的，历史里的 JSON 示例
      会和当轮要求打架。v2 每轮都要求 JSON，这个矛盾就不存在了。）
    """
    ready.settings.update(split_chance=1.0, meme_chance=1.0, meme_dir=str(memes), split_delay_ms=0)
    _enable(ready, split_enabled=True, meme_enabled=True)
    raw = '{"messages": ["快到中午了", "你不会刚睡醒吧？"], "meme": "开心"}'
    fake_openai.replies(raw, repeat_last=True)

    _say(ready)

    key = memory_key(f"group:{GROUP}", 1)
    assert wait_memory(ready.bot.pipeline.store, key, min_rows=2), "记忆没落库"
    rows = ready.bot.pipeline.store.memory_messages(key)
    assistant = next(r for r in rows if r["role"] == "assistant")
    assert assistant["content"] == raw, assistant["content"]
    assert '"messages"' in assistant["content"], "原始 JSON 应该原样保留"
    assert "[表情" not in assistant["content"], "不该再混进表情标注"


def test_memory_sends_json_back_to_the_model(ready, fake_openai: FakeOpenAI, memes) -> None:
    """第二轮请求里，历史中的助手消息也应该还是 JSON —— 格式自洽。"""
    ready.settings.update(split_chance=1.0, meme_chance=1.0, meme_dir=str(memes), split_delay_ms=0)
    _enable(ready, split_enabled=True, meme_enabled=True)
    fake_openai.replies('{"messages": ["嗯"], "meme": null}', repeat_last=True)

    _say(ready)
    assert _reply_texts_from(ready.home) == ["嗯"]

    fake_openai.reset()
    _say(ready, "接着说", message_id=2)
    assert wait_for(lambda: fake_openai.requests(), timeout=30)

    messages = fake_openai.requests()[-1]["messages"]
    assistant = [m for m in messages if m.get("role") == "assistant"]
    assert assistant, "历史里应该有助手说过的话"
    assert all('"messages"' in str(m.get("content")) for m in assistant), (
        f"历史里的助手消息应该保持 JSON 格式：{assistant[-1]}"
    )


def test_rules_are_present_every_round(ready, fake_openai: FakeOpenAI, memes) -> None:
    """v2：**规则每一轮都在**，不再有"掷中了才注入"。

    旧版这里断言的是"没掷中就不该出现【输出规则】"。骰子删掉之后，
    规则恒定存在 —— 但历史里仍然不能残留任何可模仿的格式标记。
    """
    ready.settings.update(meme_dir=str(memes), split_delay_ms=0)
    _enable(ready, split_enabled=True, meme_enabled=True)
    fake_openai.replies('{"messages": ["一", "二"], "meme": "开心"}', repeat_last=True)
    _say(ready, expect=2)

    fake_openai.reset()
    _say(ready, message_id=99, expect=2)

    payload = json.dumps(fake_openai.requests()[-1], ensure_ascii=False)
    assert "【输出规则】" in payload, "规则应该每一轮都注入"
    assert "[表情" not in payload, "历史里留着表情标注，模型会照着学"
    assert '\\n[表情' not in payload


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
def test_preview_prompt_shows_everything() -> None:
    """总体预览要把人设提示词、【输出规则】、摘要占位、示例用户消息都列出来。"""
    res = build_preview(
        system_prompt="你是米蛋，一只傲娇的猫娘。",
        rules=build_output_rules(emotions=["嫌弃"], max_messages=3, voice=True),
        enabled_note="语音回复已开启、发送图片已开启",
    )

    titles = [b["title"] for b in res["blocks"]]
    assert any("人设提示词" in t for t in titles)
    assert any("输出规则" in t for t in titles)
    assert any("长期记忆摘要" in t for t in titles)
    assert any("用户消息" in t for t in titles)
    assert "只输出一个 JSON 对象" in res["text"]
    assert "voice" in res["text"], "开了语音就要在规则里体现"
    assert "你是米蛋" in res["text"]
    assert "昵称：" in res["text"], "示例用户消息应当体现三行块格式"
    assert "语音回复已开启" in res["note"]


def test_preview_prompt_handles_missing_system_prompt() -> None:
    res = build_preview(system_prompt="", rules="", enabled_note="")
    assert "还没写提示词" in res["text"]
    # 规则为空时那一块不该出现（两个功能都关的情况）
    assert not any("输出规则" in b["title"] for b in res["blocks"])


# --------------------------------------------------------------------------- #
# 内置提示词模板
# --------------------------------------------------------------------------- #
def test_presets_are_available_and_well_formed() -> None:
    assert len(SPLIT_PRESETS) >= 3, "多段对话至少要给几套挑"
    assert len(MEME_PRESETS) >= 3
    for group in (SPLIT_PRESETS, MEME_PRESETS):
        ids = [item["id"] for item in group]
        assert len(ids) == len(set(ids)), f"模板 id 重复：{ids}"
        for item in group:
            assert item["name"] and item["text"], item


def test_first_preset_is_the_builtin_default() -> None:
    """第一套就是留空时用的那套 —— 否则"默认"和"模板列表"对不上。"""
    assert SPLIT_PRESETS[0]["text"] == SPLIT_RULE_DEFAULT
    assert MEME_PRESETS[0]["text"] == MEME_RULE_DEFAULT


def test_preset_text_lookup() -> None:
    assert preset_text(SPLIT_PRESETS, "default") == SPLIT_RULE_DEFAULT
    assert preset_text(SPLIT_PRESETS, "escalate") == SPLIT_PRESETS[1]["text"]
    assert preset_text(SPLIT_PRESETS, "") == ""
    assert preset_text(SPLIT_PRESETS, "并不存在") == ""
