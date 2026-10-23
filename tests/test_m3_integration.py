"""M3 端到端：人设切换走真实流水线。

链路与 M2 相同（假 NapCat + 假 OpenAI），额外验证：指令不走模型、不写记忆，
以及切换后**下一条消息真的用了新人设的系统提示词**。
"""

from __future__ import annotations

import time

import pytest

from conftest import FakeOpenAI, _get, _post, wait_for
from qlark.chat.store import memory_key
from qlark.core.models import Phase


def _inject(home, **kwargs) -> dict:
    return _post(f"http://127.0.0.1:{home.port}/_qlark/inject", kwargs)


def _sent(home) -> list[dict]:
    return _get(f"http://127.0.0.1:{home.port}/_qlark/sent").get("sent", [])


def _reply_texts(home) -> list[str]:
    texts: list[str] = []
    for item in _sent(home):
        for segment in (item.get("params") or {}).get("message") or []:
            if segment.get("type") == "text":
                texts.append(str((segment.get("data") or {}).get("text") or ""))
    return texts


def _reset(home) -> None:
    _post(f"http://127.0.0.1:{home.port}/_qlark/reset")


@pytest.fixture
def live(env, fake_openai: FakeOpenAI, monkeypatch):
    env.settings.update(
        base_url=fake_openai.base_url,
        api_key="test-key",
        model="fake-model",
        max_tokens=256,
    )
    monkeypatch.setenv("FAKE_NAPCAT_HAVE_CRED", "1")
    env.settings.qq_account = env.home.qq

    env.manager.start()
    assert wait_for(lambda: env.manager.phase is Phase.READY, timeout=120), (
        f"未进入 READY：{env.manager.phase} / {env.manager.last_error}"
    )
    assert wait_for(lambda: env.bot.running, timeout=20)
    _reset(env.home)
    env.personas = env.bot.pipeline.personas
    return env


USER = "100001"
GROUP = "123456"


def _add_persona(live, name: str, prompt: str = "") -> int:
    created = live.personas.create(name)
    uid = int(created["uid"])
    live.personas.update(uid, {"name": name, "system_prompt": prompt, "enabled": True})
    return uid


# --------------------------------------------------------------------------- #
# /person 走真实链路
# --------------------------------------------------------------------------- #
def test_person_switch_in_group_by_whitelisted_user(live) -> None:
    _add_persona(live, "傲娇女仆", "你是傲娇女仆。")
    live.personas.add_whitelist(USER)
    live.settings.set("persona_whitelist_only", True)

    _inject(live.home, user_id=USER, group_id=GROUP, text="　/person 2")
    assert wait_for(lambda: _reply_texts(live.home), timeout=30), "指令没有任何回复"

    texts = _reply_texts(live.home)
    assert any("已切换为人设 2" in text for text in texts), texts
    assert any("傲娇女仆" in text for text in texts), texts

    # 会话确实切过去了
    active = live.bot.pipeline.store.session_persona(
        f"group:{GROUP}", session_type="group", session_ref=GROUP
    )
    assert int(live.personas.get(active)["slot"]) == 2


def test_person_switch_denied_for_ordinary_member(live) -> None:
    _add_persona(live, "傲娇女仆")
    live.settings.set("persona_whitelist_only", True)

    _inject(live.home, user_id="200002", group_id=GROUP, text="/person 2")
    assert wait_for(lambda: _reply_texts(live.home), timeout=30)
    assert any("权限" in text for text in _reply_texts(live.home))

    active = live.bot.pipeline.store.session_persona(
        f"group:{GROUP}", session_type="group", session_ref=GROUP
    )
    assert active == 1, "没有权限却切换成功了"


def test_person_switch_allowed_when_toggle_off(live) -> None:
    _add_persona(live, "傲娇女仆")
    live.settings.set("persona_whitelist_only", False)

    _inject(live.home, user_id="200002", group_id=GROUP, text="/person 2")
    assert wait_for(lambda: _reply_texts(live.home), timeout=30)
    assert any("已切换为人设 2" in text for text in _reply_texts(live.home))


def test_person_switch_in_private_without_at(live) -> None:
    _add_persona(live, "傲娇女仆")
    live.settings.set("persona_whitelist_only", True)

    _inject(live.home, message_type="private", user_id=USER, text="/person 2")
    assert wait_for(lambda: _reply_texts(live.home), timeout=30)
    assert any("已切换为人设 2" in text for text in _reply_texts(live.home))


# --------------------------------------------------------------------------- #
# 指令不经过模型、不写入记忆
# --------------------------------------------------------------------------- #
def test_command_does_not_call_the_model(live, fake_openai: FakeOpenAI) -> None:
    _add_persona(live, "傲娇女仆")
    live.personas.add_whitelist(USER)
    fake_openai.reset()

    _inject(live.home, user_id=USER, text="/person 2")
    assert wait_for(lambda: _reply_texts(live.home), timeout=30)

    assert fake_openai.requests() == [], "指令不该调用模型"


def test_command_is_not_written_to_memory(live) -> None:
    _add_persona(live, "傲娇女仆")
    live.personas.add_whitelist(USER)

    _inject(live.home, user_id=USER, text="/person 2")
    assert wait_for(lambda: _reply_texts(live.home), timeout=30)
    time.sleep(0.8)

    for slot_uid in (1, 2):
        key = memory_key(f"group:{GROUP}", slot_uid)
        assert live.bot.pipeline.store.message_count(key) == 0, "指令被写进了记忆"


def test_help_command(live) -> None:
    _inject(live.home, text="/help")
    assert wait_for(lambda: _reply_texts(live.home), timeout=30)
    joined = "\n".join(_reply_texts(live.home))
    assert "/person" in joined and "/status" in joined


def test_status_command(live) -> None:
    _inject(live.home, text="/status")
    assert wait_for(lambda: _reply_texts(live.home), timeout=30)
    joined = "\n".join(_reply_texts(live.home))
    assert f"group:{GROUP}" in joined


def test_reset_command_clears_memory(live) -> None:
    live.personas.add_whitelist(USER)
    key = memory_key(f"group:{GROUP}", 1)
    store = live.bot.pipeline.store

    _inject(live.home, user_id=USER, text="记住这句话")
    assert wait_for(lambda: _reply_texts(live.home), timeout=30)
    # 先发送、后落库（见 pipeline），查库必须等
    assert wait_for(lambda: store.message_count(key) == 2, timeout=10), "记忆没落库"

    _reset(live.home)
    _inject(live.home, user_id=USER, text="/reset")
    assert wait_for(lambda: _reply_texts(live.home), timeout=30)
    assert wait_for(lambda: store.message_count(key) == 0, timeout=10), "/reset 没清掉记忆"


# --------------------------------------------------------------------------- #
# 人设真的生效
# --------------------------------------------------------------------------- #
def test_persona_system_prompt_reaches_the_model(live, fake_openai: FakeOpenAI) -> None:
    """切换人设后，下一条消息必须带上新人设的系统提示词。"""
    _add_persona(live, "傲娇女仆", "你是傲娇女仆，说话带刺。")
    live.personas.add_whitelist(USER)

    _inject(live.home, user_id=USER, text="/person 2")
    assert wait_for(lambda: _reply_texts(live.home), timeout=30)

    _reset(live.home)
    _inject(live.home, user_id=USER, text="你好")
    assert wait_for(lambda: _reply_texts(live.home), timeout=30)

    requests = fake_openai.requests()
    assert requests, "没有发起模型调用"
    messages = requests[-1]["messages"]
    assert messages[0]["role"] == "system"
    assert "傲娇女仆" in messages[0]["content"]
    # 默认是群聊消息，正文带着「昵称 / 时间 / 消息」三行块
    # （v2：末尾还跟着格式提醒，所以用 in 而不是 endswith）
    last = str(messages[-1]["content"])
    assert "消息：你好" in last
    assert last.rstrip().endswith("不要写别的话。）"), "末尾应当有格式提醒"


def test_persona_memory_is_isolated(live) -> None:
    """人设之间的记忆不互通（需求规格 §6.1）。"""
    _add_persona(live, "傲娇女仆")
    live.personas.add_whitelist(USER)
    store = live.bot.pipeline.store

    _inject(live.home, user_id=USER, text="人设一的事")
    assert wait_for(lambda: _reply_texts(live.home), timeout=30)
    # 产品是"先发送、后落库"（故意如此，保证记忆里不会出现有问无答的空洞），
    # 所以收到回复 ≠ 已经写进数据库了。查库必须等，否则满负载时必然偶发失败。
    assert wait_for(
        lambda: store.message_count(memory_key(f"group:{GROUP}", 1)) == 2, timeout=10
    ), "人设1的记忆没落库"

    _reset(live.home)
    _inject(live.home, user_id=USER, text="/person 2")
    assert wait_for(lambda: _reply_texts(live.home), timeout=30)

    _reset(live.home)
    _inject(live.home, user_id=USER, text="人设二的事")
    assert wait_for(lambda: _reply_texts(live.home), timeout=30)

    assert wait_for(
        lambda: store.message_count(memory_key(f"group:{GROUP}", 2)) == 2, timeout=10
    ), "人设2的记忆没落库"
    # 人设1 的记忆不该被这次对话影响
    assert store.message_count(memory_key(f"group:{GROUP}", 1)) == 2, "人设之间的记忆串了"


def test_persona_override_changes_model_request(live, fake_openai: FakeOpenAI) -> None:
    """人设的模型/温度覆盖要真的作用到请求上。"""
    uid = _add_persona(live, "省钱人设")
    live.personas.update(
        uid, {"model_override": "cheap-model", "temperature_override": 0.1, "max_tokens_override": 64}
    )
    live.personas.add_whitelist(USER)

    _inject(live.home, user_id=USER, text="/person 2")
    assert wait_for(lambda: _reply_texts(live.home), timeout=30)

    _reset(live.home)
    _inject(live.home, user_id=USER, text="测试覆盖")
    assert wait_for(lambda: _reply_texts(live.home), timeout=30)

    body = fake_openai.requests()[-1]
    assert body["model"] == "cheap-model"
    assert abs(float(body["temperature"]) - 0.1) < 1e-9
    assert body["max_tokens"] == 64


def test_empty_persona_sends_no_system_message(live, fake_openai: FakeOpenAI) -> None:
    """空人设不能发一条空的 system —— 部分 OpenAI 兼容服务会因此报错。

    v2 起【输出规则】**每一轮都在**，所以请求里一定会有 system 块。
    这里断言的是"没有**空的** system"，而不是"完全没有 system"。
    """
    live.settings.update(reply_rubric_enabled=False)
    _inject(live.home, text="空人设测试")
    assert wait_for(lambda: _reply_texts(live.home), timeout=30)

    messages = fake_openai.requests()[-1]["messages"]
    system_blocks = [m for m in messages if m["role"] == "system"]
    assert system_blocks, "输出规则应该常驻"
    assert all(str(m.get("content") or "").strip() for m in system_blocks), "发了一条空的 system"
    assert "【输出规则】" in str(system_blocks[0]["content"])


# --------------------------------------------------------------------------- #
# 删除人设的后果
# --------------------------------------------------------------------------- #
def test_deleting_persona_falls_back_and_wipes_memory(live) -> None:
    uid = _add_persona(live, "临时人设")
    live.personas.add_whitelist(USER)

    _inject(live.home, user_id=USER, text="/person 2")
    assert wait_for(lambda: _reply_texts(live.home), timeout=30)
    _reset(live.home)
    _inject(live.home, user_id=USER, text="临时人设的记忆")
    assert wait_for(lambda: _reply_texts(live.home), timeout=30)
    assert wait_for(
        lambda: live.bot.pipeline.store.message_count(memory_key(f"group:{GROUP}", uid)) == 2,
        timeout=10,
    ), "记忆没落库"

    result = live.personas.delete(uid)
    assert result.deleted_messages == 2
    assert result.affected_sessions >= 1

    # 会话静默回落到人设1
    active = live.bot.pipeline.store.session_persona(
        f"group:{GROUP}", session_type="group", session_ref=GROUP
    )
    assert active == 1
