"""合并式记忆摘要（需求规格 v1.1 §6.3，算法已冻结）。

.. code-block:: text

    每完成一次成功问答（写入 user + assistant 两条）后：
        rounds = 该 memory_key 下「未压缩」的完整轮数

        if 自动总结开启 and rounds >= W:
            material = [老总结(若有)] + [最早的 W 轮原文]
            new = LLM(总结模型, material, "压缩到 300 字以内，保留事实与用户偏好")

            if new 失败:
                标记 pending；【不删除任何原文】；下一轮重试
                return

            事务 {
                summaries[key] ← new     # 老总结块被顶掉，信息已合并在 new 内
                这 W 轮原文标记 summarized = 1
            }

**递归性质**：``新总结 = 总结(老总结 + 新 W 轮)``。老总结被替换，但它的信息
已经并进新总结里 —— 记忆视野因此**无限延伸**，代价是越久远的内容衰减越厉害。

**最要紧的一条**：压缩失败时**一条原文都不许删**。宁可让上下文暂时变长，
也不能因为一次网络抖动就把用户的记忆弄丢。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from ..llm.client import ChatResult, LLMError
from ..napcat.logbus import LogBus
from ..settings import Settings
from .store import MessageStore

log = logging.getLogger(__name__)

#: 摘要字数上限（需求规格 §6.2）：提示词约束 + 超长截断兜底
SUMMARY_MAX_CHARS = 300

#: 交给总结模型的系统提示词
SUMMARY_SYSTEM_PROMPT = (
    "你是对话记忆压缩器。把给你的对话材料压缩成一段不超过 "
    f"{SUMMARY_MAX_CHARS} 字的第三方摘要。\n"
    "要求：\n"
    "1. 用第三人称陈述事实，不要写成对话，不要加评论或寒暄；\n"
    "2. 保留：人物与称呼、用户明确的偏好与要求、已确认的结论、未解决的问题、"
    "重要的时间或数字；\n"
    "3. 丢弃：寒暄、重复内容、语气词；\n"
    "4. 若材料里已有「此前摘要」，把它的信息一并合并进来，不要丢弃其中仍然有效的内容；\n"
    f"5. 直接输出摘要正文，不要任何前缀、标题或解释，总长不超过 {SUMMARY_MAX_CHARS} 字。"
)

#: 摘要调用的温度：要稳定、可复现，不要发挥
SUMMARY_TEMPERATURE = 0.3
#: 摘要输出的 token 上限（300 汉字 ≈ 450 token，留足余量）
SUMMARY_MAX_TOKENS = 800
#: 交给总结模型的材料长度上限（字符）。
#:
#: 必须有：W 可以设到 200，而单条消息最长可以到 5000 字 —— 200 轮 × 2 条 × 5000 字
#: 就是两百万字符，直接超出任何模型的上下文，结果是**每次都 400、一条都压不成**，
#: 原文还会一直堆着。宁可一次少压几轮，也不能让压缩彻底卡死。
SUMMARY_MATERIAL_MAX_CHARS = 12000


@dataclass
class SummaryOutcome:
    """一次压缩尝试的结果（便于日志与统计）。"""

    #: disabled | not_enough | ok | empty | failed
    status: str
    rounds: int = 0
    chars: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str | None = None

    @property
    def did_call_model(self) -> bool:
        return self.status in ("ok", "empty", "failed")


def build_material(old_summary: str, rows: list[dict[str, Any]]) -> str:
    """把「老总结 + 待压缩原文」拼成一段给总结模型的材料。"""
    lines: list[str] = []
    if old_summary.strip():
        lines.append("【此前摘要】")
        lines.append(old_summary.strip())
        lines.append("")
    lines.append("【新增对话】")
    for row in rows:
        role = "用户" if row.get("role") == "user" else "机器人"
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        # 用户消息可能是「昵称 / 时间 / 消息」三行块（见 pipeline.build_labeled_message），
        # 压成一行才不会把材料的"每行一条"结构打乱。
        content = " ".join(content.split())
        if len(content) > 2000:
            content = content[:2000] + "…"
        lines.append(f"{role}：{content}")
    return "\n".join(lines)


def truncate_summary(text: str, limit: int = SUMMARY_MAX_CHARS) -> str:
    """超长截断兜底。模型偶尔不听话，不能让它把摘要写成一本书。"""
    clean = " ".join(str(text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip() + "…"


def fit_material(
    old_summary: str,
    rows: list[dict[str, Any]],
    limit: int = SUMMARY_MATERIAL_MAX_CHARS,
) -> list[dict[str, Any]]:
    """从最早的原文里挑出能塞进模型上下文的那部分。

    **只挑完整的轮**（user + assistant 成对）：把一轮拆开喂给总结模型，它会
    把"问"当成"答"，摘要就串味了。

    至少保留一轮 —— 哪怕单条消息本身就超长，也要让压缩有进展，否则那些内容
    会永远卡在"未压缩"状态，而 W 门槛又一直被满足，每次登录都白试一遍。
    """
    if not rows:
        return []
    budget = max(1, int(limit))
    used = len(str(old_summary or "")) + 200  # 提示词与标签的余量

    kept: list[dict[str, Any]] = []
    for row in rows:
        cost = len(str(row.get("content") or "")) + 8
        if kept and used + cost > budget:
            break
        used += cost
        kept.append(row)

    if len(kept) % 2 == 1:  # 不拆轮
        kept.pop()
    return kept or rows[:2]


class Summarizer:
    """检查并执行合并式压缩。

    模型调用通过 ``complete`` 注入，方便测试与替换（例如换成假后端）。
    """

    def __init__(
        self,
        settings: Settings,
        store: MessageStore,
        *,
        complete: Callable[[list[dict[str, Any]]], ChatResult],
        logbus: LogBus | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self._complete = complete
        self._logbus = logbus

    # ------------------------------------------------------------------ 属性
    @property
    def enabled(self) -> bool:
        """自动总结开关（需求规格 §6.2，默认开启）。"""
        return bool(self.settings.get("auto_summary", True))

    @property
    def window(self) -> int:
        """记忆轮数 W，同时也是压缩触发轮数 S（§6.2：S ≡ W，无独立设置项）。"""
        try:
            rounds = int(self.settings.get("memory_rounds") or 20)
        except (TypeError, ValueError):
            rounds = 20
        return max(1, rounds)

    # ------------------------------------------------------------------ 主流程
    def maybe_summarize(self, key: str, *, force: bool = False) -> SummaryOutcome:
        """按需压缩一个记忆键。**绝不抛异常**——它跑在回复之后，不能影响对话。

        Args:
            force: 用户手动点「立即压缩」时为 ``True``。它会绕过"自动总结"开关 ——
                那个开关管的是**要不要自动压**，不是"能不能压"。
                （但它仍然受 W 轮门槛约束：没有满一轮可压的东西时硬压只会把
                还很新鲜、模型正需要逐字看到的对话提前揉成一团。）
        """
        if not self.enabled and not force:
            return SummaryOutcome(status="disabled")

        window = self.window
        rounds = self.store.unsummarized_rounds(key)
        if rounds < window:
            return SummaryOutcome(status="not_enough", rounds=rounds)

        rows = self.store.oldest_unsummarized(key, window)
        if not rows:
            return SummaryOutcome(status="not_enough", rounds=rounds)

        old_summary = self.store.summary_text(key)
        # 按材料上限挑出真正要压的部分，避免材料超长导致每次都 400
        rows = fit_material(old_summary, rows)
        if not rows:
            return SummaryOutcome(status="not_enough", rounds=rounds)
        consumed = len(rows) // 2

        material = build_material(old_summary, rows)
        messages = [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": material},
        ]

        try:
            result = self._complete(messages)
        except LLMError as exc:
            # 失败：标记 pending，**原文一条不动**，下一轮重试
            self.store.set_summary_pending(key, True)
            self._log(f"记忆压缩失败，已标记待重试（原文保留）：{exc}", level="warn")
            return SummaryOutcome(status="failed", rounds=rounds, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - 兜底，绝不让它冒泡出去
            self.store.set_summary_pending(key, True)
            log.exception("记忆压缩时发生未预期异常")
            self._log(f"记忆压缩异常，已标记待重试（原文保留）：{exc}", level="error")
            return SummaryOutcome(status="failed", rounds=rounds, error=str(exc))

        text = truncate_summary(result.text)
        if not text:
            self.store.set_summary_pending(key, True)
            self._log("记忆压缩返回空内容，已标记待重试（原文保留）", level="warn")
            return SummaryOutcome(
                status="empty",
                rounds=rounds,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
            )

        covered = [int(row["id"]) for row in rows]
        self.store.apply_summary(key, text, covered)

        self._bump_counter()
        self._log(
            f"记忆已压缩：合并 {consumed} 轮 → {len(text)} 字"
            f"（{result.prompt_tokens}+{result.completion_tokens} tokens）"
        )
        return SummaryOutcome(
            status="ok",
            rounds=consumed,
            chars=len(text),
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )

    # ------------------------------------------------------------------ 辅助
    def _bump_counter(self) -> None:
        """累计"已压缩次数"，供记忆面板的容量提示展示。"""
        try:
            current = int(self.settings.get("summary_runs") or 0)
        except (TypeError, ValueError):
            current = 0
        self.settings.summary_runs = current + 1

    def _log(self, message: str, *, level: str = "info") -> None:
        if self._logbus is not None:
            from ..core.models import LogSource

            self._logbus.add(message, level=level, source=LogSource.QLARK.value)


__all__ = [
    "SUMMARY_MATERIAL_MAX_CHARS",
    "SUMMARY_MAX_CHARS",
    "SUMMARY_MAX_TOKENS",
    "SUMMARY_SYSTEM_PROMPT",
    "SUMMARY_TEMPERATURE",
    "Summarizer",
    "SummaryOutcome",
    "build_material",
    "fit_material",
    "truncate_summary",
]
