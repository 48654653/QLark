"""M2 端到端：假 NapCat 注入消息 → QLark 流水线 → 假 OpenAI → 验证回复。

这是 M2 的验收测试：**群里 @ 机器人能收到 AI 回复；语音消息降级成 ``[语音消息]``**。
整条链路（事件推送 → 过滤/去重/触发判定 → 会话串行 → 上下文组装 → 模型调用 →
发送回复 → 落库与统计）都是真的，只有 NapCat 与模型服务是假的。
"""

from __future__ import annotations

import time

import pytest

from conftest import FakeOpenAI, _get, _post, wait_for
from qlark.chat.store import memory_key
from qlark.core.models import Phase


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
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
def ready(env, fake_openai: FakeOpenAI, monkeypatch):
    """把 QLark 拉到 READY、事件服务已启动的可用状态。"""
    env.settings.update(
        base_url=fake_openai.base_url,
        api_key="test-key",
        model="fake-model",
        max_tokens=256,
        temperature=0.5,
    )
    monkeypatch.setenv("FAKE_NAPCAT_HAVE_CRED", "1")
    env.settings.qq_account = env.home.qq

    env.manager.start()
    assert wait_for(lambda: env.manager.phase is Phase.READY, timeout=120), (
        f"未进入 READY：{env.manager.phase} / {env.manager.last_error}"
    )
    assert wait_for(lambda: env.bot.running, timeout=20), "事件服务没有启动"
    _reset(env.home)
    return env


# --------------------------------------------------------------------------- #
# 事件通道
# --------------------------------------------------------------------------- #
def test_event_server_health(ready) -> None:
    data = _get(f"http://127.0.0.1:{ready.home.event_port}/_qlark/health")
    assert data["ok"] is True
    assert data["running"] is True
    assert data["port"] == ready.home.event_port


def test_napcat_pushes_events_to_qlark(ready) -> None:
    """注入的消息必须真的被推到了 QLark 的事件服务。"""
    result = _inject(ready.home, text="通道测试")
    assert result.get("pushed") is True, "假 NapCat 未能把事件推给 QLark"
    assert wait_for(
        lambda: ready.bot.event_server.snapshot()["received"] >= 1, timeout=10
    )


# --------------------------------------------------------------------------- #
# 触发规则
# --------------------------------------------------------------------------- #
def test_group_message_with_at_gets_reply(ready) -> None:
    _inject(ready.home, message_type="group", group_id="123456", user_id="999", text="你好")

    assert wait_for(lambda: _reply_texts(ready.home), timeout=30), "群里 @ 机器人没有回复"
    texts = _reply_texts(ready.home)
    assert any("你好" in text for text in texts), texts

    sent = _sent(ready.home)[0]
    assert sent["action"] == "send_group_msg"
    assert str(sent["params"]["group_id"]) == "123456"
    # 群聊**默认不引用**原消息：引用会让群里看着很吵，而且 @ 机器人时上下文本来就清楚。
    # （想引用的人可以在「模型配置 → 对话行为」里打开，见 test_group_speaker.py）
    assert not any(seg["type"] == "reply" for seg in sent["params"]["message"])
    # 但要带上发送者名字，否则模型不知道是谁说的
    assert any(
        seg.get("type") == "text" and "测试用户" in str((seg.get("data") or {}).get("text"))
        for seg in sent["params"]["message"]
    ) or texts


def test_group_message_without_at_is_ignored(ready) -> None:
    _inject(ready.home, message_type="group", text="没 @ 你", at_bot=False)
    time.sleep(2.5)
    assert _reply_texts(ready.home) == [], "群聊里没 @ 机器人却回复了"


def test_at_all_does_not_trigger(ready) -> None:
    _inject(
        ready.home,
        segments=[
            {"type": "at", "data": {"qq": "all"}},
            {"type": "text", "data": {"text": " 大家早"}},
        ],
    )
    time.sleep(2.5)
    assert _reply_texts(ready.home) == [], "@全体成员不该触发机器人"


def test_private_message_does_not_need_at(ready) -> None:
    _inject(ready.home, message_type="private", user_id="999", text="在吗")

    assert wait_for(lambda: _reply_texts(ready.home), timeout=30)
    sent = _sent(ready.home)[0]
    assert sent["action"] == "send_private_msg"
    assert str(sent["params"]["user_id"]) == "999"
    # 私聊不需要引用
    assert not any(seg["type"] == "reply" for seg in sent["params"]["message"])


# --------------------------------------------------------------------------- #
# 过滤与去重
# --------------------------------------------------------------------------- #
def test_self_message_is_ignored(ready) -> None:
    _inject(ready.home, user_id=ready.home.qq, text="我自己说的")
    time.sleep(2.5)
    assert _reply_texts(ready.home) == [], "机器人回复了自己的消息"


def test_duplicate_message_is_deduped(ready) -> None:
    _inject(ready.home, text="重复消息", message_id=555001)
    assert wait_for(lambda: _reply_texts(ready.home), timeout=30)

    _inject(ready.home, text="重复消息", message_id=555001)
    time.sleep(2.5)
    assert len(_reply_texts(ready.home)) == 1, "同一条消息被回复了两次"


def test_ignored_group_is_skipped(ready) -> None:
    ready.settings.set("ignored_groups", ["123456"])
    _inject(ready.home, group_id="123456", text="黑名单群")
    time.sleep(2.5)
    assert _reply_texts(ready.home) == []


def test_ignored_user_is_skipped(ready) -> None:
    ready.settings.set("ignored_users", ["999"])
    _inject(ready.home, user_id="999", text="黑名单用户")
    time.sleep(2.5)
    assert _reply_texts(ready.home) == []


# --------------------------------------------------------------------------- #
# 消息段降级（M2 验收标准）
# --------------------------------------------------------------------------- #
def test_voice_segment_becomes_placeholder(ready) -> None:
    """验收标准：语音消息要以 ``[语音消息]`` 的形式送到模型。"""
    _inject(
        ready.home,
        segments=[
            {"type": "at", "data": {"qq": ready.home.qq}},
            {"type": "record", "data": {"file": "voice.silk"}},
            {"type": "text", "data": {"text": " 听听这个"}},
        ],
    )
    assert wait_for(lambda: _reply_texts(ready.home), timeout=30)
    texts = _reply_texts(ready.home)
    assert any("[语音消息]" in text for text in texts), texts


def test_image_segment_becomes_placeholder(ready) -> None:
    _inject(
        ready.home,
        segments=[
            {"type": "at", "data": {"qq": ready.home.qq}},
            {"type": "image", "data": {"file": "a.jpg", "url": "http://x/a.jpg"}},
        ],
    )
    assert wait_for(lambda: _reply_texts(ready.home), timeout=30)
    texts = _reply_texts(ready.home)
    assert any("[图片]" in text for text in texts), texts


def test_mixed_unknown_segment_degrades(ready) -> None:
    _inject(
        ready.home,
        segments=[
            {"type": "at", "data": {"qq": ready.home.qq}},
            {"type": "some_new_type", "data": {}},
            {"type": "text", "data": {"text": " 结尾"}},
        ],
    )
    assert wait_for(lambda: _reply_texts(ready.home), timeout=30)
    texts = _reply_texts(ready.home)
    assert any("[不支持的消息类型]" in text for text in texts), texts


# --------------------------------------------------------------------------- #
# 落库与统计
# --------------------------------------------------------------------------- #
def test_exchange_is_persisted_and_counted(ready) -> None:
    _inject(ready.home, text="记一下这句话")
    assert wait_for(lambda: _reply_texts(ready.home), timeout=30)
    # 给落库留一点时间（发送与落库在同一个任务里，紧挨着）
    assert wait_for(
        lambda: ready.bot.pipeline.store.message_count(memory_key("group:123456", 1)) >= 2,
        timeout=10,
    )

    stats = ready.db.stats_today()
    assert stats.calls >= 1
    assert stats.msg_in >= 1
    assert stats.msg_out >= 1
    assert stats.total_tokens > 0

    rounds = ready.bot.pipeline.store.recent_rounds(memory_key("group:123456", 1), 5)
    assert [role for role, _ in rounds] == ["user", "assistant"]
    # 群聊落库时带上发送者名字：一个群共用一份记忆，不带名字模型分不清谁说的
    assert rounds[0][1] == "测试用户：记一下这句话"


def test_memory_is_isolated_between_groups(ready) -> None:
    """不同群的记忆必须互不干扰（需求规格 §6.1）。"""
    _inject(ready.home, group_id="111111", text="群一的秘密")
    assert wait_for(lambda: _reply_texts(ready.home), timeout=30)
    _reset(ready.home)
    _inject(ready.home, group_id="222222", text="群二的问题")
    assert wait_for(lambda: _reply_texts(ready.home), timeout=30)

    store = ready.bot.pipeline.store
    group1 = store.recent_rounds(memory_key("group:111111", 1), 20)
    group2 = store.recent_rounds(memory_key("group:222222", 1), 20)
    assert all("群二的" not in content for _, content in group1)
    assert all("群一的" not in content for _, content in group2)


def test_context_carries_history_to_the_model(ready, fake_openai: FakeOpenAI) -> None:
    """第二轮请求里必须带上第一轮的问答。"""
    _inject(ready.home, text="第一句")
    assert wait_for(lambda: _reply_texts(ready.home), timeout=30)
    _reset(ready.home)

    _inject(ready.home, text="第二句")
    assert wait_for(lambda: _reply_texts(ready.home), timeout=30)

    requests = fake_openai.requests()
    assert len(requests) >= 1
    messages = requests[-1]["messages"]
    joined = "\n".join(
        str(m.get("content")) for m in messages if isinstance(m.get("content"), str)
    )
    assert "第一句" in joined, "历史没有带上"
    assert "收到：" in joined, "上一轮的回复没有带上"
    assert messages[-1]["content"] == "测试用户：第二句"


# --------------------------------------------------------------------------- #
# 暂停与失败
# --------------------------------------------------------------------------- #
def test_paused_bot_stays_silent(ready) -> None:
    ready.bot.set_paused(True)
    assert ready.bot.paused is True

    _inject(ready.home, text="暂停时说的话")
    time.sleep(2.5)
    assert _reply_texts(ready.home) == [], "暂停后仍然回复了"

    ready.bot.set_paused(False)
    _inject(ready.home, text="恢复后说的话")
    assert wait_for(lambda: _reply_texts(ready.home), timeout=30)


def test_llm_failure_notifies_private_and_skips_memory(ready, fake_openai: FakeOpenAI) -> None:
    fake_openai.fail_next(times=99, status=401, message="bad key")

    _inject(ready.home, message_type="private", user_id="999", text="会失败的一句")

    assert wait_for(lambda: ready.bot.pipeline.stats.failed >= 1, timeout=30), "没有记录失败"
    assert wait_for(lambda: _reply_texts(ready.home), timeout=30), "私聊应当收到错误提示"
    assert any("出问题" in text for text in _reply_texts(ready.home))

    # 失败不写记忆：不能留下"有问无答"的空洞
    assert ready.bot.pipeline.store.message_count(memory_key("private:999", 1)) == 0
    assert ready.db.stats_today().errors >= 1


def test_error_notice_is_not_sent_to_groups(ready, fake_openai: FakeOpenAI) -> None:
    fake_openai.fail_next(times=99, status=401, message="bad key")
    _inject(ready.home, message_type="group", text="群里失败的一句")

    assert wait_for(lambda: ready.bot.pipeline.stats.failed >= 1, timeout=30)
    time.sleep(1.5)
    assert _reply_texts(ready.home) == [], "群聊里不该刷错误提示"


def test_pipeline_snapshot_shape(ready) -> None:
    snapshot = ready.bot.snapshot()
    assert set(snapshot) == {"bot", "event_server"}
    assert {"running", "paused", "queued", "handled", "replied", "dropped"} <= set(snapshot["bot"])
    assert {"running", "host", "port"} <= set(snapshot["event_server"])


# --------------------------------------------------------------------------- #
# 代理：本机调用必须绕开，远端调用才走
# --------------------------------------------------------------------------- #
def test_local_onebot_calls_bypass_system_proxy(ready, monkeypatch) -> None:
    """对本机 NapCat 的调用**绝不能**走系统代理。

    实测（2026-09-25）：本机配了系统代理 127.0.0.1:7890，ProxyOverride 里虽然写了
    ``127.*``，但 **Python 读注册表时不解析这个白名单** —— 于是
    ``http://127.0.0.1:3000`` 的请求也被丢给代理，表现为探针偶发超时（扫码登录后
    认不出账号）以及偶发发不出消息。

    这里故意把代理指到一个死端口：客户端若没用 trust_env=False，这个请求必然失败。
    """
    from qlark.onebot.client import OneBotClient

    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")

    client = OneBotClient(f"http://127.0.0.1:{ready.home.port}", ready.home.token)
    try:
        info = client.get_login_info()
    finally:
        client.close()
    assert str((info.get("data") or {}).get("user_id")) == ready.home.qq


def test_llm_client_keeps_using_system_proxy() -> None:
    """模型请求是**远端**的，必须保留系统代理（很多人靠它访问 API）。"""
    from qlark.llm.client import OpenAIChatClient

    client = OpenAIChatClient("https://api.deepseek.com", "k", "m")
    try:
        assert client._session.trust_env is True
    finally:
        client.close()


# --------------------------------------------------------------------------- #
# 事件服务的反复启停（账号切换 / 重启 NapCat 都会走这条路）
# --------------------------------------------------------------------------- #
def test_event_server_rebinds_after_repeated_restarts(env) -> None:
    """切换账号会"停掉再拉起"，事件服务必须能重新绑回同一个端口。

    真实使用中会留下已建立的连接，端口在重新绑定时可能被 TIME_WAIT 影响 ——
    这件事必须真的试过才知道，不能靠"应该没问题"。
    """
    for attempt in range(3):
        ok = env.bot.start()
        assert ok, f"第 {attempt + 1} 次启动事件服务失败：{env.bot.last_error}"
        assert env.bot.running is True

        # 制造一次真实连接，让端口上留下已用过的套接字
        data = _get(f"http://127.0.0.1:{env.home.event_port}/_qlark/health")
        assert data["ok"] is True

        env.bot.stop(wait=True)
        assert env.bot.running is False
