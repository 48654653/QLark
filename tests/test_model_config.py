"""模型配置与 NapCat 连接配置的测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FakeOpenAI
from qlark import model_config
from qlark.settings import Settings
from qlark.storage.db import Database


@pytest.fixture
def settings(tmp_path: Path):
    db = Database(tmp_path / "q.db")
    try:
        yield Settings(db)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 快照与打码
# --------------------------------------------------------------------------- #
def test_snapshot_masks_secrets(settings: Settings) -> None:
    settings.update(
        base_url="https://api.deepseek.com",
        api_key="sk-1234567890abcdef",
        model="deepseek-chat",
    )
    snap = model_config.snapshot(settings)
    assert snap["api_key_set"] is True
    assert "1234567890abcdef" not in snap["api_key_masked"]
    assert snap["api_key_masked"].startswith("sk-1")
    assert snap["api_key_masked"].endswith("cdef")
    assert snap["token_set"] is True
    assert snap["token_masked"]


def test_mask_short_and_empty() -> None:
    assert model_config.mask("") == ""
    assert model_config.mask("abc") == "***"
    assert model_config.mask("abcdefghij") == "abcd**ghij"


# --------------------------------------------------------------------------- #
# 模型侧校验
# --------------------------------------------------------------------------- #
def test_apply_rejects_bad_base_url(settings: Settings) -> None:
    result = model_config.apply_model(settings, {"base_url": ""})
    assert result["ok"] is False
    assert "base_url" in result["errors"]


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"max_tokens": 0}, "max_tokens"),
        ({"max_tokens": 999999}, "max_tokens"),
        ({"max_tokens": "abc"}, "max_tokens"),
        ({"temperature": 3}, "temperature"),
        ({"temperature": "热"}, "temperature"),
        ({"memory_rounds": 0}, "memory_rounds"),
        ({"max_reply_chars": 10}, "max_reply_chars"),
        ({"model": ""}, "model"),
    ],
)
def test_apply_validation(settings: Settings, payload: dict, field: str) -> None:
    result = model_config.apply_model(settings, payload)
    assert result["ok"] is False
    assert field in result["errors"]


def test_apply_stores_values(settings: Settings) -> None:
    result = model_config.apply_model(
        settings,
        {
            "base_url": "https://api.deepseek.com",
            "api_key": "sk-test",
            "model": "deepseek-chat",
            "vision_model": "deepseek-vl",
            "summary_model": "deepseek-chat",
            "max_tokens": 1024,
            "temperature": 0.3,
            "memory_rounds": 30,
            "reply_quote_group": False,
            "max_reply_chars": 500,
            "notify_error_private": False,
        },
    )
    assert result["ok"] is True
    assert settings.get("base_url") == "https://api.deepseek.com"
    assert settings.get("api_key") == "sk-test"
    assert settings.get("model") == "deepseek-chat"
    assert settings.get("vision_model") == "deepseek-vl"
    assert settings.get("max_tokens") == 1024
    assert abs(float(settings.get("temperature")) - 0.3) < 1e-9
    assert settings.get("memory_rounds") == 30
    assert settings.reply_quote_group is False
    assert settings.max_reply_chars == 500
    assert settings.notify_error_private is False


# --------------------------------------------------------------------------- #
# API Key 的四种语义（这是最容易出错的地方）
# --------------------------------------------------------------------------- #
def test_api_key_absent_keeps_value(settings: Settings) -> None:
    settings.set("api_key", "sk-original")
    model_config.apply_model(settings, {"model": "x"})
    assert settings.get("api_key") == "sk-original"


def test_api_key_empty_string_keeps_value(settings: Settings) -> None:
    """输入框没动过时提交的是空串 —— 绝不能把已存的 Key 清掉。"""
    settings.set("api_key", "sk-original")
    model_config.apply_model(settings, {"api_key": ""})
    assert settings.get("api_key") == "sk-original"


def test_api_key_value_overwrites(settings: Settings) -> None:
    settings.set("api_key", "sk-original")
    model_config.apply_model(settings, {"api_key": "sk-new"})
    assert settings.get("api_key") == "sk-new"


def test_clear_api_key_flag_wins(settings: Settings) -> None:
    settings.set("api_key", "sk-original")
    model_config.apply_model(settings, {"api_key": "", "clear_api_key": True})
    assert settings.get("api_key") == ""


def test_error_notice_private_alias_is_accepted(settings: Settings) -> None:
    """界面用的键名是 ``error_notice_private``，内部存的是 ``notify_error_private``。"""
    model_config.apply_model(settings, {"error_notice_private": False})
    assert settings.notify_error_private is False

    model_config.apply_model(settings, {"notify_error_private": True})
    assert settings.notify_error_private is True


@pytest.mark.parametrize("field", ["max_tokens", "temperature", "memory_rounds", "max_reply_chars"])
def test_blank_number_fields_report_empty_not_invalid(settings: Settings, field: str) -> None:
    """界面把清空的数字框发成 null，提示应当是"不能为空"而不是"必须是数字"。"""
    result = model_config.apply_model(settings, {field: None})
    assert result["ok"] is False
    assert "不能为空" in result["errors"][field]


# --------------------------------------------------------------------------- #
# NapCat 侧
# --------------------------------------------------------------------------- #
def test_napcat_rejects_equal_ports(settings: Settings) -> None:
    result = model_config.apply_napcat(settings, {"onebot_port": 4000, "event_port": 4000})
    assert result["ok"] is False
    assert "event_port" in result["errors"]


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com", ""])
def test_napcat_rejects_non_local_host(settings: Settings, host: str) -> None:
    result = model_config.apply_napcat(settings, {"onebot_host": host})
    assert result["ok"] is False
    assert "onebot_host" in result["errors"]


@pytest.mark.parametrize("port", [0, 70000, "abc"])
def test_napcat_rejects_bad_port(settings: Settings, port) -> None:
    result = model_config.apply_napcat(settings, {"onebot_port": port})
    assert result["ok"] is False
    assert "onebot_port" in result["errors"]


def test_napcat_marks_restart_required_only_when_changed(settings: Settings) -> None:
    assert settings.restart_required is False

    unchanged = model_config.apply_napcat(settings, {"onebot_port": settings.onebot_port})
    assert unchanged == {"ok": True, "restart_required": False, "changed": False}
    assert settings.restart_required is False

    changed = model_config.apply_napcat(settings, {"onebot_port": 4321})
    assert changed["changed"] is True
    assert changed["restart_required"] is True
    assert settings.restart_required is True
    assert settings.onebot_port == 4321


def test_napcat_token_semantics(settings: Settings) -> None:
    from qlark.version import DEFAULT_ONEBOT_TOKEN

    settings.set("onebot_token", "original")
    model_config.apply_napcat(settings, {"event_port": 3333, "token": ""})
    assert settings.onebot_token == "original", "空串表示不修改"

    model_config.apply_napcat(settings, {"event_port": 3334, "token": "my-token"})
    assert settings.onebot_token == "my-token"

    # token 不允许为空，所以"清除"= 恢复出厂默认
    model_config.apply_napcat(settings, {"event_port": 3335, "clear_token": True})
    assert settings.onebot_token == DEFAULT_ONEBOT_TOKEN


# --------------------------------------------------------------------------- #
# 连通性测试（对着假 OpenAI）
# --------------------------------------------------------------------------- #
def test_test_model_success(settings: Settings, fake_openai: FakeOpenAI) -> None:
    result = model_config.test_model(
        settings,
        {"base_url": fake_openai.base_url, "api_key": "k", "model": "fake-model"},
    )
    assert result["ok"] is True
    assert result["model"] == "fake-model"
    assert result["latency_ms"] >= 0
    # 请求侧必须限制成 max_tokens=1（见 test_test_model_does_not_send_extra_tokens）
    assert result["prompt_tokens"] >= 0


def test_test_model_uses_saved_config(settings: Settings, fake_openai: FakeOpenAI) -> None:
    settings.update(base_url=fake_openai.base_url, api_key="k", model="saved-model")
    result = model_config.test_model(settings)
    assert result["ok"] is True
    assert result["model"] == "saved-model"


def test_test_model_reports_auth_failure(settings: Settings, fake_openai: FakeOpenAI) -> None:
    fake_openai.fail_next(times=5, status=401, message="bad key")
    result = model_config.test_model(
        settings,
        {"base_url": fake_openai.base_url, "api_key": "wrong", "model": "m"},
    )
    assert result["ok"] is False
    assert result["kind"] == "auth"
    assert "Key" in result["hint"]


def test_test_model_reports_missing_config(settings: Settings) -> None:
    result = model_config.test_model(settings, {})
    assert result["ok"] is False
    assert result["kind"] == "config"
    assert result["hint"]


def test_test_model_does_not_fall_back_when_base_url_explicitly_blank(
    settings: Settings, fake_openai: FakeOpenAI
) -> None:
    """界面清空 Base URL 后点测试 → 必须报配置错误，而不是偷偷测已保存的地址。"""
    settings.update(base_url=fake_openai.base_url, api_key="k", model="saved-model")
    result = model_config.test_model(settings, {"base_url": "", "model": ""})
    assert result["ok"] is False
    assert result["kind"] == "config"


def test_test_model_omitting_key_uses_saved_value(
    settings: Settings, fake_openai: FakeOpenAI
) -> None:
    settings.update(base_url=fake_openai.base_url, api_key="k", model="saved-model")
    result = model_config.test_model(settings, {})
    assert result["ok"] is True
    assert result["model"] == "saved-model"


def test_test_model_does_not_send_extra_tokens(settings: Settings, fake_openai: FakeOpenAI) -> None:
    """测试按钮绝不能"花多余的 Token"：只发一条极短消息 + max_tokens=1。"""
    fake_openai.reset()
    model_config.test_model(
        settings, {"base_url": fake_openai.base_url, "api_key": "k", "model": "m"}
    )
    requests = fake_openai.requests()
    assert len(requests) == 1
    body = requests[0]
    assert body["max_tokens"] == 1
    assert len(body["messages"]) == 1
    assert body["messages"][0]["content"] == "1"
