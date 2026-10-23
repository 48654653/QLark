"""语音合成：把一句话变成 QQ 能发的音频。

用的是 MiMo 的 ``mimo-v2.5-tts-voicedesign``（自定义音色）模型。调用形状和
普通 chat 完全不同，实测确认过下面三条：

1. **音色描述放 ``user``，要读的文本放 ``assistant``。**
   反过来模型会把文本当成"风格描述"，一个字都不念。
2. **不能带 ``audio.voice``。** 那个参数只对预置音色模型有效，
   voicedesign 带上会直接 ``400 Param Incorrect``。
3. **``modalities`` 必须含 ``"audio"``**，否则只返回文本。

返回的音频在 ``choices[0].message.audio.data``（base64）。文档里还有
``expires_at``，说明那份数据有时效 —— 所以拿到的字节要**立刻落盘**。

.. note::
   音色描述写"慢半拍""慵懒"这类词会把声音带向成熟御姐；
   写"小女孩""奶气""轻快"才会往稚嫩走。实测调了五轮才定下现在的默认值。
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger(__name__)

#: 默认模型
DEFAULT_MODEL = "mimo-v2.5-tts-voicedesign"

#: 支持的输出格式。mp3 体积小、QQ 直接能收。
SUPPORTED_FORMATS = ("mp3", "wav", "pcm16")


class TTSError(Exception):
    """语音合成失败。调用方应当**降级发文字**，绝不能把回复弄丢。"""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    def hint(self) -> str:
        if self.status == 401:
            return "TTS 的 API Key 无效"
        if self.status == 402:
            return "TTS 账户余额不足"
        if self.status == 404:
            return "TTS 模型名不对，或者这个地址没有语音合成能力"
        if self.status == 429:
            return "TTS 触发限流，稍后再试"
        if self.status and self.status >= 500:
            return "TTS 服务端出错"
        return "语音合成失败"


@dataclass
class SpeechResult:
    """一次合成的结果。"""

    audio: bytes
    #: 实际格式（mp3 / wav / pcm16）
    fmt: str = "mp3"
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def __len__(self) -> int:
        return len(self.audio)


class VoiceDesignClient:
    """voicedesign 模型的客户端。**共用对话的 base_url / api_key**，只单独配模型名。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = DEFAULT_MODEL,
        *,
        timeout: float = 20.0,
    ) -> None:
        self.base_url = str(base_url or "").strip()
        self.api_key = str(api_key or "").strip()
        self.model = str(model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self.timeout = max(1.0, float(timeout))
        self._session = requests.Session()
        # 本机的系统代理设置会让 localhost / 某些域名走代理，必须绕开
        self._session.trust_env = False

    # ------------------------------------------------------------------ 内部
    def _endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    # ------------------------------------------------------------------ 公开
    def speak(
        self,
        text: str,
        *,
        voice_prompt: str,
        emotion: str = "开心",
        fmt: str = "mp3",
    ) -> SpeechResult:
        """把 ``text`` 合成成音频。

        Args:
            voice_prompt: 音色描述（放 user），例如"十三四岁少女，清透干净…"。
            emotion: 风格标签，会写成 ``(开心)`` 放在文本开头。
                传空串就不加标签。

        Raises:
            TTSError: 参数不对、网络失败、返回为空、或没有音频字段。
        """
        spoken = str(text or "").strip()
        if not spoken:
            raise TTSError("要合成的文本是空的")
        design = str(voice_prompt or "").strip()
        if not design:
            raise TTSError("缺少音色描述（voice_prompt）")
        wanted = str(emotion or "").strip()
        content = f"({wanted}){spoken}" if wanted else spoken
        audio_format = fmt if fmt in SUPPORTED_FORMATS else "mp3"

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                # 音色描述必须在 user —— 这是 voicedesign 的约定
                {"role": "user", "content": design},
                # 要读的文本必须在 assistant，否则一个字都不念
                {"role": "assistant", "content": content},
            ],
            "audio": {"format": audio_format},
            # 不带 audio.voice：voicedesign 带上会 400
            "modalities": ["text", "audio"],
        }

        try:
            response = self._session.post(
                self._endpoint(),
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise TTSError(f"请求失败：{exc}") from exc

        if response.status_code != 200:
            raise TTSError(
                f"HTTP {response.status_code}：{response.text[:200]}",
                status=response.status_code,
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise TTSError(f"返回的不是 JSON：{response.text[:120]}") from exc

        choice = (data.get("choices") or [{}])[0] or {}
        message = choice.get("message") or {}
        audio = message.get("audio") or {}
        encoded = audio.get("data")
        if not encoded:
            raise TTSError(f"返回里没有音频数据：{str(data)[:160]}")

        try:
            raw = base64.b64decode(encoded)
        except (ValueError, TypeError) as exc:
            raise TTSError("音频数据不是合法的 base64") from exc
        if not raw:
            raise TTSError("音频数据是空的")

        usage = data.get("usage") or {}
        return SpeechResult(
            audio=raw,
            fmt=audio_format,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )


__all__ = ["DEFAULT_MODEL", "SUPPORTED_FORMATS", "SpeechResult", "TTSError", "VoiceDesignClient"]
