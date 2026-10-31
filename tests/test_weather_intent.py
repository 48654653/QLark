"""趣味功能·天气接进对话：关键词 → 意图 JSON → 注入【实时天气】→ 正常回复。

刻意**不用 function calling**：这个端点上 ``tools`` 与 ``response_format``
互斥（实测：带 json_object 时模型会把 tool call 写进 content）。所以走
"意图 JSON"这条路：一次极小调用解析城市，再用现有协议正常回复。

这里打的是真实流水线（假 NapCat + 假 OpenAI），只把和风的 HTTP 换掉。
"""

from __future__ import annotations

import pytest

from conftest import FakeOpenAI, wait_for

from qlark.fun import weather as fun_weather

from test_m2_integration import _inject, _reply_texts, _reset, ready  # noqa: F401

FAKE_BRIEF = "成都现在 18℃ 阴，体感 17℃，北风2级，湿度 85%。今天 小雨 20/15℃。"


@pytest.fixture
def weather_ready(ready, monkeypatch):
    """打开天气功能，并把和风的 HTTP 换成固定结果。"""
    ready.settings.update(
        weather_enabled=True,
        weather_keywords=[],  # 空 = 用内置默认词表
        qweather_host="demo1234.re.qweatherapi.com",
        qweather_key="fake-key-0123456789abcdef",
        qweather_city="北京",
        memory_rounds=50,
        auto_summary=False,
    )
    monkeypatch.setattr(
        fun_weather.QWeather, "brief", lambda self, city: f"{city}现在 18℃ 阴，体感 17℃，北风2级，湿度 85%。今天 小雨 20/15℃。"
    )
    # 逐条测试都从"没聊过天气"开始，否则话题延续会让计数断言飘
    ready.bot.pipeline._weather_cache.clear()
    ready.bot.pipeline._weather_seen.clear()
    ready.bot.pipeline._weather_last_city.clear()
    return ready


def _say(env, text: str, *, message_id: int = 930) -> None:
    _reset(env.home)
    _inject(
        env.home,
        message_type="private",
        user_id="20000002",
        message_id=message_id,
        text=text,
    )
    assert wait_for(lambda: _reply_texts(env.home), timeout=60), "没有回复"


def _system_and_user(payload: dict) -> str:
    parts: list[str] = []
    for message in payload.get("messages") or []:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return "\n".join(parts)


def test_weather_is_injected_into_the_reply(weather_ready, fake_openai: FakeOpenAI) -> None:
    fake_openai.reset()
    # 「成都天气怎么样」走快路：城市直接从句子里取，只有回复这一次调用
    fake_openai.replies('{"messages": ["成都 18℃，阴，出门带件外套"]}', repeat_last=True)

    _say(weather_ready, "成都天气怎么样")

    payload = fake_openai.requests()[-1]
    joined = _system_and_user(payload)
    assert "【实时天气】" in joined, f"没有注入天气：{joined[-300:]}"
    assert "成都现在 18℃ 阴" in joined
    assert weather_ready.bot.pipeline.stats.weathers >= 1
    assert _reply_texts(weather_ready.home), "注入天气后仍要正常回复"


def test_city_in_sentence_skips_the_intent_call(weather_ready, fake_openai: FakeOpenAI) -> None:
    """「成都热死了」里的城市直接取出来，不再花一次模型调用（实测那次 8s 超时就死在这）。"""
    fake_openai.reset()
    fake_openai.replies('{"messages": ["成都 19℃ 是挺闷的"]}', repeat_last=True)

    _say(weather_ready, "成都热死了", message_id=960)

    payloads = fake_openai.requests()
    assert len(payloads) == 1, f"不该有意图调用，实际打了 {len(payloads)} 次"
    joined = _system_and_user(payloads[0])
    assert "【实时天气】" in joined
    assert "成都现在 18℃ 阴" in joined


def test_bad_city_guess_falls_back_to_intent(
    weather_ready, fake_openai: FakeOpenAI, monkeypatch
) -> None:
    """猜出来的名字查不到时，退回意图调用，而不是拿错城市硬答。"""
    known = {"成都", "北京", "杭州", "伦敦", "洛阳", "菏泽", "洛宁"}

    def brief(self, city):  # noqa: ANN001
        if city not in known:
            raise fun_weather.WeatherError("没有找到城市")
        return f"{city}现在 18℃ 阴。"

    monkeypatch.setattr(fun_weather.QWeather, "brief", brief)
    fake_openai.reset()
    fake_openai.replies('{"city": "火星"}', '{"messages": ["火星我查不到"]}', repeat_last=True)

    _say(weather_ready, "火星天气预报", message_id=961)

    payloads = fake_openai.requests()
    assert len(payloads) >= 2, "猜错城市后应当退回意图调用"
    intent_text = _system_and_user(payloads[0])
    assert "意图" in intent_text or "intent" in intent_text.lower() or "JSON" in intent_text
    joined = _system_and_user(payloads[-1])
    assert "【实时天气】" not in joined
    assert _reply_texts(weather_ready.home)


def test_weather_skipped_when_model_says_not_needed(weather_ready, fake_openai: FakeOpenAI) -> None:
    """模型判断"不需要实时数据"时，不该白查一次和风。"""
    fake_openai.reset()
    fake_openai.replies('{"city": null}', '{"messages": ["今天确实挺热的"]}', repeat_last=True)

    _say(weather_ready, "今天天气好热啊")

    joined = _system_and_user(fake_openai.requests()[-1])
    assert "【实时天气】" not in joined
    assert _reply_texts(weather_ready.home)


def test_weather_failure_does_not_break_the_reply(weather_ready, fake_openai: FakeOpenAI, monkeypatch) -> None:
    """和风挂了也只当没有天气，回复必须照常。"""
    def boom(self, city):  # noqa: ANN001
        raise fun_weather.WeatherError("连不上和风（4s 超时）")

    monkeypatch.setattr(fun_weather.QWeather, "brief", boom)
    fake_openai.reset()
    fake_openai.replies('{"city": "成都"}', '{"messages": ["我这边查不到天气"]}', repeat_last=True)

    _say(weather_ready, "成都天气怎么样", message_id=931)

    joined = _system_and_user(fake_openai.requests()[-1])
    assert "【实时天气】" not in joined
    assert _reply_texts(weather_ready.home), "天气失败不该让回复失联"


def test_weather_ignored_when_fun_disabled(weather_ready, fake_openai: FakeOpenAI) -> None:
    weather_ready.settings.update(weather_enabled=False)
    fake_openai.reset()
    # 开关关着时只有"回复"这一次调用，所以队列里只放一条
    fake_openai.replies('{"messages": ["嗯"]}', repeat_last=True)

    _say(weather_ready, "成都天气怎么样", message_id=932)

    joined = _system_and_user(fake_openai.requests()[-1])
    assert "【实时天气】" not in joined
    # 开关关着时连意图解析都不该发生：只有一次回复请求
    assert len(fake_openai.requests()) == 1


def test_custom_keyword_table_controls_trigger(weather_ready, fake_openai: FakeOpenAI) -> None:
    """词表换了，触发条件就跟着换：这是"可编辑数据表"的核心承诺。"""
    weather_ready.settings.update(weather_keywords=["紫外线"])

    # "天气"已经不在表里、也没在聊天气 → 不触发，只有一次回复调用
    fake_openai.reset()
    fake_openai.replies('{"messages": ["成都天气还行吧"]}', repeat_last=True)
    _say(weather_ready, "成都天气怎么样", message_id=935)
    assert len(fake_openai.requests()) == 1
    assert "【实时天气】" not in _system_and_user(fake_openai.requests()[-1])

    # 表里有的"紫外线" → 意图调用 + 查天气 + 注入
    fake_openai.reset()
    fake_openai.replies('{"city": "成都"}', '{"messages": ["成都紫外线中等"]}', repeat_last=True)
    _say(weather_ready, "成都紫外线强吗", message_id=936)
    assert "【实时天气】" in _system_and_user(fake_openai.requests()[-1])


def test_weather_cache_avoids_second_api_call(weather_ready, fake_openai: FakeOpenAI, monkeypatch) -> None:
    """同一城市 10 分钟内不重复打和风。"""
    calls: list[str] = []

    def brief(self, city):  # noqa: ANN001
        calls.append(city)
        return f"{city}现在 18℃ 阴。"

    monkeypatch.setattr(fun_weather.QWeather, "brief", brief)
    fake_openai.reset()
    fake_openai.replies('{"city": "成都"}', '{"messages": ["成都 18℃"]}', repeat_last=True)

    _say(weather_ready, "成都天气怎么样", message_id=933)
    _say(weather_ready, "成都天气怎么样", message_id=934)

    assert calls == ["成都"], f"第二次应当命中缓存，实际打了 {len(calls)} 次"


# --------------------------------------------------------------------------- #
# 省略问句（实测踩过的坑）
# --------------------------------------------------------------------------- #
def test_followup_without_weather_word_is_still_queried(
    weather_ready, fake_openai: FakeOpenAI
) -> None:
    """"洛宁呢" 里一个天气词都没有，但刚才在聊天气，就该接着查。"""
    fake_openai.reset()
    fake_openai.replies('{"city": "洛阳"}', '{"messages": ["洛阳 15℃ 雾"]}', repeat_last=True)
    _say(weather_ready, "洛阳天气如何", message_id=940)

    fake_openai.reset()
    fake_openai.replies('{"city": "洛宁"}', '{"messages": ["洛宁 14℃ 阴"]}', repeat_last=True)
    _say(weather_ready, "洛宁呢", message_id=941)

    joined = _system_and_user(fake_openai.requests()[-1])
    assert "【实时天气】" in joined, f"省略问句没接住：{joined[-300:]}"
    assert "洛宁现在 18℃" in joined


def test_bare_city_name_is_treated_as_followup(weather_ready, fake_openai: FakeOpenAI) -> None:
    """用户只发"菏泽"两个字 —— 这也是在问天气（前面刚聊过）。"""
    fake_openai.reset()
    fake_openai.replies('{"city": "洛阳"}', '{"messages": ["洛阳 15℃ 雾"]}', repeat_last=True)
    _say(weather_ready, "洛阳天气如何", message_id=942)

    fake_openai.reset()
    fake_openai.replies('{"city": "菏泽"}', '{"messages": ["菏泽 20℃ 晴"]}', repeat_last=True)
    _say(weather_ready, "菏泽", message_id=943)

    joined = _system_and_user(fake_openai.requests()[-1])
    assert "【实时天气】" in joined
    assert "菏泽现在 18℃" in joined


def test_followup_window_expires(weather_ready, fake_openai: FakeOpenAI, monkeypatch) -> None:
    """超过"余温"窗口就不再当作在聊天气，省一次调用。"""
    monkeypatch.setattr(type(weather_ready.bot.pipeline), "WEATHER_FOLLOWUP_WINDOW", 0.0)
    fake_openai.reset()
    fake_openai.replies('{"city": "洛阳"}', '{"messages": ["洛阳 15℃"]}', repeat_last=True)
    _say(weather_ready, "洛阳天气如何", message_id=944)

    fake_openai.reset()
    fake_openai.replies('{"messages": ["嗯"]}', repeat_last=True)
    _say(weather_ready, "洛宁呢", message_id=945)

    assert len(fake_openai.requests()) == 1, "窗口过期后不该再多一次意图调用"
    assert "【实时天气】" not in _system_and_user(fake_openai.requests()[-1])


def test_followup_carries_last_city_into_intent(weather_ready, fake_openai: FakeOpenAI) -> None:
    """"那明天呢"要能继承上文城市，否则会拿默认城市去查。"""
    fake_openai.reset()
    fake_openai.replies('{"city": "洛阳"}', '{"messages": ["洛阳 15℃"]}', repeat_last=True)
    _say(weather_ready, "洛阳天气如何", message_id=948)

    fake_openai.reset()
    fake_openai.replies('{"city": "洛阳"}', '{"messages": ["洛阳明天小雨"]}', repeat_last=True)
    _say(weather_ready, "那明天呢", message_id=949)

    intent_request = fake_openai.requests()[0]
    intent_text = _system_and_user(intent_request)
    assert "上文刚问过：洛阳" in intent_text, intent_text[-300:]
    assert "【实时天气】" in _system_and_user(fake_openai.requests()[-1])


def test_failed_lookup_forbids_empty_promise(
    weather_ready, fake_openai: FakeOpenAI, monkeypatch
) -> None:
    """查不到时明确禁止"我查一下，稍等" —— 那种回复等于挖坑不填。"""
    def boom(self, city):  # noqa: ANN001
        raise fun_weather.WeatherError("没有找到城市")

    monkeypatch.setattr(fun_weather.QWeather, "brief", boom)
    fake_openai.reset()
    fake_openai.replies('{"city": "洛宁"}', '{"messages": ["洛宁查不到"]}', repeat_last=True)

    _say(weather_ready, "洛宁天气怎么样", message_id=946)

    joined = _system_and_user(fake_openai.requests()[-1])
    assert "【实时天气】" not in joined
    assert "不要说自己没有实时数据" not in joined, "没数据时不该用'直接用数据'那段提示"
    assert "这轮没有实时数据" in joined or "我查一下" in joined, joined[-300:]


def test_no_promise_note_applies_when_no_weather_context(
    weather_ready, fake_openai: FakeOpenAI
) -> None:
    """功能开着、但这句跟天气无关时，也要禁止"我查一下"这种空头承诺。"""
    fake_openai.reset()
    fake_openai.replies('{"messages": ["吃面吧"]}', repeat_last=True)

    _say(weather_ready, "今天中午吃什么", message_id=947)

    joined = _system_and_user(fake_openai.requests()[-1])
    assert "手头没有实时数据" in joined, joined[-300:]
    assert len(fake_openai.requests()) == 1


def test_no_promise_note_does_not_claim_incapability() -> None:
    """约束只能说"别空口承诺"，不能说"你没有联网能力"。

    真实事故：上一轮真查到了伦敦的天气，下一轮模型读到"你没有联网查询能力"，
    立刻把**正确的数据**也否了 ——「其实本鲸刚才在瞎编，我没有查天气的能力」。
    这种假道歉比不回答更伤，所以措辞必须守住。
    """
    from qlark.chat.pipeline import MessagePipeline

    note = MessagePipeline.WEATHER_NO_PROMISE_NOTE
    for forbidden in ("没有联网", "没有查天气", "不具备", "无法联网"):
        assert forbidden not in note, f"禁止再出现「{forbidden}」式自贬：{note}"
    assert "不知道" in note


def test_correction_about_city_is_queried(weather_ready, fake_openai: FakeOpenAI) -> None:
    """用户更正城市（"我不在伦敦，我在杭州"）时要真的去查杭州。

    这句既没有天气词、也不是纯地名，以前被长度/标点规则挡在外面，
    模型只能顺着上一轮硬聊，甚至反过来否认自己查过。
    """
    fake_openai.reset()
    fake_openai.replies('{"city": "伦敦"}', '{"messages": ["伦敦 7℃ 多云"]}', repeat_last=True)
    _say(weather_ready, "伦敦现在天气如何", message_id=950)

    fake_openai.reset()
    fake_openai.replies('{"city": "杭州"}', '{"messages": ["杭州 22℃ 晴"]}', repeat_last=True)
    _say(weather_ready, "我不在伦敦，我在杭州", message_id=951)

    payloads = fake_openai.requests()
    intent_text = _system_and_user(payloads[0])
    assert "上文刚问过：伦敦" in intent_text, intent_text[-200:]
    joined = _system_and_user(payloads[-1])
    assert "【实时天气】" in joined, f"更正句没有触发查询：{joined[-300:]}"
    assert "杭州现在 18℃" in joined


def test_correction_without_weather_context_is_ignored(
    weather_ready, fake_openai: FakeOpenAI
) -> None:
    """没在聊天气时，"我在杭州" 不该触发查询（省一次调用）。"""
    fake_openai.reset()
    fake_openai.replies('{"messages": ["杭州挺好"]}', repeat_last=True)

    _say(weather_ready, "我不在伦敦，我在杭州", message_id=952)

    assert len(fake_openai.requests()) == 1
    assert "【实时天气】" not in _system_and_user(fake_openai.requests()[-1])

