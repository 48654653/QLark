"""语音回复：模型说要发语音时，真的发出去；出问题就退回文字。

关键契约（都是实测出来的，写错就废）：
  · 音色描述放 ``user``，要读的文本放 ``assistant``
  · **不能带 ``audio.voice``** —— voicedesign 带上会 400
  · ``modalities`` 必须含 ``"audio"``
  · 失败**一律降级发文字**，绝不能把回复弄丢
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import FakeOpenAI, wait_for
from qlark.llm.tts import SpeechResult, TTSError, VoiceDesignClient

from test_m2_integration import _inject, _reset, _sent, ready  # noqa: F401

GROUP = "888888"
VOICE_PROMPT = "一个十三四岁少女的声音，清透干净，稚嫩里带着奶气。"


def _images(env) -> list[dict]:
    return [
        segment
        for item in _sent(env.home)
        for segment in (item.get("params") or {}).get("message") or []
        if segment.get("type") == "image"
    ]


def _records(env) -> list[dict]:
    return [
        segment
        for item in _sent(env.home)
        for segment in (item.get("params") or {}).get("message") or []
        if segment.get("type") == "record"
    ]


def _texts(env) -> list[str]:
    out: list[str] = []
    for item in _sent(env.home):
        for segment in (item.get("params") or {}).get("message") or []:
            if segment.get("type") == "text":
                out.append(str((segment.get("data") or {}).get("text") or ""))
    return out


# --------------------------------------------------------------------------- #
# 客户端：调用形状必须对
# --------------------------------------------------------------------------- #
class _TTSStub:
    """一个只记请求、返回固定音频的假 TTS 服务。"""

    def __init__(self) -> None:
        self.payloads: list[dict] = []
        self.fail_status: int | None = None
        self.empty = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                outer.payloads.append(json.loads(raw or b"{}"))
                if outer.fail_status:
                    body = json.dumps({"error": {"message": "boom"}}).encode()
                    self.send_response(outer.fail_status)
                else:
                    audio = "" if outer.empty else base64.b64encode(b"FAKEAUDIO").decode()
                    body = json.dumps(
                        {"choices": [{"message": {"audio": {"data": audio}}}],
                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
                    ).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()


def test_client_sends_the_right_payload() -> None:
    """音色描述放 user、文本放 assistant、不带 voice、带 modalities。"""
    with _TTSStub() as stub:
        client = VoiceDesignClient(f"http://127.0.0.1:{stub.port}/v1", "sk-x", "m", timeout=10)
        result = client.speak("在呢", voice_prompt=VOICE_PROMPT, emotion="嫌弃")
    assert result.audio == b"FAKEAUDIO"

    payload = stub.payloads[-1]
    messages = payload["messages"]
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == VOICE_PROMPT, "音色描述必须在 user"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == "(嫌弃)在呢", "文本必须在 assistant，且带情绪标签"
    assert "voice" not in (payload.get("audio") or {}), "voicedesign 不能带 audio.voice"
    assert "audio" in payload.get("modalities", [])


def test_client_reports_http_errors() -> None:
    with _TTSStub() as stub:
        stub.fail_status = 402
        client = VoiceDesignClient(f"http://127.0.0.1:{stub.port}/v1", "sk-x", "m", timeout=10)
        with pytest.raises(TTSError) as info:
            client.speak("在呢", voice_prompt=VOICE_PROMPT)
    assert info.value.status == 402
    assert "余额" in info.value.hint()


def test_client_rejects_empty_audio() -> None:
    with _TTSStub() as stub:
        stub.empty = True
        client = VoiceDesignClient(f"http://127.0.0.1:{stub.port}/v1", "sk-x", "m", timeout=10)
        with pytest.raises(TTSError):
            client.speak("在呢", voice_prompt=VOICE_PROMPT)


# --------------------------------------------------------------------------- #
# 流水线：发出去的东西对不对
# --------------------------------------------------------------------------- #
class _FakeTTS:
    """直接替换掉 pipeline 里的 TTS 客户端，不用起服务。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str, str]] = []

    def speak(self, text: str, *, voice_prompt: str, emotion: str = "开心", **_) -> SpeechResult:
        self.calls.append((text, voice_prompt, emotion))
        if self.fail:
            raise TTSError("boom", status=500)
        return SpeechResult(audio=b"ID3-fake-audio", fmt="mp3")


def _enable_voice(env, tts: _FakeTTS, **persona) -> None:
    env.settings.update(voice_enabled=True, voice_max_chars=20)
    fields = {"voice_enabled": True, "voice_prompt": VOICE_PROMPT, "meme_enabled": False}
    fields.update(persona)
    env.bot.pipeline.personas.update(1, fields)
    env.bot.pipeline._tts_client = tts


def _say(env, reply: str | None = None, message_id: int = 1) -> None:
    """注入一条 @ 机器人的消息，等它回完。

    模型回复由 ``fake_openai.replies(...)`` 预先排队，这里不用传 ``reply``。
    """
    _reset(env.home)
    _inject(
        env.home,
        message_type="group",
        group_id=GROUP,
        user_id="333333",
        message_id=message_id,
        text="在吗",
        at_bot=True,
        nickname="毛糙蛋",
    )
    assert wait_for(lambda: _texts(env) or _records(env), timeout=30)


def test_voice_reply_is_sent_as_record(ready, fake_openai: FakeOpenAI) -> None:
    """模型填了 voice=true → 发出去的是语音，文字不再发。"""
    tts = _FakeTTS()
    _enable_voice(ready, tts)
    fake_openai.replies('{"messages": ["嗯"], "voice": true, "emotion": "嫌弃"}', repeat_last=True)

    _say(ready)

    records = _records(ready)
    assert len(records) == 1, "应该发一条语音"
    assert str(records[0]["data"]["file"]).startswith("base64://")
    assert _texts(ready) == [], "语音已经替代了文字，不该再发一遍"
    assert tts.calls == [("嗯", VOICE_PROMPT, "嫌弃")]


def test_long_text_falls_back_to_text(ready, fake_openai: FakeOpenAI) -> None:
    """超过 20 字就不发语音 —— 实测 46 字要念 21 秒。"""
    tts = _FakeTTS()
    _enable_voice(ready, tts)
    long_line = "这条真的特别长" * 4
    fake_openai.replies(
        json.dumps({"messages": [long_line], "voice": True}, ensure_ascii=False),
        repeat_last=True,
    )

    _say(ready)

    assert _records(ready) == [], "超长不该发语音"
    assert _texts(ready), "应该退回发文字"
    assert tts.calls == [], "护栏在解析阶段就拦下了，根本不该调 TTS"


def test_tts_failure_falls_back_to_text(ready, fake_openai: FakeOpenAI) -> None:
    """TTS 挂了也必须把话发出去 —— 语音是可选的，回复不是。"""
    tts = _FakeTTS(fail=True)
    _enable_voice(ready, tts)
    fake_openai.replies('{"messages": ["嗯"], "voice": true}', repeat_last=True)

    _say(ready)

    assert _records(ready) == [], "合成失败不该发语音"
    assert _texts(ready) == ["嗯"], "必须退回发文字"
    assert ready.bot.pipeline.stats.voice_fallbacks == 1


def test_voice_off_never_calls_tts(ready, fake_openai: FakeOpenAI) -> None:
    """角色没开语音 → 一次都不该调 TTS，即使模型填了 voice。"""
    tts = _FakeTTS()
    _enable_voice(ready, tts, voice_enabled=False)
    fake_openai.replies('{"messages": ["嗯"], "voice": true}', repeat_last=True)

    _say(ready)

    assert tts.calls == []
    assert _texts(ready) == ["嗯"]


def test_global_switch_off_never_calls_tts(ready, fake_openai: FakeOpenAI) -> None:
    tts = _FakeTTS()
    _enable_voice(ready, tts)
    ready.settings.update(voice_enabled=False)
    fake_openai.replies('{"messages": ["嗯"], "voice": true}', repeat_last=True)

    _say(ready)

    assert tts.calls == []


def test_voice_counter_increments(ready, fake_openai: FakeOpenAI) -> None:
    tts = _FakeTTS()
    _enable_voice(ready, tts)
    fake_openai.replies('{"messages": ["嗯"], "voice": true}', repeat_last=True)

    _say(ready)

    assert ready.bot.pipeline.stats.voices == 1


def test_temp_audio_file_is_removed(ready, fake_openai: FakeOpenAI) -> None:
    """中转的音频文件发完要删掉，不然临时目录会越堆越多。"""
    tts = _FakeTTS()
    _enable_voice(ready, tts)
    fake_openai.replies('{"messages": ["嗯"], "voice": true}', repeat_last=True)

    _say(ready)

    import glob
    import os
    import tempfile

    leftovers = glob.glob(os.path.join(tempfile.gettempdir(), "qlark-voice-*.mp3"))
    assert leftovers == [], f"临时语音文件没清理：{leftovers}"
