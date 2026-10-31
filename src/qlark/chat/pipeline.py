"""消息处理流水线：过滤 → 去重 → 触发判定 → 会话串行 → 调模型 → 回复 → 落库。

设计要点（每条都对应需求规格里的硬规则）：

1. **群聊必须 @ 机器人**才触发；私聊无限制（§4.1）。
2. **只认 @**：回复机器人的消息但没 @ 不算触发，纯文本 "@昵称" 也不算
   —— 判定基于消息段里的 ``at`` 段，不是字符串匹配。
3. **每会话串行**：同一群/同一人的消息排队处理，避免记忆错乱（见 ``queue.py``）。
4. **调用成功后才写入记忆**：失败不写，否则记忆里会留下"有问无答"的空洞。
5. **绝不把自己的消息当输入**：否则会和自己聊起来。
"""

from __future__ import annotations

import logging
import base64
import json
import random
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .. import paths
from ..core.models import LogSource
from ..fun import config as fun_config
from ..fun import weather as fun_weather
from ..llm.client import ChatResult, LLMError, OpenAIChatClient
from ..llm.tts import DEFAULT_MODEL as DEFAULT_TTS_MODEL
from ..llm.tts import TTSError, VoiceDesignClient
from ..napcat.logbus import LogBus
from ..onebot.client import OneBotClient, OneBotError
from ..onebot.events import GROUP, MessageEvent, describe_event, parse_message_event
from ..onebot.sender import ReplySender
from ..onebot.segments import ParsedMessage, VideoRef, parse_segments
from ..settings import Settings
from ..storage.db import Database
from .commands import CommandRouter
from .context import build_chat_messages
from .dedup import MessageDedup
from .images import (
    DEFAULT_MAX_IMAGES,
    ImagePreparer,
    PreparedImage,
    build_user_content,
    looks_like_vision_unsupported,
)
from .memes import MemeLibrary, resolve_dir as resolve_meme_dir
from .moderation import RateLimiter, find_blocked_word
from .personas import PersonaStore
from .queue import SessionQueue
from .coalesce import Coalescer
from .keepalive import KeepAlive
from .quoted import MAX_RENDER_CHARS as MAX_QUOTED_CHARS
from .quoted import QuotedContent, QuotedFetcher
from .reply_format import (
    FORMAT_REMINDER,
    REPLY_RUBRIC,
    append_reminder,
    build_output_rules,
    compose_memory_text,
    parse_reply,
    salvage,
)
from .store import MessageStore, memory_key
from .summarizer import (
    SUMMARY_MAX_TOKENS,
    SUMMARY_MIN_TOKENS,
    SUMMARY_TEMPERATURE,
    Summarizer,
)

log = logging.getLogger(__name__)

#: 语音朗读允许的语气标签。**只有两个** —— 标签越少模型挑得越准，
#: 而且 QQ 聊天本来就不需要把每种情绪都演一遍。
VOICE_EMOTIONS = ("开心", "嫌弃")

#: 一张图在上下文预算里折合多少"字符"。
#:
#: 实测 token ≈ 像素数 ÷ 1024：512px 约 258 token。按 4 字符 ≈ 1 token 折算，
#: 约 1024 字符。**绝不能按 base64 长度算** —— 那有五万多字符，
#: 一张图就能把整段历史挤掉。
IMAGE_CHAR_COST = 1024

#: 存进记忆的图片占位文本
IMAGE_PLACEHOLDER = "[图片]"

#: 存进记忆的视频占位文本
VIDEO_PLACEHOLDER = "[视频]"

#: 一段视频在上下文预算里折合多少"字符"。
#:
#: 实测（mimo，1MB 短视频）：``prompt_tokens_details.video_tokens`` = 1104。
#: 按 4 字符 ≈ 1 token 折算约 4400 字符 —— 比一张图贵 4 倍多，
#: 所以历史里**只回放最近那一段**，不能像图片那样按轮数回溯。
VIDEO_CHAR_COST = 4400

#: 带视频的请求用多长的超时（秒）。
#:
#: 服务端得先把整段视频拉下来解码：实测 1MB 的短视频从发出到首字节就要 60s 上下，
#: 用客户端默认的 60s 会**必然踩线**。所以带视频时显式放宽。
VIDEO_TIMEOUT_S = 180.0

#: 视频直链的有效期（秒）。NapCat 给的 URL 带 ``rkey``，约 2 小时过期。
#:
#: 历史里回放超过这个时长的直链只会让服务端报错，不如直接按占位文本处理。
VIDEO_URL_TTL_S = 90 * 60

#: 长期记忆块的标签（需求规格 §6.3 组装请求时的上下文）
SUMMARY_BLOCK_LABEL = "[长期记忆]"

#: 默认上下文预算（字符数，粗估）
DEFAULT_CONTEXT_BUDGET = 24000

#: 输出格式不对时，第二次调用附在后面的纠正指令
RETRY_INSTRUCTION = (
    "你上一次的输出不是合法的 JSON（错误：{error}）。"
    "请**只**输出一个 JSON 对象，不要有任何解释文字，也不要用 ``` 代码块包裹。"
    '格式：{{"messages": ["你要说的话"], "meme": "表情名"}}'
)


def _preview(text: str | None, limit: int = 240) -> str:
    """把模型输出压成一行、截断，用于日志。

    **解析失败时必须把原文记下来** —— 只记「不是合法 JSON」等于什么都没说，
    事后完全没法判断是模型没听指令、返回了空、还是格式差一点点。
    """
    flat = " ".join(str(text or "").split())
    if not flat:
        return "（空内容）"
    return flat if len(flat) <= limit else flat[:limit] + "…"


@dataclass
class ReplyPlan:
    """这一轮到底要发什么。"""

    texts: list[str]
    meme: str | None = None
    image_path: Path | None = None
    #: 这一轮走**语音**（已经合成好音频时 ``audio_path`` 才有值）
    voice: bool = False
    #: 朗读用的语气标签
    emotion: str | None = None
    #: 合成出来的语音文件；发出去之后会被删掉
    audio_path: Path | None = None
    #: 实际调用模型的次数（含重新生成）
    calls: int = 1
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: 走到"抢救"或"兜底"时的原因，仅用于日志
    degraded: str | None = None
    #: 多条之间的间隔（毫秒）；人设级设置优先，留空则用全局
    delay_ms: int = 0
    #: 模型这次**原样返回的文本**（v2 起就是那个 JSON 字符串）
    #:
    #: 记忆里存它、而不是简化后的自然语言，原因有两个：
    #:   ① 后台调试时能看到真实输出，不用猜"它当时到底怎么回的"
    #:   ② 模型回看历史时看到的示范也是 JSON，**格式自洽** ——
    #:      如果历史里全是散文，它会慢慢被带跑，越来越不肯输出 JSON
    raw_text: str = ""

    @property
    def memory_text(self) -> str:
        """落库用的文本。

        v2 起优先存**模型原始输出**（JSON）；原始输出不可用（为空）时才退回
        拼起来的自然文本。
        """
        raw = str(self.raw_text or "").strip()
        if raw:
            return raw
        return compose_memory_text(self.texts)


def _merge_batch(
    items: list[tuple[MessageEvent, ParsedMessage]],
) -> tuple[MessageEvent, ParsedMessage]:
    """把一批碎片合并成一条消息。

    · 文本：每个碎片**一行**（保留"他是一句一句发的"这个信息）
    · 图片：取并集
    · 引用：取**最后一条**非空的（他引用的是最关心的那条）
    · @ / 事件本身：用最后一条的（时间、消息 ID 都以最新为准）
    """
    if len(items) == 1:
        return items[0]

    event, last = items[-1]
    lines: list[str] = []
    images: list[Any] = []
    ats: list[str] = []
    forwards: list[dict[str, Any]] = []
    reply_id: str | None = None
    mentioned = False
    for _, parsed in items:
        text = parsed.text_for_memory().strip()
        if text:
            lines.append(text)
        images.extend(parsed.images)
        ats.extend(parsed.ats)
        forwards.extend(getattr(parsed, "forwards", ()) or ())
        if parsed.reply_id:
            reply_id = parsed.reply_id
        mentioned = mentioned or parsed.mentions_bot

    merged = replace(
        last,
        text="\n".join(lines),
        images=images,
        ats=ats,
        forwards=forwards,
        reply_id=reply_id,
        mentions_bot=mentioned,
    )
    return event, merged


def _content_cost(content: Any) -> int:
    """估算一条 message 占多少"字符预算"。

    **图片不能按 base64 长度算。** 一张 512px 的 JPEG base64 有五万多字符，
    但实测只吃 258 token —— 按 base64 算的话，一张图就能把整段历史挤没，
    表现是"明明带着图，请求里却一张都没有"（真踩过）。

    这里给每张图固定折 1024 字符（约 256 token，和实测的 258 对齐）。
    """
    if isinstance(content, str):
        return len(content)
    if not isinstance(content, list):
        return len(str(content or ""))
    total = 0
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "image_url":
            total += IMAGE_CHAR_COST
        elif part.get("type") == "video_url":
            total += VIDEO_CHAR_COST
        else:
            total += len(str(part.get("text") or ""))
    return total


def _strip_image_placeholders(text: str) -> str:
    """去掉文本里的 ``[图片]`` 占位。

    多模态请求里图片会以真身出现，文本里再留一条 ``[图片]`` 会让模型以为
    "这里有两张图"，甚至去描述那个并不存在的占位符。
    """
    cleaned = str(text or "").replace(IMAGE_PLACEHOLDER, " ")
    return " ".join(cleaned.split())


def _strip_video_placeholders(text: str) -> str:
    """去掉文本里的 ``[视频]`` 占位。

    和图片同理：视频真身已经以 ``video_url`` 段附上了，正文里再留一条
    ``[视频]`` 会让模型以为自己"只收到一个占位符"，然后如实回答"看不到"。
    """
    cleaned = str(text or "").replace(VIDEO_PLACEHOLDER, " ")
    return " ".join(cleaned.split())


def _json_object(text: str | None) -> dict[str, Any] | None:
    """从模型输出里抠出第一个 JSON 对象（容忍 ```json 代码块和前后废话）。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw[:4].lower() == "json":
            raw = raw[4:]
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(raw[start : end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def format_stamp(timestamp: float | int | None) -> str:
    """按 ``2026-9-25 17:00`` 格式化时间。

    月、日**不补零**（用户要的就是 ``9-25`` 不是 ``09-25``），时、分补零。
    Python 的 ``strftime`` 在 Windows 上不支持 ``%-m``，所以这里手动拼。
    """
    try:
        moment = datetime.fromtimestamp(float(timestamp)) if timestamp else datetime.now()
    except (TypeError, ValueError, OSError):
        moment = datetime.now()
    return f"{moment.year}-{moment.month}-{moment.day} {moment.hour:02d}:{moment.minute:02d}"


def clean_sender_name(raw: str) -> str:
    """把昵称清理成可以安全放进 ``昵称：`` 那一行的形式。

    换行/制表会破坏三行结构；顺手限长，免得有人用超长昵称把每条消息都撑大。
    """
    return " ".join(str(raw or "").split())[:32].strip("：: ").strip()


def build_labeled_message(name: str, timestamp: float | int | None, text: str) -> str:
    """把一条消息包成带昵称与时间的块。

    .. code-block:: text

        昵称：毛糙蛋
        时间：2026-9-25 17:00
        消息：早上好啊

    为什么要这么做：**群聊**里所有人共用一份记忆（规格 §6.1），不带昵称的话
    模型根本分不清哪句是谁说的；**时间**则让它能理解"早上好"是几点的早上好、
    是不是隔了很久才回。私聊虽然只有一个人，但时间同样有意义。
    """
    return f"昵称：{name}\n时间：{format_stamp(timestamp)}\n消息：{text}"


def _strip_media_parts(
    messages: list[dict[str, Any]], text: str
) -> list[dict[str, Any]]:
    """把多模态内容全部换回纯文本（视觉降级用）。

    只换最后一条 user 是不够的 —— v2 起**历史里也会带真实的图和视频**，
    模型不支持多模态时那些段同样会让请求失败。所以这里逐条处理：
    文本段照旧、图片/视频段退化成占位符。

    最后一条 user 消息直接用调用方给的 ``text``（就是落库的那份带占位符的记忆文本），
    保证降级后的上下文和原来的措辞一致。
    """
    result = [dict(message) for message in messages]
    for message in reversed(result):
        if message.get("role") != "user":
            continue
        message["content"] = text or IMAGE_PLACEHOLDER
        break
    cleaned: list[dict[str, Any]] = []
    for message in result:
        content = message.get("content")
        if isinstance(content, list):
            pieces: list[str] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                kind = str(part.get("type") or "")
                if kind == "text":
                    pieces.append(str(part.get("text") or ""))
                elif kind == "image_url":
                    pieces.append(IMAGE_PLACEHOLDER)
                elif kind == "video_url":
                    pieces.append(VIDEO_PLACEHOLDER)
            joined = " ".join(piece for piece in pieces if piece).strip()
            if not joined:
                # 整条只剩媒体段、又把占位符也剥掉了 → 这条干脆不发
                continue
            message["content"] = joined
        cleaned.append(message)
    return cleaned


@dataclass
class PipelineStats:
    handled: int = 0
    replied: int = 0
    dropped: int = 0
    failed: int = 0
    #: 走指令（未调用模型）的消息数
    commands: int = 0
    #: 成功执行的记忆压缩次数（M4）
    summaries: int = 0
    #: 图片成功交给视觉模型的次数（M5）
    images: int = 0
    #: v2.3：视频成功交给模型的次数（原生 video_url）
    videos: int = 0
    #: v2.3.1：真正把实时天气注入上下文的次数
    weathers: int = 0
    #: 因为模型不支持视觉而降级成纯文本的次数（M5）
    vision_fallbacks: int = 0
    #: 命中敏感词被拦下的条数（M6 风控）
    blocked: int = 0
    #: 因为超出速率上限被拦下的条数（M6 风控）
    rate_limited: int = 0
    #: v2：走语音发出去的回复数
    voices: int = 0
    #: v2：配了表情的回复数
    memes: int = 0
    #: v2：发成多条（分段）的回复数
    splits: int = 0
    #: v2：模型要求语音但合成失败、降级成文字的次数
    voice_fallbacks: int = 0
    #: v2.1：成功取回被引用内容的次数
    quotes: int = 0
    last_error: str | None = None
    last_reply_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "handled": self.handled,
            "replied": self.replied,
            "dropped": self.dropped,
            "failed": self.failed,
            "commands": self.commands,
            "summaries": self.summaries,
            "images": self.images,
            "videos": self.videos,
            "weathers": self.weathers,
            "vision_fallbacks": self.vision_fallbacks,
            "blocked": self.blocked,
            "rate_limited": self.rate_limited,
            # v2：模型自主决定的三种形态用了几次。**没有骰子之后，
            # 频率是否过密只能靠这三个数看** —— 太高就调提示词。
            "voices": self.voices,
            "memes": self.memes,
            "splits": self.splits,
            "voice_fallbacks": self.voice_fallbacks,
            "quotes": self.quotes,
            "last_error": self.last_error,
            "last_reply_at": self.last_reply_at,
        }


class MessagePipeline:
    """把收到的消息变成回复。"""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        logbus: LogBus,
        *,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.logbus = logbus
        self._on_change = on_change or (lambda: None)

        self.store = MessageStore(db)
        self.store.ensure_default_persona()
        self.personas = PersonaStore(db)
        self.commands = CommandRouter(settings, self.store, self.personas, logbus)

        self.dedup = MessageDedup(window_seconds=settings.dedup_window)
        self.queue = SessionQueue(
            settings.max_concurrent,
            on_drop=self._on_queue_full,
        )
        self.stats = PipelineStats()
        #: 趣味功能·天气：按城市缓存渲染结果（10 分钟内不重复打和风）
        self._weather_cache = fun_weather.WeatherCache()
        #: 每个会话「上一次聊天气」的时间戳：用来接住"洛宁呢""菏泽"这种省略问句。
        #: 没有它就只能靠关键词，而省略问句里一个天气词都没有（实测踩过：
        #: 用户只发"菏泽"，模型会回"我查一下，稍等"，然后什么都不发）。
        self._weather_seen: dict[str, float] = {}
        #: 每个会话上一次查的城市（给"那明天呢"这种追问兜底指代）
        self._weather_last_city: dict[str, str] = {}
        self._weather_lock = threading.RLock()

        self.summarizer = Summarizer(
            settings,
            self.store,
            complete=self._summarize_call,
            logbus=logbus,
        )
        #: 图片预处理：长边缩到 `image_max_side`（默认 512）。
        #:
        #: 实测 token ≈ 像素数 ÷ 1024：
        #:   512px → 258 token    1024px → 1026 token    2048px → 4098 token
        #: QQ 聊天里 512px 足够看清，没必要为清晰度多付四倍。
        self._images = ImagePreparer(max_side=self._setting_int("image_max_side", 512))
        self._rate_limiter: RateLimiter | None = None

        self._paused = settings.bot_paused
        self._running = False
        self._lock = threading.RLock()

        self._llm_cache: tuple[tuple[str, str, str], OpenAIChatClient] | None = None
        self._vision_cache: tuple[tuple[Any, ...], OpenAIChatClient] | None = None
        self._summary_cache: tuple[tuple[str, str, str], OpenAIChatClient] | None = None
        self._ob_cache: tuple[tuple[str, str], OneBotClient] | None = None
        #: 语音合成客户端（TTS 出错后不缓存，下次重新构造）
        self._tts_client: VoiceDesignClient | None = None
        #: @ 激活保活：被 @ 之后的一段时间内，那个人不必再 @ 也能接着聊。
        #: **按 (会话, 用户) 独立** —— 别人说话续不了你的窗口。
        self._keepalive = KeepAlive(
            seconds=self._setting_int("keepalive_seconds", 60)
        )
        #: 引用消息取回器（懒构造，取不到内容时不影响后续）
        self._quoted_fetcher: QuotedFetcher | None = None
        #: 免@ 消息的合并缓冲：有人一句一句发碎片时，攒起来一起回。
        #: **按 (会话, 用户) 独立**，回调在定时器线程里跑（这里已经自己加锁）。
        self._coalescer = Coalescer(
            quiet_seconds=self._setting_float("coalesce_quiet_seconds", 3.0),
            max_seconds=self._setting_float("coalesce_max_seconds", 15.0),
            on_flush=self._flush_batch,
        )

    # ================================================================== 生命周期
    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self.dedup.clear()
            self.queue.start()
        self._log("消息流水线已启动")

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
        # 缓冲里的碎片直接丢掉，**不能把半批发出去** —— 停止过程中发消息
        # 会出现"说到一半"的回复，比不发更糟。
        dropped = self._coalescer.discard_all()
        if dropped:
            self._log(f"停止时丢弃了 {dropped} 条尚未合并的消息", level="debug")
        self.queue.shutdown(wait=True)
        self._log("消息流水线已停止", level="debug")

    @property
    def running(self) -> bool:
        return self._running

    def invalidate_clients(self) -> None:
        """配置变更后让缓存的模型 / OneBot 客户端失效，下一次调用重新构造。"""
        with self._lock:
            self._llm_cache = None
            self._vision_cache = None
            self._summary_cache = None
            self._ob_cache = None
            self._tts_client = None

    # ================================================================== 暂停
    @property
    def paused(self) -> bool:
        return self._paused

    def set_paused(self, paused: bool) -> bool:
        with self._lock:
            self._paused = bool(paused)
        self.settings.bot_paused = self._paused
        if self._paused:
            # 暂停时把攒着的碎片丢掉，避免一恢复就把几分钟前的旧话回出去
            dropped = self._coalescer.discard_all()
            if dropped:
                self._log(f"暂停时丢弃了 {dropped} 条尚未合并的消息", level="debug")
        self._log("已暂停回复" if self._paused else "已恢复回复", level="warn")
        self._on_change()
        return self._paused

    # ================================================================== 状态
    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "paused": self._paused,
            "queued": self.queue.pending_count,
            **self.stats.to_dict(),
        }

    # ================================================================== 入口
    def submit(self, payload: dict[str, Any]) -> None:
        """由事件服务调用。**必须立刻返回**，所有耗时工作交给工作线程。"""
        event = parse_message_event(payload, self_id=self.settings.qq_account)
        if event is None:
            # notice / meta_event / request 等，只记一条 debug 日志
            self._log(f"忽略非消息事件：{describe_event(payload)}", level="debug")
            return

        if not self._passes_filters(event):
            return

        parsed = parse_segments(event.segments, self_id=event.self_id or self.settings.qq_account)
        if parsed.is_empty:
            self.stats.dropped += 1
            self._log(f"消息没有可读内容，已忽略：{event.describe()}", level="debug")
            self._on_change()
            return

        reason = self._trigger_reason(event, parsed)
        if reason is None:
            self.stats.dropped += 1
            self._log(f"未 @ 机器人，不触发：{event.describe()}", level="debug")
            self._on_change()
            return

        if self._paused:
            self.stats.dropped += 1
            self._log(f"已暂停回复，忽略：{event.describe()}", level="debug")
            self._on_change()
            return

        session_id = event.session_id
        speaker = str(event.user_id or "")
        batch_key = f"{session_id}#{speaker}"
        self.stats.handled += 1

        # ---- v2.2 合并：免@ 的消息先攒一攒，不要一条一条回 ----
        #
        # 有人习惯把一句话拆成好几条发（"在" "吗" "问个事"）。每条都回的话
        # 会变成「咋了 / 嗯？ / 说」三连，一眼就是机器人。
        if reason == "window" and self._coalesce_on():
            count = self._coalescer.add(batch_key, (event, parsed))
            self._log(
                f"收到：{event.describe()} → {parsed.text[:60]!r}"
                f"（免@ 第 {count} 条，静默 {self._coalescer.quiet_seconds:g} 秒后一起回）"
            )
            self._on_change()
            return

        self._log(f"收到：{event.describe()} → {parsed.text[:60]!r}")
        self._on_change()

        # @ 了机器人，或者没开合并 → 立刻回。
        # 但要把这个人已经攒下的碎片**一起吸收**进来，否则会先回一个"嗯？"
        # 再回正式答案，反而更糟。
        items = [(event, parsed)]
        if self._coalesce_on():
            pending = self._coalescer.take(batch_key)
            if pending:
                self._log(
                    f"把 {speaker} 之前攒的 {len(pending)} 条碎片并进这一轮",
                    level="debug",
                )
                items = pending + items
        self._dispatch(session_id, items)

    def _dispatch(self, session_id: str, items: list[tuple[MessageEvent, ParsedMessage]]) -> None:
        """把一批（可能只有一条）合并后交给工作线程。"""
        event, parsed = _merge_batch(items)
        accepted = self.queue.submit(
            session_id, lambda e=event, p=parsed: self._handle(e, p)
        )
        if not accepted:
            self._log(f"{session_id} 待处理消息过多，已丢弃 {len(items)} 条", level="warn")

    def _flush_batch(self, batch_key: str, items: list[Any]) -> None:
        """合并计时到点：把整批发出去。**在定时器线程里跑。**"""
        if not items:
            return
        session_id = str(batch_key).split("#", 1)[0]
        self._log(
            f"{session_id} 这 {len(items)} 条免@ 消息已合并，开始回复",
            level="debug",
        )
        try:
            self._dispatch(session_id, list(items))
        except Exception as exc:  # noqa: BLE001 定时器线程里不能把异常抛出去
            log.exception("合并批次派发失败")
            self._log(f"合并批次派发失败：{exc}", level="error")

    def _coalesce_on(self) -> bool:
        if not bool(self.settings.get("coalesce_enabled", True)):
            return False
        quiet = self._setting_float("coalesce_quiet_seconds", 3.0)
        cap = self._setting_float("coalesce_max_seconds", 15.0)
        if (quiet, cap) != (self._coalescer.quiet_seconds, self._coalescer.max_seconds):
            # 配置改了就地生效，不用重启
            self._coalescer.configure(quiet_seconds=quiet, max_seconds=cap)
        return True

    # ================================================================== 过滤
    def _passes_filters(self, event: MessageEvent) -> bool:
        if event.is_from_self:
            self._log(f"忽略自己发出的消息：{event.describe()}", level="debug")
            self.stats.dropped += 1
            return False

        if event.user_id in self.settings.id_set("ignored_users"):
            self.stats.dropped += 1
            self._log(f"发送者在黑名单里，已忽略：{event.user_id}", level="debug")
            return False

        if event.is_group and (event.group_id or "") in self.settings.id_set("ignored_groups"):
            self.stats.dropped += 1
            self._log(f"群在黑名单里，已忽略：{event.group_id}", level="debug")
            return False

        # 去重必须在触发判定之前：重连时同一条消息可能被推两次
        dedup_key = f"{event.session_id}#{event.message_id}"
        if self.dedup.seen(dedup_key):
            self.stats.dropped += 1
            self._log(f"重复消息，已忽略：{event.describe()}", level="debug")
            return False
        return True

    def _trigger_reason(self, event: MessageEvent, parsed: ParsedMessage) -> str | None:
        """判断这一条要不要回，以及**为什么**要回。

        Returns:
            ``"private"`` 私聊一律回；``"at"`` 明确 @ 了机器人；
            ``"window"`` 在免@ 保活窗口里（会续期）；``None`` 不理。
        """
        if not event.is_group:
            return "private"

        self_id = event.self_id or self.settings.qq_account or ""
        mentioned = parsed.mentions_bot or (bool(self_id) and self_id in parsed.ats)
        session = event.session_id
        speaker = str(event.user_id or "")

        if mentioned:
            if self._keepalive_on():
                self._keepalive.arm(session, speaker)
            return "at"

        if not self._keepalive_on() or not self._keepalive.active(session, speaker):
            return None

        # 窗口内：续期，并照常回复
        self._keepalive.touch(session, speaker)
        remaining = self._keepalive.remaining(session, speaker)
        self._log(
            f"{speaker} 的 @ 保活还有 {remaining:.0f} 秒，本条按继续对话处理",
            level="debug",
        )
        return "window"

    def _is_triggered(self, event: MessageEvent, parsed: ParsedMessage) -> bool:
        """兼容旧调用：只要判断"要不要回"。"""
        return self._trigger_reason(event, parsed) is not None

    def _keepalive_on(self) -> bool:
        if not bool(self.settings.get("keepalive_enabled", True)):
            return False
        seconds = self._setting_int("keepalive_seconds", 60)
        if seconds != int(self._keepalive.seconds):
            # 配置改了就地生效，不用重启
            self._keepalive.set_seconds(seconds)
        # 过期条目只在被查询时会顺手清掉那些"再也没说话"的用户，
        # 会一直留在字典里 —— 攒够一批就整体清一次，防止缓慢泄漏。
        if len(self._keepalive) > 200:
            self._keepalive.purge()
        return True

    # ================================================================== 处理
    def _handle(self, event: MessageEvent, parsed: ParsedMessage) -> None:
        """在工作线程里跑，同一会话不会并发。"""
        try:
            self._handle_inner(event, parsed)
        except LLMError as exc:
            self._on_llm_error(event, exc)
        except OneBotError as exc:
            self.stats.failed += 1
            self.stats.last_error = f"发送失败：{exc}"
            self._log(f"回复发送失败：{exc}", level="error")
        except Exception as exc:  # 兜底，绝不能让工作线程静默死掉
            self.stats.failed += 1
            self.stats.last_error = f"处理异常：{exc}"
            log.exception("处理消息时发生未预期异常")
            self._log(f"处理消息异常：{exc}", level="error")
        finally:
            self._on_change()

    def _handle_inner(self, event: MessageEvent, parsed: ParsedMessage) -> None:
        session_type = GROUP if event.is_group else "private"
        session_ref = event.group_id if event.is_group else event.user_id
        persona_uid = self.store.session_persona(
            event.session_id, session_type=session_type, session_ref=str(session_ref)
        )
        key = memory_key(event.session_id, persona_uid)

        # 指令先处理：不走模型，也**不写入记忆**（否则上下文会被 /person 污染）
        outcome = self.commands.handle(event, parsed, persona_uid)
        if outcome.handled:
            self.stats.commands += 1
            sent = 0
            if outcome.reply:
                sent = self._sender().send_reply(event, outcome.reply)
            self.db.bump_stats(msg_in=1, msg_out=sent)
            self.db.touch_session(event.session_id)
            self.stats.last_reply_at = time.time()
            self._log(f"指令已处理：{parsed.text.strip()[:40]!r}（{event.session_id}）")
            return

        persona = self.personas.get(persona_uid) or {}
        base_text: str = parsed.text_for_memory() or (
            IMAGE_PLACEHOLDER if parsed.has_image else ""
        )
        # 包成「昵称 / 时间 / 消息」三行块。这一条同时作用于"发给模型的当前消息"
        # 和"落库的历史"，两边保持一致，模型回看历史时也能分清谁在什么时候说了什么。
        memory_text = self._label_message(event, base_text)

        # 风控两条都放在调用模型**之前**：既然要拦，就别先花掉一次 token
        blocked_word = find_blocked_word(memory_text, self.settings.get("blocked_words") or [])
        if blocked_word:
            self.stats.blocked += 1
            self._log(
                f"命中敏感词「{blocked_word}」，已忽略：{event.describe()}",
                level="warn",
            )
            self._on_change()
            return

        allowed, sent_count = self._limiter().check(event.session_id)
        if not allowed:
            self.stats.rate_limited += 1
            wait = self._limiter().retry_after(event.session_id)
            self._log(
                f"{event.session_id} 一分钟内已回复 {sent_count} 条，达到上限，本条已忽略"
                f"（约 {wait:.0f} 秒后恢复；可在「模型配置 → 对话行为」里调整）",
                level="warn",
            )
            self._on_change()
            return

        # 图片：**立即下载**（URL 约 2 小时过期，技术文档坑 17）。
        # base64 绝不入库；落盘只为了以后能把这张图放回上下文。
        prepared: list[PreparedImage] = []
        media_names: list[str] = []
        if parsed.has_image:
            prepared = self._prepare_images(parsed)
            media_names = self._save_media(prepared)

        # ---- 引用消息 + 合并转发：把内容取回来看得见 ----
        #
        # OneBot 的 reply 段**只带消息 ID**，内容得回头调 get_msg 取。
        # 单独发过来的聊天记录（forward 段）同理：解析阶段只认出了它是个转发，
        # 真实内容要在这里展开，否则模型只看到一个 ``[合并转发]`` 占位符。
        # 两处取不到都安静跳过 —— 取内容只是锦上添花，不能把这条回复卡死。
        quoted = self._quoted_content(parsed)
        forwards = self._forward_content(parsed)
        extra_images = quoted.images + forwards.images
        if extra_images:
            extra, problems = self._images.prepare_all(
                extra_images,
                limit=len(extra_images),
                resolve=self._resolve_image_file,
            )
            if problems:
                self._log("引用 / 聊天记录里的部分图片未能处理：" + "；".join(problems), level="warn")
            prepared.extend(extra)

        # 取回的内容拼在用户话的前面，模型才看得懂"他指的是哪条"
        blocks: list[str] = []
        if quoted.text:
            blocks.append(f"【引用了这条消息】{quoted.text}")
        if forwards.text:
            blocks.append(f"【转发的聊天记录】{forwards.text}")
        visible_text = _strip_image_placeholders(parsed.text)
        if blocks:
            head = "\n".join(blocks)
            visible_text = f"{head}\n{visible_text}" if visible_text else head

        # ---- 视频：交给模型自己看（``video_url`` 段） ----
        #
        # 模型**原生支持视频直链**，所以 QLark 不下载、不抽帧、不解码 ——
        # 直接把 NapCat 给的 url 塞进请求，服务端自己拉取解码。
        # 代价是贵（实测 1MB ≈ 1100 video token）且慢（要等它下完），
        # 所以**整个上下文只保留最近那一段**：这一轮发的 / 引用的优先，
        # 都没有时由 ``_history_for_context`` 从历史里挑最近的一条。
        videos = self._pick_videos(parsed, quoted, forwards)
        if videos:
            # 真身都附上了，正文里再留 [视频] 会让模型以为"只收到占位符"
            visible_text = _strip_video_placeholders(visible_text)

        if prepared or videos:
            visible = visible_text or ("请看我发的这段视频。" if videos else "请看这张图片。")
            visible = self._label_message(event, visible)
            user_content: str | list[dict[str, Any]] = build_user_content(
                visible, prepared, videos
            )
        elif blocks:
            user_content = self._label_message(event, visible_text)
        else:
            user_content = memory_text

        rounds = int(self.settings.get("memory_rounds") or 20)

        # 长期记忆摘要作为独立 system 块插入，**不占轮次**（需求规格 §6.3）
        summary_text = self.store.summary_text(key)
        extra_system = [f"{SUMMARY_BLOCK_LABEL} {summary_text}"] if summary_text else []

        # ---- 输出规则：**每一轮都发，永远不掷骰子** ----
        #
        # v2 的核心变化。以前是"掷中了才把规则发给模型"，带来两个问题：
        #   ① 骰子是上下文盲的（用户说「求你了」，骰到极短档就回一个「嗯」）
        #   ② **能力不一致** —— 没掷中的轮次模型根本不知道自己能发图
        #
        # 现在规则永远在场，由模型自己判断要不要配表情、要不要发语音、要不要分段。
        # 代价是失去固定频率控制，所以规则里给的是**判断标准**而不是概率。
        emotions = self._meme_library().emotions() if persona.get("meme_enabled") else []
        voice_on = self._voice_enabled(persona)
        rules = build_output_rules(
            emotions=emotions,
            max_messages=self.settings.split_max_messages,
            voice=voice_on,
            voice_max_chars=self._setting_int("voice_max_chars", 20),
            voice_emotions=VOICE_EMOTIONS,
        )
        delay_ms = int(self._effective(persona, "split_delay_ms", self.settings.split_delay_ms))

        # 末尾指令（离生成点最近，位置决定效果）：
        #   ① 天气   —— 本条消息命中关键词时才有，纯事实
        #   ② 回复判断表 —— 教它按情境判断该回多长、该不该认真
        #   ③ 格式提醒   —— 格式错要重试、代价更高，所以放最后
        weather_block = self._weather_system_block(parsed.text, key)
        trailing_blocks: list[str] = []
        if weather_block:
            # 天气是**事实**，放在离用户消息最近的位置 —— 塞进开头那段 system 时，
            # 中间隔着几十轮历史，模型经常当没看见（实测会回"我没有实时数据"）。
            trailing_blocks.append(weather_block)
        elif self._weather_ready():
            # 功能开着但本轮拿不到数据：禁止"我查一下，稍等"这种空头承诺
            trailing_blocks.append(self.WEATHER_NO_PROMISE_NOTE)
        if bool(self.settings.get("reply_rubric_enabled", True)):
            trailing_blocks.append(REPLY_RUBRIC)
        extra_system.append(rules)
        # 双保险：同一句提醒放**两处** —— 历史之后的 system、以及用户消息文本末尾。
        #
        # 为什么非放两处：只放末尾 system 实测约 83%，剩下的失败集中在
        # "模型特别想表达"的那几句（辞职、相亲、工资）。两处都放 30/30 = 100%。
        # 详见 reply_format.FORMAT_REMINDER 的说明。
        trailing_blocks.append(FORMAT_REMINDER)
        user_content = append_reminder(user_content)
        trailing_system = "\n\n".join(trailing_blocks)

        # 先取历史，再调模型；确认成功后才把这一轮写进记忆。
        # v2：最近几轮的图片以**真身**放回上下文，更早的退化成纯文本。
        # 视频更贵，所以只在"这一轮没带视频"时才从历史里补最近那一段。
        history = self._history_for_context(key, rounds, allow_video=not videos)
        system_prompt = self.store.persona_prompt(persona_uid)
        history = self._fit_budget(history, system_prompt, extra_system, user_content)
        messages = build_chat_messages(
            system_prompt=system_prompt,
            history=history,
            user_content=user_content,
            extra_system=extra_system,
            trailing_system=trailing_system,
        )

        started = time.monotonic()
        plan = self._plan_reply(
            persona,
            messages,
            prepared,
            memory_text,
            videos=videos,
            voice_on=voice_on,
            emotions=emotions,
            delay_ms=delay_ms,
        )
        elapsed = time.monotonic() - started

        # ---- 语音：模型在 JSON 里要了 voice，就把这句话合成成音频 ----
        #
        # 合成失败**一律降级发文字** —— 语音是可选的，绝不能因为 TTS 出问题
        # 就把整条回复弄丢。
        if plan.voice:
            self._synthesize(plan, persona)
        if plan.meme:
            self.stats.memes += 1
        if len(plan.texts) > 1:
            self.stats.splits += 1

        self.db.bump_stats(
            prompt_tokens=plan.prompt_tokens,
            completion_tokens=plan.completion_tokens,
            calls=plan.calls,
        )
        self.db.touch_session(event.session_id)

        try:
            sent = self._sender().send_messages(
                event,
                plan.texts,
                image=plan.image_path,
                audio=plan.audio_path,
                delay_ms=plan.delay_ms if len(plan.texts) > 1 else 0,
            )
        finally:
            # 语音文件只是中转，发完就删
            if plan.audio_path is not None:
                try:
                    plan.audio_path.unlink(missing_ok=True)
                except OSError:
                    pass
        if sent == 0:
            self._log("模型返回空回复，未发送", level="warn")
            return

        # 发送成功后才落库，保证记忆里不会出现"有问无答"的空洞。
        # 用户说的存 memory_text（含 [图片] 占位），**绝不能把 base64 写进记忆**；
        # 视频只存**直链**（一条），下一轮"刚才那个视频"还能看到画面。
        # 机器人自己说的存**模型原始输出**（v2 起就是那个 JSON）—— 见
        # ReplyPlan.memory_text 上对"为什么要存 JSON"的说明。
        self.store.append(
            key, "user", memory_text, has_image=parsed.has_image, media=media_names,
            video=self._video_url(videos),
            msg_id=event.message_id, created_at=event.time or None,
        )
        self.store.append(
            key, "assistant", plan.memory_text,
            tokens=plan.completion_tokens, created_at=event.time or None,
        )

        self.stats.replied += 1
        self.stats.last_reply_at = time.time()
        self.stats.last_error = None
        self.db.bump_stats(msg_in=1, msg_out=sent)

        detail = f"{sent} 条消息"
        if plan.meme:
            detail += f" + 表情「{plan.meme}」"
        if plan.calls > 1:
            detail += f"（重新生成 {plan.calls - 1} 次）"
        self._log(
            f"已回复 {event.session_id}（{elapsed:.1f}s，"
            f"{plan.prompt_tokens}+{plan.completion_tokens} tokens，{detail}）"
        )

        # 回复已经发出去了，压缩放在最后：它慢一点也不会让用户多等
        self._maybe_summarize(key)

    # ------------------------------------------------------------------ 群聊
    def _label_message(self, event: MessageEvent, text: str) -> str:
        """把消息包成「昵称 / 时间 / 消息」三行块（群聊和私聊都做）。

        为什么群聊非做不可：一个群**共用一份记忆**（需求规格 §6.1），所有人的话
        都落在同一条上下文里。不带昵称的话模型看到的是

            user: 你好呀 / assistant: 你好！ / user: 我不吃香菜

        它根本不知道这三句分别是谁说的 —— 会把 A 的忌口记到 B 头上，也没法回答
        "刚才谁说要吃什么"。

        为什么私聊也带上时间：只有一个人，昵称意义不大，但**时间有意义** ——
        模型能据此判断"早上好"是几点的早上好、这句是不是隔了两小时才回。
        """
        if not text:
            return text
        if not bool(self.settings.get("message_label", True)):
            return text
        name = clean_sender_name(event.sender_name)
        if not name:
            return text
        return build_labeled_message(name, event.time, text)

    # ------------------------------------------------------- 随机分段 / 表情包
    def _meme_library(self) -> MemeLibrary:
        """表情库。设置里没配目录就用 ``<程序目录>/memes``。"""
        return MemeLibrary(resolve_meme_dir(self.settings.meme_dir, paths.app_dir() / "memes"))

    # ------------------------------------------------------------ 趣味功能：天气
    #
    # 链路（刻意用"意图 JSON"而不是 function calling：response_format 与 tools
    # 互斥，而这里的回复协议必须保持 json_object）：
    #
    #   关键词粗筛 → 极小的一次调用解析城市 {"city": "成都"}
    #     → 查和风（10 分钟内同城走缓存）→ 渲染成一句人话
    #     → 作为【实时天气】放进**末尾 system**（紧挨用户消息，模型才会真的用）
    #
    # 任何一步失败都安静跳过，回复照常 —— 天气是锦上添花，不能拖垮对话。

    #: 解析城市用的提示词。只要 JSON，不要解释。
    WEATHER_INTENT_PROMPT = (
        "你是天气查询的意图解析器。判断用户这句话是否需要**实时天气数据**才能回答。\n"
        "需要：只输出 {\"city\": \"城市名\"}；用户没提城市就输出 {\"city\": \"\"}。\n"
        "不需要：只输出 {\"city\": null}。\n"
        "只输出这个 JSON，不要任何其他文字。城市名用中文简称（如 成都、北京）。"
    )

    #: 天气话题的"余温"（秒）：这段时间内同一会话再发一句短消息，就当作在聊天气
    WEATHER_FOLLOWUP_WINDOW = 600.0

    #: 查不到数据时的约束。
    #:
    #: 实测踩过：用户只发"菏泽"，关键词没命中 → 没查 → 模型回了"我查一下，稍等"，
    #: 然后**再也没有下文**。这种"空头承诺"比说不知道更伤，必须从提示词上禁掉。
    WEATHER_NO_DATA_NOTE = (
        "（这轮没有实时数据：直接说你查不到或不知道，"
        "不要说「我查一下」「稍等」这类承诺。）"
    )

    #: 功能开着、但本轮没有注入数据时的约束。
    #:
    #: 措辞必须只说"别空口承诺"，**不能说"你没有联网能力"**：
    #: 实测踩过 —— 天气明明开着、上一轮也真查到了伦敦，模型读到
    #: "你没有联网查询能力" 后，用户一纠正城市，它就把**正确的数据**也否了：
    #: 「其实本鲸刚才在瞎编，我没有查天气的能力，对不起」。这种假道歉比不答更伤。
    WEATHER_NO_PROMISE_NOTE = (
        "（手头没有实时数据时：直接说不知道就好，"
        "不要把「我查一下」「稍等」这类话当作回复。）"
    )

    def _weather_ready(self) -> bool:
        """天气开关开着、且 Host/Key 都配了，才值得往下走。"""
        if not fun_config.weather_on(self.settings):
            return False
        return bool(
            str(self.settings.get("qweather_host") or "").strip()
            and str(self.settings.get("qweather_key") or "").strip()
        )

    def _weather_recent(self, key: str) -> bool:
        """这个会话刚才聊过天气吗？（用来接住"洛宁呢""菏泽"这种省略问句）"""
        with self._weather_lock:
            stamp = self._weather_seen.get(key)
        return bool(stamp) and (time.monotonic() - stamp) <= self.WEATHER_FOLLOWUP_WINDOW

    def _weather_mark(self, key: str, city: str = "") -> None:
        with self._weather_lock:
            self._weather_seen[key] = time.monotonic()
            if city:
                self._weather_last_city[key] = city
            if len(self._weather_seen) > 200:  # 防止长期运行无限增长
                cutoff = time.monotonic() - self.WEATHER_FOLLOWUP_WINDOW
                self._weather_seen = {
                    name: stamp for name, stamp in self._weather_seen.items() if stamp >= cutoff
                }

    def _weather_last_city_for(self, key: str) -> str:
        with self._weather_lock:
            return self._weather_last_city.get(key, "")

    @staticmethod
    def _looks_like_weather_followup(text: str) -> bool:
        """天气话题里的短追问/更正："洛宁呢" / "那明天呢" / "菏泽" / "我不在成都，我在杭州"。

        只认**很短**的消息（≤25 字）：太长的多半在聊别的。是否真去查仍由意图模型判断
        （它会返回 ``{"city": null}``），这里只是决定"值不值得多问一次模型"。

        以前还要求带"呢/吗/？"或纯地名，结果把更正句挡在外面 ——
        用户说"我不在伦敦，我在杭州"时完全不查，模型只能顺着上一轮硬聊。
        """
        content = str(text or "").strip()
        return bool(content) and len(content) <= 25

    def _weather_system_block(self, text: str, key: str) -> str:
        """返回一段 ``【实时天气】``；没数据时返回"别承诺去查"的约束；都不需要返回空串。"""
        if not self._weather_ready():
            return ""

        words = fun_config.keywords(self.settings)
        if not fun_weather.looks_like_weather_question(text, words):
            # 关键词没命中时，只有在"刚聊过天气 + 这是个短追问"的情况下才多花一次调用
            if not self._weather_recent(key) or not self._looks_like_weather_followup(text):
                return ""

        # ① 快路：句子里本来就写着城市（"成都热死了"）→ 正则取出来，GeoAPI 验证通过就用。
        #    完全跳过意图调用：省一次 ~1s 的模型调用，也避开"意图超时 → 这轮没天气"
        #    那个坑（实测：8s 超时，而"成都"就明晃晃写在句子里）。
        for candidate in fun_weather.city_candidates(text, words):
            brief = self._weather_brief(candidate, quiet=True)
            if brief:
                self._weather_mark(key, candidate)
                self._log(f"天气：句子里直接认出「{candidate}」，跳过意图调用", level="debug")
                return self._weather_use_block(brief)

        # ② 慢路：城市得靠模型抽（"那明天呢""菏泽""我不在伦敦，我在杭州"）
        city = self._weather_city_from(text, key)
        if city is None:
            # 模型明确判断不需要实时数据（例如"热干面真好吃"）
            return ""
        self._weather_mark(key, city)
        city = city or str(self.settings.get("qweather_city") or fun_config.DEFAULT_CITY)
        brief = self._weather_brief(city)
        if not brief:
            return self.WEATHER_NO_DATA_NOTE
        return self._weather_use_block(brief)

    @staticmethod
    def _weather_use_block(brief: str) -> str:
        return (
            f"【实时天气】{brief}\n"
            "（上面是刚刚查到的真实数据，请直接用；"
            "不要说自己没有实时数据，也不要说「我查一下」这类话。）"
        )

    def _weather_city_from(self, text: str, key: str = "") -> str | None:
        """用一次极小调用把城市名抽出来。

        Returns:
            城市名（可能是空串，表示"用默认城市"）；``None`` 表示模型判断不需要天气。
        """
        client = self._llm(None)
        # 带上"上文问过哪个城市"：否则"那明天呢"这种追问会被解析成空城市，
        # 然后拿默认城市去查（用户问的是洛阳，回了个北京，很出戏）。
        last_city = self._weather_last_city_for(key) if key else ""
        user_text = str(text or "")[:400]
        if last_city:
            user_text = f"（上文刚问过：{last_city}）\n{user_text}"
        messages = [
            {"role": "system", "content": self.WEATHER_INTENT_PROMPT},
            {"role": "user", "content": user_text},
        ]
        try:
            result = client.complete(messages, retries=0, timeout=8)
        except LLMError as exc:
            # 解析失败就别硬查了：用默认城市查一次也未必对，宁可跳过
            self._log(f"天气意图解析失败，本轮跳过：{exc}", level="warn")
            return None

        self.db.bump_stats(
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            calls=1,
        )
        payload = _json_object(result.text)
        if not isinstance(payload, dict) or "city" not in payload:
            self._log(f"天气意图不是预期 JSON，本轮跳过：{_preview(result.text, 80)}", level="warn")
            return None
        city = payload.get("city")
        if city is None:
            return None
        return str(city).strip()

    def _weather_brief(self, city: str, quiet: bool = False) -> str:
        """查天气（带缓存）。失败返回空串，绝不影响回复。

        ``quiet=True`` 用于"正则猜出来的候选城市"：猜错很正常，不该刷 WARN。
        """
        cached = self._weather_cache.get(city)
        if cached:
            self._log(f"天气命中缓存：{city}", level="debug")
            return cached

        started = time.monotonic()
        client: fun_weather.QWeather | None = None
        try:
            client = fun_weather.QWeather(
                str(self.settings.get("qweather_host") or ""),
                str(self.settings.get("qweather_key") or ""),
            )
            brief = client.brief(city)
        except fun_weather.WeatherError as exc:
            self._log(
                f"天气查询失败（{city}）：{exc}",
                level="debug" if quiet else "warn",
            )
            return ""
        except Exception as exc:  # noqa: BLE001 - 天气不能拖垮回复
            log.exception("天气查询未预期失败")
            self._log(f"天气查询异常（{city}）：{exc}", level="error")
            return ""
        finally:
            if client is not None:
                client.close()

        elapsed = int((time.monotonic() - started) * 1000)
        self._weather_cache.put(city, brief)
        self.stats.weathers += 1
        self._log(f"天气已注入：{brief}（{elapsed}ms）")
        return brief


    def _effective(self, persona: dict[str, Any] | None, key: str, fallback: Any) -> Any:
        """读人设级数值；**留空（NULL / 空串）就跟随全局设置**。

        注意不能把"留空"和"0"混为一谈：概率填 0 是"永不触发"，
        留空是"用全局的"。所以空值存进库时是 NULL 而不是 0。
        """
        value = (persona or {}).get(key)
        if value is None or value == "":
            return fallback
        return value

    def effective_chance(self, persona_uid: int, key: str) -> float:
        """这个会话**实际生效**的概率（人设覆盖值优先，其次全局）。

        界面用它显示"这一档到底是多少"。不显示的话就会出现这种情况：
        用户把全局设成 90%，但某个人设上存着 10%，那个会话永远是 10%，
        **界面上完全看不出来** —— 看起来就像"概率坏了"。
        """
        persona = self.personas.get(persona_uid)
        enabled_key = "meme_enabled" if key.startswith("meme") else "split_enabled"
        if persona is not None and not persona.get(enabled_key):
            return 0.0
        try:
            value = float(self._effective(persona, key, self.settings.get(key, 0.0)))
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, value))

    def voice_prompt(self, persona: dict[str, Any] | None) -> str:
        """这个角色的音色描述：人设上写了就用它的，没写用全局默认。"""
        value = str((persona or {}).get("voice_prompt") or "").strip()
        return value or str(self.settings.get("voice_prompt") or "").strip()

    def _voice_enabled(self, persona: dict[str, Any] | None) -> bool:
        """这个角色这一轮能不能发语音。

        总开关关着就一律不发；人设上没勾也一律不发；没有音色描述也不发
        （否则合成出来的声音是随机的，不如不发）。

        **v2 没有语音骰子** —— 要不要发由模型判断，这里只管"能不能"。
        """
        if not bool(self.settings.get("voice_enabled", False)):
            return False
        if not (persona or {}).get("voice_enabled"):
            return False
        return bool(self.voice_prompt(persona))

    def _setting_int(self, key: str, fallback: int) -> int:
        """读一个整数配置；类型不对就用默认值（DB 里可能存着字符串）。"""
        try:
            return int(float(self.settings.get(key, fallback)))
        except (TypeError, ValueError):
            return fallback

    def _setting_float(self, key: str, fallback: float) -> float:
        """读一个浮点配置（合并时长按秒算，允许小数）。"""
        try:
            return float(self.settings.get(key, fallback))
        except (TypeError, ValueError):
            return fallback

    # ------------------------------------------------------------------ 视频
    def _video_enabled(self) -> bool:
        return bool(self.settings.get("video_understanding", True))

    def _video_timeout(self) -> float:
        """带视频的请求超时：服务端要先下载解码，默认的 60s 不够。"""
        return max(30.0, self._setting_float("video_timeout_s", VIDEO_TIMEOUT_S))

    @staticmethod
    def _video_url(videos: list[VideoRef] | None) -> str | None:
        for video in videos or []:
            url = str(video.url or "").strip()
            if url:
                return url
        return None

    def _pick_videos(
        self,
        parsed: ParsedMessage,
        quoted: QuotedContent,
        forwards: QuotedContent | None = None,
    ) -> list[VideoRef]:
        """这一轮要附上哪一段视频。**最多一段。**

        优先级：本条消息里的视频 > 被引用的那条消息里的视频 > 转发的聊天记录里的。
        历史里更早的视频不在这里补 —— 那是 ``_history_for_context`` 的事，
        而且只有"这一轮没带视频"时才会去补。
        """
        if not self._video_enabled():
            return []
        collected = list(parsed.videos) + list(quoted.videos)
        if forwards is not None:
            collected.extend(forwards.videos)
        candidates = [video for video in collected if str(video.url or "").strip()]
        if not candidates:
            return []
        if len(candidates) > 1:
            self._log(
                f"这一轮有 {len(candidates)} 段视频，只把最近的一段交给模型",
                level="debug",
            )
        return candidates[:1]

    def _plan_reply(
        self,
        persona: dict[str, Any] | None,
        messages: list[dict[str, Any]],
        prepared: list[PreparedImage],
        memory_text: str,
        *,
        videos: list[VideoRef] | None = None,
        voice_on: bool = False,
        emotions: list[str] | None = None,
        delay_ms: int = 0,
    ) -> ReplyPlan:
        """调用模型 → 解析 → 重新生成 → 抢救 → 兜底，产出一份"要发什么"的计划。"""
        spec = {
            # v2：规则每一轮都在，所以**永远按 JSON 校验**。
            # 模型输出非 JSON 才算失败（会重试一次再抢救）。
            "expect_json": True,
            "max_messages": self.settings.split_max_messages,
            "emotions": emotions or [],
            "allow_voice": voice_on,
            "voice_max_chars": self._setting_int("voice_max_chars", 20),
            "voice_emotions": VOICE_EMOTIONS,
        }
        result = self._complete_with_vision_fallback(
            persona, messages, prepared, memory_text, videos=videos
        )
        parsed = parse_reply(result.text, **spec)
        plan = ReplyPlan(
            texts=[],
            calls=1,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            delay_ms=delay_ms,
            raw_text=str(result.text or ""),
        )
        if parsed.ok:
            plan.texts = parsed.messages
            plan.meme = parsed.meme
            plan.voice = parsed.voice
            plan.emotion = parsed.emotion
            self._attach_meme(plan)
            return plan

        # 被 max_tokens 截断导致的"格式不对"要单独说清楚：
        # 这种情况重试同样的长度毫无意义，得让用户去调大上限。
        if (result.finish_reason or "").lower() == "length":
            self._log(
                f"输出被 max_tokens 截断（finish_reason=length），JSON 不完整。"
                f"建议在「模型配置」里调大「最大 Token」（当前 {self.settings.get('max_tokens')}）；"
                f"截断内容：{_preview(parsed.raw, 120)}",
                level="error",
            )

        # ① 重新生成：**把"错在哪"告诉它**，否则它很可能原样再错一遍
        attempts = self.settings.reply_retry_max
        for index in range(attempts):
            self._log(
                f"输出不是合法 JSON（{parsed.error}），重新生成（{index + 1}/{attempts}）；"
                f"模型原文：{_preview(parsed.raw)}",
                level="warn",
            )
            retry_messages = [
                *messages,
                {"role": "assistant", "content": parsed.raw[:600]},
                {"role": "user", "content": RETRY_INSTRUCTION.format(error=parsed.error)},
            ]
            try:
                follow = self._complete_with_vision_fallback(
                    persona, retry_messages, prepared, memory_text, videos=videos
                )
            except LLMError as exc:
                self._log(f"重新生成失败，不再重试：{exc}", level="warn")
                break
            plan.calls += 1
            plan.prompt_tokens += follow.prompt_tokens
            plan.completion_tokens += follow.completion_tokens
            parsed = parse_reply(follow.text, **spec)
            # 重试成功了就以这一轮为准 —— 记忆里要存**最终生效**的那份输出
            plan.raw_text = str(follow.text or "")
            if parsed.ok:
                plan.texts = parsed.messages
                plan.meme = parsed.meme
                plan.voice = parsed.voice
                plan.emotion = parsed.emotion
                self._attach_meme(plan)
                return plan

        # ② 抢救：从坏 JSON 里把引号内容抠出来。大多数"少个逗号"都能救回来，
        #    比直接甩一句"服务器宕机了"体面得多。
        rescued = salvage(parsed.raw, max_messages=self.settings.split_max_messages)
        if rescued:
            plan.degraded = f"JSON 解析失败（{parsed.error}），已从原文抢救"
            plan.texts = rescued
            plan.meme = None
            self._log(f"{plan.degraded}：{' / '.join(rescued)[:80]}", level="warn")
            return plan

        # ③ 兜底
        plan.degraded = f"JSON 解析失败（{parsed.error}），且无可抢救内容"
        plan.texts = [self.settings.reply_fallback_text]
        plan.meme = None
        self._log(
            f"{plan.degraded}，已发送兜底文案；模型原文：{_preview(parsed.raw)}",
            level="error",
        )
        return plan

    def _synthesize(self, plan: ReplyPlan, persona: dict[str, Any] | None) -> None:
        """把 ``plan.texts`` 合成成语音写进 ``plan.audio_path``。

        **任何失败都只是退回文字**（``plan.voice = False``），不抛异常、
        不影响已经生成好的回复。
        """
        spoken = " ".join(plan.texts).strip()
        if not spoken:
            plan.voice = False
            return
        try:
            client = self._llm_tts()
            result = client.speak(
                spoken,
                voice_prompt=self.voice_prompt(persona),
                emotion=plan.emotion or VOICE_EMOTIONS[0],
            )
        except TTSError as exc:
            plan.voice = False
            self.stats.voice_fallbacks += 1
            self._log(f"语音合成失败，改发文字（{exc.hint()}：{exc}）", level="warn")
            return
        except Exception as exc:  # noqa: BLE001 兜底：语音绝不能拖垮回复
            plan.voice = False
            self.stats.voice_fallbacks += 1
            log.exception("语音合成未预期失败")
            self._log(f"语音合成异常，改发文字：{exc}", level="error")
            return

        path = Path(tempfile.gettempdir()) / f"qlark-voice-{uuid.uuid4().hex}.{result.fmt}"
        try:
            path.write_bytes(result.audio)
        except OSError as exc:
            plan.voice = False
            self.stats.voice_fallbacks += 1
            self._log(f"语音文件写入失败，改发文字：{exc}", level="warn")
            return
        plan.audio_path = path
        self.stats.voices += 1
        self._log(
            f"语音合成完成（{result.fmt}，{len(result.audio) / 1024:.1f} KB，"
            f"语气「{plan.emotion or VOICE_EMOTIONS[0]}」）",
            level="debug",
        )

    def _llm_tts(self) -> VoiceDesignClient:
        """语音合成客户端。**共用对话的 base_url / api_key**，只单独配模型名。"""
        client = self._tts_client
        if client is None:
            client = VoiceDesignClient(
                str(self.settings.get("base_url") or ""),
                str(self.settings.get("api_key") or ""),
                str(self.settings.get("tts_model") or DEFAULT_TTS_MODEL),
                timeout=self._setting_int("tts_timeout_s", 20),
            )
            self._tts_client = client
        return client

    def _attach_meme(self, plan: ReplyPlan) -> None:
        """按情绪名挑一张表情图；挑不到就不配（绝不能因此报错）。

        **这里绝不做"随机补一张"。** 做过一次，被否掉了，否得对：
        配一张和这句话毫无关系的图，比不配更糟 —— 那正是"假"的来源。
        所以模型给的名字对不上时，宁可这一轮没有图。

        真正该修的是**让模型别编名字**（见 ``reply_format.MEME_RULE_DEFAULT``
        里"只能原样复制"的写法），而不是在下面偷偷兜底。
        """
        if not plan.meme:
            return
        item = self._meme_library().pick(plan.meme)
        if item is None:
            self._log(
                f"模型给的表情「{plan.meme}」在目录里找不到，本轮不配图"
                "（这是模型没按列表取词，不是概率没生效）",
                level="warn",
            )
            plan.meme = None
            return
        plan.image_path = item.path

    # ------------------------------------------------------------------ 记忆
    def _context_budget(self) -> int:
        try:
            value = int(self.settings.get("max_context_chars") or DEFAULT_CONTEXT_BUDGET)
        except (TypeError, ValueError):
            value = DEFAULT_CONTEXT_BUDGET
        return max(2000, value)

    def _media_dir(self) -> Path:
        """聊天里收到过的图片存这里（只存文件名进数据库）。"""
        return paths.data_dir() / "media"

    def _save_media(self, prepared: list[PreparedImage]) -> list[str]:
        """把本轮预处理好的图片落到磁盘，返回**文件名**列表。

        为什么要落盘：v2 起历史里回放的是**真实的图**，而不是 ``[图片]`` 占位符。
        数据库里只记文件名（base64 绝不入库），要用的时候再读回来。
        """
        names: list[str] = []
        if not prepared:
            return names
        folder = self._media_dir()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._log(f"图片目录创建失败，本轮图片不进记忆：{exc}", level="warn")
            return names
        for image in prepared:
            try:
                encoded = image.data_url.split(",", 1)[1]
                raw = base64.b64decode(encoded)
            except (IndexError, ValueError):
                continue
            name = f"{uuid.uuid4().hex}.jpg"
            try:
                (folder / name).write_bytes(raw)
            except OSError as exc:
                self._log(f"图片落盘失败，跳过：{exc}", level="warn")
                continue
            names.append(name)
        return names

    def _history_for_context(
        self, key: str, rounds: int, *, allow_video: bool = True
    ) -> list[tuple[str, Any]]:
        """取历史；最近 ``media_history_rounds`` 轮的图片**以真身放回上下文**。

        更早的轮次变成纯文本（``[图片]`` 占位符会被去掉，不留空壳）。
        一张 512px 约 258 token，回溯两轮多花 500 左右，完全划得来。

        视频规则不一样：**整个上下文只保留最近那一段**。视频比图贵四倍多
        （实测 1MB ≈ 1100 video token），而且"刚才那个视频"指的一定是最新那段。
        所以：
          · 只有 ``allow_video``（这一轮自己没带视频）时才从历史里补；
          · 只补**一条**（时间上最晚的那条），更早的退化成 ``[视频]`` 文本；
          · 直链带 ``rkey`` 约 2 小时过期，过期的干脆按占位文本处理，
            否则服务端只会回一个"拉不到视频"的错误。
        """
        keep = self._setting_int("media_history_rounds", 2)
        rows = self.store.recent_rounds_with_media_and_video(key, rounds, media_rounds=keep)

        chosen = -1
        if allow_video and bool(self.settings.get("video_understanding", True)):
            now = time.time()
            for index in range(len(rows) - 1, -1, -1):
                url = rows[index][3]
                if not url:
                    continue
                created = rows[index][4]
                age = (now - float(created)) if created else 0.0
                if age > VIDEO_URL_TTL_S:
                    self._log(
                        f"历史视频直链已过期（{age / 60:.0f} 分钟前），按 {VIDEO_PLACEHOLDER} 处理",
                        level="debug",
                    )
                    break
                chosen = index
                break

        out: list[tuple[str, Any]] = []
        for index, (role, content, names, video, _created) in enumerate(rows):
            parts: list[dict[str, Any]] = []
            if names:
                parts = self._media_parts(names)
            text = _strip_image_placeholders(content).strip()
            if index == chosen and video:
                # 真身要附上，正文里的占位符就必须去掉（否则模型以为只收到占位符）
                text = _strip_video_placeholders(text)
                parts = [*parts, VideoRef(url=video).to_segment()]
            if not parts:
                out.append((role, text))
                continue
            blocks: list[dict[str, Any]] = []
            if text:
                blocks.append({"type": "text", "text": text})
            blocks.extend(parts)
            out.append((role, blocks))
        return out

    def _media_parts(self, names: list[str]) -> list[dict[str, Any]]:
        """按文件名读回图片，拼成 ``image_url`` 段。读不到就跳过（不报错）。"""
        parts: list[dict[str, Any]] = []
        folder = self._media_dir()
        for name in names:
            try:
                raw = (folder / name).read_bytes()
            except OSError:
                continue
            if not raw:
                continue
            encoded = base64.b64encode(raw).decode("ascii")
            parts.append(
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}}
            )
        return parts

    def _fit_budget(
        self,
        history: list[tuple[str, Any]],
        system_prompt: str,
        extra_system: list[str],
        user_content: str | list[dict[str, Any]],
    ) -> list[tuple[str, Any]]:
        """超出上下文预算时，**从最早的原文开始丢弃，长期记忆摘要始终保留**。

        需求规格 §6.3 的硬规则：宁可丢掉最早的寒暄，也不能丢掉被压缩过的
        长期信息 —— 那部分内容一旦丢了就再也找不回来（原文已经被标记为已压缩）。
        """
        budget = self._context_budget()
        fixed = len(system_prompt or "") + sum(len(block) for block in extra_system)
        if isinstance(user_content, str):
            fixed += len(user_content)
        elif isinstance(user_content, list):
            fixed += _content_cost(user_content)

        kept: list[tuple[str, Any]] = []
        used = fixed
        for role, content in reversed(history):
            cost = _content_cost(content) + 8
            if kept and used + cost > budget:
                break
            used += cost
            kept.append((role, content))
        kept.reverse()

        dropped = len(history) - len(kept)
        if dropped:
            # 提到 WARN：被丢的这批原文**还没进摘要**（压缩按 W 触发，可能还没到门槛），
            # 丢一次就是这轮对话永久少了一段。用户至少得在日志里看得见。
            self._log(
                f"上下文超预算（{used}/{budget} 字符），已丢弃最早的 {dropped} 条原文；"
                "这部分内容还没进摘要，本次回复看不到它们（长期记忆摘要仍在）",
                level="warn",
            )
        return kept

    def _maybe_summarize(self, key: str) -> None:
        """按 W 轮触发合并式压缩；把总结调用的 token 也计入今日统计。"""
        try:
            outcome = self.summarizer.maybe_summarize(key)
        except Exception:  # 兜底：压缩绝不能影响已经完成的这次回复
            log.exception("记忆压缩未预期失败")
            return
        if outcome.status == "ok":
            self.stats.summaries += 1
        elif outcome.status == "not_enough":
            # 把"还差几轮"写进日志：以前这里什么都不说，用户只能靠猜，
            # 而记忆面板显示的又是"条数"，很容易误判成压缩坏了。
            window = self.summarizer.window
            log.debug("记忆压缩未触发：未压缩 %s/%s 轮", outcome.rounds, window)
            if outcome.rounds + 1 == window:
                self._log(
                    f"记忆还差 1 轮就自动压缩（当前未压缩 {outcome.rounds}/{window} 轮）",
                    level="info",
                )
        if outcome.did_call_model:
            self.db.bump_stats(
                prompt_tokens=outcome.prompt_tokens,
                completion_tokens=outcome.completion_tokens,
                calls=1,
            )
            self._on_change()

    # ------------------------------------------------------------------ 错误
    def _on_llm_error(self, event: MessageEvent, exc: LLMError) -> None:
        self.stats.failed += 1
        self.stats.last_error = f"{exc.hint()}：{exc}"
        self.db.bump_stats(calls=1, errors=1)
        self._log(f"模型调用失败（{exc.kind.value}）：{exc}", level="error")

        # 群聊里回错误提示会很吵，默认只在私聊告知
        if not event.is_group and self.settings.notify_error_private:
            self._sender().send_notice(event, f"抱歉，我这边出问题了：{exc.hint()}")

    def _on_queue_full(self, session_id: str, pending: int) -> None:
        self._log(f"{session_id} 积压 {pending} 条，已丢弃新消息", level="warn")

    # ------------------------------------------------------------------ 依赖
    def _llm(self, persona: dict[str, Any] | None = None) -> OpenAIChatClient:
        """按"人设覆盖优先、全局兜底"构造客户端，配置没变就复用连接。"""
        persona = persona or {}
        base_url = str(self.settings.get("base_url") or "")
        api_key = str(self.settings.get("api_key") or "")
        model = str(persona.get("model_override") or self.settings.get("model") or "")

        max_tokens = persona.get("max_tokens_override")
        if max_tokens in (None, ""):
            max_tokens = self.settings.get("max_tokens")

        temperature = persona.get("temperature_override")
        if temperature is None:
            temperature = self.settings.get("temperature")

        thinking = str(self.settings.get("thinking_mode") or "")
        # v2 每一轮都要 JSON：双保险的第一道 response_format。
        # 不支持的服务（或"认了但返回空"的，比如 DeepSeek 带历史时）会被客户端
        # 自动关掉并重试一次 —— 见 llm/client.py 里的降级逻辑。
        json_mode = bool(self.settings.get("reply_json_mode", True))

        signature = (
            base_url, api_key, model, str(max_tokens), str(temperature), thinking,
            str(json_mode),
        )
        cached = self._llm_cache
        if cached is not None and cached[0] == signature:
            return cached[1]
        client = OpenAIChatClient(
            base_url,
            api_key,
            model,
            max_tokens=max_tokens,
            temperature=temperature,
            thinking=thinking,
            json_mode=json_mode,
        )
        self._llm_cache = (signature, client)
        return client

    # ------------------------------------------------------------------ 图片
    def _quoted_content(self, parsed: ParsedMessage) -> QuotedContent:
        """取回被引用的消息内容。取不到就返回空（安静降级）。"""
        if not parsed.reply_id:
            return QuotedContent()
        if not bool(self.settings.get("quote_context_enabled", True)):
            return QuotedContent()
        fetcher = self._quoted_fetcher
        if fetcher is None:
            fetcher = QuotedFetcher(self._onebot, max_chars=MAX_QUOTED_CHARS)
            self._quoted_fetcher = fetcher
        try:
            content = fetcher.fetch(str(parsed.reply_id))
        except Exception as exc:  # noqa: BLE001 兜底：引用绝不能拖垮回复
            log.exception("引用消息处理未预期失败")
            self._log(f"引用消息处理失败，忽略引用：{exc}", level="warn")
            return QuotedContent()
        if content.ok:
            self.stats.quotes += 1
            self._log(
                f"引用消息已取回（{len(content.text)} 字，"
                f"{len(content.images)} 张图，类型：{'/'.join(content.kinds) or '无'}）",
                level="debug",
            )
        else:
            self._log(f"引用消息取不到内容（id={parsed.reply_id}），按无引用处理", level="debug")
        return content

    def _forward_content(self, parsed: ParsedMessage) -> QuotedContent:
        """展开**单独发过来的**聊天记录。

        解析阶段只认出"这是个转发"（正文留 ``[合并转发]`` 占位符），
        真实内容要在这里取 —— 否则模型眼里只有那四个字。

        和引用一样：取不到就安静跳过，不影响回复。
        """
        if not parsed.forwards:
            return QuotedContent()
        if not bool(self.settings.get("quote_context_enabled", True)):
            return QuotedContent()

        fetcher = self._quoted_fetcher
        if fetcher is None:
            fetcher = QuotedFetcher(self._onebot, max_chars=MAX_QUOTED_CHARS)
            self._quoted_fetcher = fetcher

        merged = QuotedContent()
        for data in parsed.forwards:
            try:
                content = fetcher.fetch_forward(data)
            except Exception as exc:  # noqa: BLE001 兜底：聊天记录取不到不能拖垮回复
                log.exception("展开聊天记录未预期失败")
                self._log(f"展开聊天记录失败，忽略：{exc}", level="warn")
                continue
            if not content.ok:
                continue
            if merged.text and content.text:
                merged.text = f"{merged.text}\n{content.text}"
            elif content.text:
                merged.text = content.text
            merged.images.extend(content.images)
            merged.ok = True

        if merged.ok:
            self.stats.quotes += 1
            self._log(
                f"聊天记录已展开（{len(merged.text)} 字，{len(merged.images)} 张图）",
                level="debug",
            )
        else:
            self._log("聊天记录取不到内容，按 [合并转发] 占位处理", level="debug")
        return merged

    def _prepare_images(self, parsed: ParsedMessage) -> list[PreparedImage]:
        """立即下载并预处理图片；任何一张失败都不影响其余。"""
        if not bool(self.settings.get("image_understanding", True)):
            self._log("图片理解已关闭，按 [图片] 占位处理", level="debug")
            return []
        try:
            limit = int(self.settings.get("max_images_per_message") or DEFAULT_MAX_IMAGES)
        except (TypeError, ValueError):
            limit = DEFAULT_MAX_IMAGES

        images, problems = self._images.prepare_all(
            parsed.images, limit=limit, resolve=self._resolve_image_file
        )
        if problems:
            self._log("部分图片未能处理：" + "；".join(problems), level="warn")
        if images:
            total_kb = max(1, sum(image.size for image in images) // 1024)
            self._log(
                f"已下载 {len(images)} 张图片（约 {total_kb} KB），将交给视觉模型",
                level="debug",
            )
        return images

    def _resolve_image_file(self, ref: Any) -> str | None:
        """``url`` 缺失时向 NapCat 要一次（OneBot 的 ``get_image``）。"""
        file_value = str(getattr(ref, "file", "") or "").strip()
        if not file_value:
            return None
        try:
            payload = self._onebot().call("get_image", {"file": file_value})
        except OneBotError as exc:
            self._log(f"向 NapCat 索取图片地址失败：{exc}", level="warn")
            return None
        data = payload.get("data") or {}
        resolved = str(data.get("url") or data.get("file") or "").strip()
        return resolved or None

    def _complete_with_vision_fallback(
        self,
        persona: dict[str, Any] | None,
        messages: list[dict[str, Any]],
        prepared: list[PreparedImage],
        memory_text: str,
        *,
        videos: list[VideoRef] | None = None,
    ) -> ChatResult:
        """带图 / 带视频调用；模型明确表示不支持时，降级成纯文本再试一次。

        需求规格 §6.5：「模型不支持视觉时降级」。降级而不是直接报错，是因为
        用户至少还想知道"他发了什么文字"；而且**明确告诉用户去配视觉模型**，
        否则他会一直以为图片被看懂了。

        视频走的是同一条路：模型自己按 ``video_url`` 去拉直链、解码，
        QLark 不下载也不抽帧 —— 所以这里的"不支持"判断对图片和视频都适用。
        """
        videos = list(videos or [])
        if not prepared and not videos:
            return self._llm(persona).complete(messages)

        vision_model = str(self.settings.get("vision_model") or "").strip()
        client = self._llm_vision(persona) if vision_model else self._llm(persona)
        # 视频要多等：服务端得先把整段视频拉下来解码（实测 1MB ≈ 60s）
        timeout = self._video_timeout() if videos else None

        try:
            result = client.complete(messages, timeout=timeout)
        except LLMError as exc:
            if not looks_like_vision_unsupported(str(exc)):
                raise
            self.stats.vision_fallbacks += 1
            what = "视频" if videos and not prepared else "图片"
            self._log(
                f"当前模型不支持{what}输入，已降级为纯文本。"
                "想让机器人看懂图片/视频，请在「模型配置 → 视觉模型」里填一个支持多模态的模型"
                f"（原始错误：{exc}）",
                level="warn",
            )
            return self._llm(persona).complete(_strip_media_parts(messages, memory_text))

        if prepared:
            self.stats.images += 1
        if videos:
            self.stats.videos += 1
            self._log(
                f"视频已附上（{len(videos)} 段），交给模型直接看画面",
                level="debug",
            )
        return result

    def _llm_vision(self, persona: dict[str, Any] | None = None) -> OpenAIChatClient:
        """视觉模型客户端：其余参数（温度 / 上限）沿用全局与当前人设。"""
        persona = persona or {}
        base_url = str(self.settings.get("base_url") or "")
        api_key = str(self.settings.get("api_key") or "")
        model = str(self.settings.get("vision_model") or "").strip() or str(
            persona.get("model_override") or self.settings.get("model") or ""
        )

        max_tokens = persona.get("max_tokens_override")
        if max_tokens in (None, ""):
            max_tokens = self.settings.get("max_tokens")
        temperature = persona.get("temperature_override")
        if temperature is None:
            temperature = self.settings.get("temperature")

        signature = ("vision", base_url, api_key, model, str(max_tokens), str(temperature))
        cached = self._vision_cache
        if cached is not None and cached[0] == signature:
            return cached[1]
        client = OpenAIChatClient(
            base_url,
            api_key,
            model,
            max_tokens=max_tokens,
            temperature=temperature,
            thinking=str(self.settings.get("thinking_mode") or ""),
        )
        self._vision_cache = (signature, client)
        return client

    def _llm_summary(self) -> OpenAIChatClient:
        """总结专用客户端：用「总结模型」，没配就退回对话模型。

        温度压到 0.3、输出上限压到 800 —— 摘要是要稳定复现的系统任务，
        不需要也不会让它发挥。
        """
        base_url = str(self.settings.get("base_url") or "")
        api_key = str(self.settings.get("api_key") or "")
        model = str(self.settings.get("summary_model") or "").strip() or str(
            self.settings.get("model") or ""
        )
        signature = ("summary", base_url, api_key, model)
        cached = self._summary_cache
        if cached is not None and cached[0] == signature:
            return cached[1]

        try:
            configured = int(self.settings.get("max_tokens") or 0)
        except (TypeError, ValueError):
            configured = 0
        # 下限别省：这个预算是**推理 + 正文**共用的，太小的话推理一挤就什么都不剩，
        # 摘要永远压不出来（实测 512 被 reasoning 全部吃掉，正文为空）。
        max_tokens = (
            max(SUMMARY_MIN_TOKENS, min(configured, SUMMARY_MAX_TOKENS))
            if configured > 0
            else SUMMARY_MAX_TOKENS
        )

        client = OpenAIChatClient(
            base_url,
            api_key,
            model,
            max_tokens=max_tokens,
            temperature=SUMMARY_TEMPERATURE,
            # 摘要是有标准答案的机械任务，花推理 token 纯属浪费
            thinking="off",
        )
        self._summary_cache = (signature, client)
        return client

    def _summarize_call(self, messages: list[dict[str, Any]]) -> ChatResult:
        """摘要器注入的模型调用：只重试 1 次（失败会被标记 pending，下轮再来）。"""
        return self._llm_summary().complete(messages, retries=1)

    def _limiter(self) -> RateLimiter:
        """速率限制器。配置改了就地重建（窗口会重置，可以接受）。"""
        try:
            limit = int(self.settings.get("rate_limit_per_minute") or 0)
        except (TypeError, ValueError):
            limit = 0
        current = self._rate_limiter
        if current is None or current.limit != limit:
            current = RateLimiter(limit=limit)
            self._rate_limiter = current
        return current

    def _onebot(self) -> OneBotClient:
        """到 NapCat 动作端口的客户端（配置没变就复用连接）。"""
        signature = (self.settings.onebot_base_url, self.settings.onebot_token)
        cached = self._ob_cache
        if cached is None or cached[0] != signature:
            cached = (signature, OneBotClient(signature[0], signature[1]))
            self._ob_cache = cached
        return cached[1]

    def _sender(self) -> ReplySender:
        return ReplySender(
            self._onebot(),
            quote_group=self.settings.reply_quote_group,
            max_chars=self.settings.max_reply_chars,
        )

    # ------------------------------------------------------------------ 日志
    def _log(self, message: str, *, level: str = "info") -> None:
        self.logbus.add(message, level=level, source=LogSource.QLARK.value)


__all__ = ["IMAGE_PLACEHOLDER", "MessagePipeline", "PipelineStats"]
