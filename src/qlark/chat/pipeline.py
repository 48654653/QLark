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
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .. import paths
from ..core.models import LogSource
from ..llm.client import ChatResult, LLMError, OpenAIChatClient
from ..napcat.logbus import LogBus
from ..onebot.client import OneBotClient, OneBotError
from ..onebot.events import GROUP, MessageEvent, describe_event, parse_message_event
from ..onebot.sender import ReplySender
from ..onebot.segments import ParsedMessage, parse_segments
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
from .reply_format import (
    build_output_rules,
    compose_memory_text,
    parse_reply,
    salvage,
)
from .store import MessageStore, memory_key
from .summarizer import SUMMARY_MAX_TOKENS, SUMMARY_TEMPERATURE, Summarizer

log = logging.getLogger(__name__)

#: 存进记忆的图片占位文本
IMAGE_PLACEHOLDER = "[图片]"

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


@dataclass
class ReplyPlan:
    """这一轮到底要发什么。"""

    texts: list[str]
    meme: str | None = None
    image_path: Path | None = None
    #: 实际调用模型的次数（含重新生成）
    calls: int = 1
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: 走到"抢救"或"兜底"时的原因，仅用于日志
    degraded: str | None = None
    #: 多条之间的间隔（毫秒）；人设级设置优先，留空则用全局
    delay_ms: int = 0

    @property
    def memory_text(self) -> str:
        """记忆里存什么：自然文本 + ``[表情：x]``，**不存 JSON**。"""
        return compose_memory_text(self.texts, self.meme)


def _strip_image_placeholders(text: str) -> str:
    """去掉文本里的 ``[图片]`` 占位。

    多模态请求里图片会以真身出现，文本里再留一条 ``[图片]`` 会让模型以为
    "这里有两张图"，甚至去描述那个并不存在的占位符。
    """
    cleaned = str(text or "").replace(IMAGE_PLACEHOLDER, " ")
    return " ".join(cleaned.split())


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


def _replace_user_content(
    messages: list[dict[str, Any]], text: str
) -> list[dict[str, Any]]:
    """把最后一条 user 消息的多模态内容换回纯文本（视觉降级用）。"""
    result = [dict(message) for message in messages]
    for message in reversed(result):
        if message.get("role") == "user":
            message["content"] = text or IMAGE_PLACEHOLDER
            break
    return result


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
    #: 因为模型不支持视觉而降级成纯文本的次数（M5）
    vision_fallbacks: int = 0
    #: 命中敏感词被拦下的条数（M6 风控）
    blocked: int = 0
    #: 因为超出速率上限被拦下的条数（M6 风控）
    rate_limited: int = 0
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
            "vision_fallbacks": self.vision_fallbacks,
            "blocked": self.blocked,
            "rate_limited": self.rate_limited,
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

        self.summarizer = Summarizer(
            settings,
            self.store,
            complete=self._summarize_call,
            logbus=logbus,
        )
        self._images = ImagePreparer()
        self._rate_limiter: RateLimiter | None = None

        self._paused = settings.bot_paused
        self._running = False
        self._lock = threading.RLock()

        self._llm_cache: tuple[tuple[str, str, str], OpenAIChatClient] | None = None
        self._vision_cache: tuple[tuple[Any, ...], OpenAIChatClient] | None = None
        self._summary_cache: tuple[tuple[str, str, str], OpenAIChatClient] | None = None
        self._ob_cache: tuple[tuple[str, str], OneBotClient] | None = None

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

    # ================================================================== 暂停
    @property
    def paused(self) -> bool:
        return self._paused

    def set_paused(self, paused: bool) -> bool:
        with self._lock:
            self._paused = bool(paused)
        self.settings.bot_paused = self._paused
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

        if not self._is_triggered(event, parsed):
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
        self.stats.handled += 1
        self._log(f"收到：{event.describe()} → {parsed.text[:60]!r}")
        self._on_change()

        accepted = self.queue.submit(session_id, lambda: self._handle(event, parsed))
        if not accepted:
            self._log(f"{session_id} 待处理消息过多，已丢弃一条", level="warn")

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

    def _is_triggered(self, event: MessageEvent, parsed: ParsedMessage) -> bool:
        """群聊必须 @ 机器人；私聊一律触发。"""
        if not event.is_group:
            return True
        self_id = event.self_id or self.settings.qq_account or ""
        return parsed.mentions_bot or (bool(self_id) and self_id in parsed.ats)

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
        # 记忆里始终只留 [图片] 占位，base64 绝不入库。
        prepared: list[PreparedImage] = []
        if parsed.has_image:
            prepared = self._prepare_images(parsed)

        if prepared:
            # 多模态：文本里那条 [图片] 占位要去掉，否则模型会看到"占位 + 真图"
            visible = _strip_image_placeholders(parsed.text) or "请看这张图片。"
            visible = self._label_message(event, visible)
            user_content: str | list[dict[str, Any]] = build_user_content(visible, prepared)
        else:
            user_content = memory_text

        rounds = int(self.settings.get("memory_rounds") or 20)

        # 长期记忆摘要作为独立 system 块插入，**不占轮次**（需求规格 §6.3）
        summary_text = self.store.summary_text(key)
        extra_system = [f"{SUMMARY_BLOCK_LABEL} {summary_text}"] if summary_text else []

        # 随机分段 / 表情包：**骰子必须在组装提示词之前掷**。
        # 没掷中的那一项，它的规则块根本不进提示词 —— 模型不知道有这个能力，
        # 自然就不会用。这比在提示词里写"请你不要每次都这样"可靠一百倍。
        allow_split, allow_meme, emotions, delay_ms = self._roll_expression(persona)
        rules = build_output_rules(
            split=allow_split,
            max_messages=self.settings.split_max_messages,
            meme=allow_meme,
            emotions=emotions,
            split_prompt=str(persona.get("split_prompt") or ""),
            meme_prompt=str(persona.get("meme_prompt") or ""),
        )
        if rules:
            extra_system.append(rules)

        # 先取历史，再调模型；确认成功后才把这一轮写进记忆
        history = self.store.recent_rounds(key, rounds)
        system_prompt = self.store.persona_prompt(persona_uid)
        history = self._fit_budget(history, system_prompt, extra_system, user_content)
        messages = build_chat_messages(
            system_prompt=system_prompt,
            history=history,
            user_content=user_content,
            extra_system=extra_system,
        )

        started = time.monotonic()
        plan = self._plan_reply(
            persona,
            messages,
            prepared,
            memory_text,
            allow_split=allow_split,
            allow_meme=allow_meme,
            emotions=emotions,
            delay_ms=delay_ms,
        )
        elapsed = time.monotonic() - started

        self.db.bump_stats(
            prompt_tokens=plan.prompt_tokens,
            completion_tokens=plan.completion_tokens,
            calls=plan.calls,
        )
        self.db.touch_session(event.session_id)

        sent = self._sender().send_messages(
            event,
            plan.texts,
            image=plan.image_path,
            delay_ms=plan.delay_ms if len(plan.texts) > 1 else 0,
        )
        if sent == 0:
            self._log("模型返回空回复，未发送", level="warn")
            return

        # 发送成功后才落库，保证记忆里不会出现"有问无答"的空洞。
        # 注意存的是 memory_text（只含 [图片] 占位），**绝不能把 base64 写进记忆**；
        # 机器人自己说的话存 plan.memory_text（自然文本 + [表情：x]），**不存 JSON**。
        self.store.append(
            key, "user", memory_text, has_image=parsed.has_image,
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

    def _effective(self, persona: dict[str, Any] | None, key: str, fallback: Any) -> Any:
        """读人设级数值；**留空（NULL / 空串）就跟随全局设置**。

        注意不能把"留空"和"0"混为一谈：概率填 0 是"永不触发"，
        留空是"用全局的"。所以空值存进库时是 NULL 而不是 0。
        """
        value = (persona or {}).get(key)
        if value is None or value == "":
            return fallback
        return value

    def _roll_expression(
        self, persona: dict[str, Any] | None
    ) -> tuple[bool, bool, list[str], int]:
        """掷骰子决定这一轮允不允许分段 / 配表情。

        **随机的关键在这里，不在提示词里。** 只要把规则发给模型，它几乎每次都会
        用；没掷中时规则根本不进提示词，模型不知道有这个能力，自然就不用。

        分段和表情**各自独立掷**：这样才能出现"只发一条但配个表情"这种自然行为。

        Returns:
            ``(允许分段, 允许表情, 可用情绪, 多段连发间隔毫秒)``
        """
        split_on = bool(persona.get("split_enabled")) if persona else False
        meme_on = bool(persona.get("meme_enabled")) if persona else False

        emotions: list[str] = []
        if meme_on:
            emotions = self._meme_library().emotions()
            if not emotions:
                self._log(
                    "人设启用了表情包，但表情目录里没有可用图片，本轮不配表情"
                    f"（{self._meme_library().describe()}）",
                    level="warn",
                )
                meme_on = False

        split_chance = float(self._effective(persona, "split_chance", self.settings.split_chance))
        meme_chance = float(self._effective(persona, "meme_chance", self.settings.meme_chance))
        delay_ms = int(self._effective(persona, "split_delay_ms", self.settings.split_delay_ms))

        allow_split = split_on and random.random() < split_chance
        allow_meme = meme_on and random.random() < meme_chance
        if allow_split:
            self._log("本轮允许分段回复", level="debug")
        if allow_meme:
            self._log("本轮允许配表情", level="debug")
        return allow_split, allow_meme, emotions, delay_ms

    def _plan_reply(
        self,
        persona: dict[str, Any] | None,
        messages: list[dict[str, Any]],
        prepared: list[PreparedImage],
        memory_text: str,
        *,
        allow_split: bool,
        allow_meme: bool,
        emotions: list[str],
        delay_ms: int = 0,
    ) -> ReplyPlan:
        """调用模型 → 解析 → 重新生成 → 抢救 → 兜底，产出一份"要发什么"的计划。"""
        spec = {
            # 提示词里要求了 JSON 才按 JSON 校验；两个都关时模型就该输出普通文本
            "expect_json": allow_split or allow_meme,
            "allow_split": allow_split,
            "allow_meme": allow_meme,
            "max_messages": self.settings.split_max_messages,
            "emotions": emotions,
        }
        result = self._complete_with_vision_fallback(persona, messages, prepared, memory_text)
        parsed = parse_reply(result.text, **spec)
        plan = ReplyPlan(
            texts=[],
            calls=1,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            delay_ms=delay_ms,
        )
        if parsed.ok:
            plan.texts = parsed.messages
            plan.meme = parsed.meme
            self._attach_meme(plan)
            return plan

        # ① 重新生成：**把"错在哪"告诉它**，否则它很可能原样再错一遍
        attempts = self.settings.reply_retry_max
        for index in range(attempts):
            self._log(
                f"输出不是合法 JSON（{parsed.error}），重新生成（{index + 1}/{attempts}）",
                level="warn",
            )
            retry_messages = [
                *messages,
                {"role": "assistant", "content": parsed.raw[:600]},
                {"role": "user", "content": RETRY_INSTRUCTION.format(error=parsed.error)},
            ]
            try:
                follow = self._complete_with_vision_fallback(
                    persona, retry_messages, prepared, memory_text
                )
            except LLMError as exc:
                self._log(f"重新生成失败，不再重试：{exc}", level="warn")
                break
            plan.calls += 1
            plan.prompt_tokens += follow.prompt_tokens
            plan.completion_tokens += follow.completion_tokens
            parsed = parse_reply(follow.text, **spec)
            if parsed.ok:
                plan.texts = parsed.messages
                plan.meme = parsed.meme
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
        self._log(f"{plan.degraded}，已发送兜底文案", level="error")
        return plan

    def _attach_meme(self, plan: ReplyPlan) -> None:
        """按情绪名挑一张表情图；挑不到就静默不配（绝不能因此报错）。"""
        if not plan.meme:
            return
        item = self._meme_library().pick(plan.meme)
        if item is None:
            self._log(f"表情「{plan.meme}」在目录里找不到，本轮不配图", level="warn")
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

    def _fit_budget(
        self,
        history: list[tuple[str, str]],
        system_prompt: str,
        extra_system: list[str],
        user_content: str | list[dict[str, Any]],
    ) -> list[tuple[str, str]]:
        """超出上下文预算时，**从最早的原文开始丢弃，长期记忆摘要始终保留**。

        需求规格 §6.3 的硬规则：宁可丢掉最早的寒暄，也不能丢掉被压缩过的
        长期信息 —— 那部分内容一旦丢了就再也找不回来（原文已经被标记为已压缩）。
        """
        budget = self._context_budget()
        fixed = len(system_prompt or "") + sum(len(block) for block in extra_system)
        if isinstance(user_content, str):
            fixed += len(user_content)
        elif isinstance(user_content, list):
            for part in user_content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "image_url":
                    # 图片按 base64 的体积折算成本，否则一张大图会把历史全挤掉
                    fixed += len(str((part.get("image_url") or {}).get("url") or ""))
                else:
                    fixed += len(str(part.get("text") or ""))

        kept: list[tuple[str, str]] = []
        used = fixed
        for role, content in reversed(history):
            cost = len(str(content or "")) + 8
            if kept and used + cost > budget:
                break
            used += cost
            kept.append((role, content))
        kept.reverse()

        dropped = len(history) - len(kept)
        if dropped:
            self._log(
                f"上下文超预算（{used}/{budget} 字符），已丢弃最早的 {dropped} 条原文；"
                "长期记忆摘要保留",
                level="debug",
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

        signature = (base_url, api_key, model, str(max_tokens), str(temperature))
        cached = self._llm_cache
        if cached is not None and cached[0] == signature:
            return cached[1]
        client = OpenAIChatClient(
            base_url,
            api_key,
            model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        self._llm_cache = (signature, client)
        return client

    # ------------------------------------------------------------------ 图片
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
    ) -> ChatResult:
        """带图调用；模型明确表示不支持图片时，降级成纯文本再试一次。

        需求规格 §6.5：「模型不支持视觉时降级」。降级而不是直接报错，是因为
        用户至少还想知道"他发了什么文字"；而且**明确告诉用户去配视觉模型**，
        否则他会一直以为图片被看懂了。
        """
        if not prepared:
            return self._llm(persona).complete(messages)

        vision_model = str(self.settings.get("vision_model") or "").strip()
        client = self._llm_vision(persona) if vision_model else self._llm(persona)

        try:
            result = client.complete(messages)
        except LLMError as exc:
            if not looks_like_vision_unsupported(str(exc)):
                raise
            self.stats.vision_fallbacks += 1
            self._log(
                "当前模型不支持图片，已降级为纯文本。"
                "想让机器人看懂图片，请在「模型配置 → 视觉模型」里填一个支持视觉的模型"
                f"（原始错误：{exc}）",
                level="warn",
            )
            return self._llm(persona).complete(_replace_user_content(messages, memory_text))

        self.stats.images += 1
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
            base_url, api_key, model, max_tokens=max_tokens, temperature=temperature
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
        max_tokens = min(configured, SUMMARY_MAX_TOKENS) if configured > 0 else SUMMARY_MAX_TOKENS

        client = OpenAIChatClient(
            base_url,
            api_key,
            model,
            max_tokens=max_tokens,
            temperature=SUMMARY_TEMPERATURE,
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
