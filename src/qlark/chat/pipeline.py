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
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.models import LogSource
from ..llm.client import LLMError, OpenAIChatClient
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
from .personas import PersonaStore
from .queue import SessionQueue
from .store import MessageStore, memory_key

log = logging.getLogger(__name__)

#: 存进记忆的图片占位文本
IMAGE_PLACEHOLDER = "[图片]"


@dataclass
class PipelineStats:
    handled: int = 0
    replied: int = 0
    dropped: int = 0
    failed: int = 0
    #: 走指令（未调用模型）的消息数
    commands: int = 0
    last_error: str | None = None
    last_reply_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "handled": self.handled,
            "replied": self.replied,
            "dropped": self.dropped,
            "failed": self.failed,
            "commands": self.commands,
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

        self._paused = settings.bot_paused
        self._running = False
        self._lock = threading.RLock()

        self._llm_cache: tuple[tuple[str, str, str], OpenAIChatClient] | None = None
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
        user_text = parsed.text_for_memory() or (IMAGE_PLACEHOLDER if parsed.has_image else "")
        rounds = int(self.settings.get("memory_rounds") or 20)

        # 先取历史，再调模型；确认成功后才把这一轮写进记忆
        history = self.store.recent_rounds(key, rounds)
        system_prompt = self.store.persona_prompt(persona_uid)
        messages = build_chat_messages(
            system_prompt=system_prompt, history=history, user_content=user_text
        )

        started = time.monotonic()
        result = self._llm(persona).complete(messages)
        elapsed = time.monotonic() - started

        self.db.bump_stats(
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            calls=1,
        )
        self.db.touch_session(event.session_id)

        sent = self._sender().send_reply(event, result.text)
        if sent == 0:
            self._log("模型返回空回复，未发送", level="warn")
            return

        # 发送成功后才落库，保证记忆里不会出现"有问无答"的空洞
        self.store.append(
            key, "user", user_text, has_image=parsed.has_image,
            msg_id=event.message_id, created_at=event.time or None,
        )
        self.store.append(
            key, "assistant", result.text,
            tokens=result.completion_tokens, created_at=event.time or None,
        )

        self.stats.replied += 1
        self.stats.last_reply_at = time.time()
        self.stats.last_error = None
        self.db.bump_stats(msg_in=1, msg_out=sent)

        self._log(
            f"已回复 {event.session_id}（{elapsed:.1f}s，"
            f"{result.prompt_tokens}+{result.completion_tokens} tokens，{sent} 条消息）"
        )

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

    def _sender(self) -> ReplySender:
        signature = (self.settings.onebot_base_url, self.settings.onebot_token)
        cached = self._ob_cache
        if cached is None or cached[0] != signature:
            cached = (signature, OneBotClient(signature[0], signature[1]))
            self._ob_cache = cached
        return ReplySender(
            cached[1],
            quote_group=self.settings.reply_quote_group,
            max_chars=self.settings.max_reply_chars,
        )

    # ------------------------------------------------------------------ 日志
    def _log(self, message: str, *, level: str = "info") -> None:
        self.logbus.add(message, level=level, source=LogSource.QLARK.value)


__all__ = ["IMAGE_PLACEHOLDER", "MessagePipeline", "PipelineStats"]
