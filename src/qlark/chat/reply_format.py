"""分段回复与表情包回复：输出规则提示词 + 模型输出的解析与抢救。

**输出格式**（只在对应功能开启时才要求模型这么写）：

.. code-block:: json

    {"messages": ["快到中午了", "你不会刚睡醒吧？"], "meme": "发怒"}

**为什么解析器永远开着**：不管功能开没开，收到回复后都会跑一遍解析。万一模型
哪次"习惯性"输出了 JSON，也能被拆成普通文本再发出去、再落库 —— 这样
JSON 泄漏到聊天界面和记忆里在结构上就不可能发生。

**解析的兜底顺序**（由调用方 :mod:`qlark.chat.pipeline` 编排）：

1. 直接 ``json.loads``
2. 修一修再试（去掉 ``` 代码块围栏、去掉尾随逗号、抠出第一个 ``{...}``）
3. 重新生成一次（把上次错在哪告诉模型）
4. 抢救：把所有引号里的短句抠出来当消息
5. 都不行 → 兜底文案
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

log = logging.getLogger(__name__)

#: 【输出规则】块的标题
OUTPUT_RULES_TITLE = "【输出规则】"

#: 单条消息的建议长度上限（提示词里用）
SUGGESTED_MESSAGE_CHARS = 40

#: 内置的「多段对话」规则。人设里填了自定义提示词就整段替换掉它。
#:
#: 可用占位符：``{max_messages}``（条数上限）、``{chars}``（单条字数建议）
SPLIT_RULE_DEFAULT = """**本轮请写成 2 条**（最多 {max_messages} 条）：像真人打字时想到什么又补一句那样，
第一条说完，第二条接着说，**两条合起来才是一个完整的意思**。
只有在这句话短到根本没法拆（比如「嗯」「好的」「在的」）时，才写成 1 条。
每条不超过 {chars} 字，不要包含换行，也不要把一句话从中间硬切成两半。"""

#: 内置的「发送表情」规则。占位符：``{emotions}``（可用情绪，顿号分隔）
MEME_RULE_DEFAULT = """**本轮请配一个表情**，从这些里选一个（只挑一个，不要写成数组）：{emotions}
要挑和你这句话情绪最贴合的那个。只有当这句话完全不适合配任何表情时，才省略这个键。"""

#: 两个功能都关时，messages 只能有一条
SINGLE_MESSAGE_RULE = "messages：必填，**只能写 1 条**，就是你要说的话。"

#: 没开表情时必须明确禁止，否则模型可能自己加一个
NO_MEME_RULE = "不要输出 meme 键。"


# --------------------------------------------------------------------------- #
# 内置提示词模板
# --------------------------------------------------------------------------- #
#: 「多段对话」的内置模板。界面上给用户挑，挑完仍可继续改。
#: 占位符：``{max_messages}``、``{chars}``
SPLIT_PRESETS: tuple[dict[str, str], ...] = (
    {
        "id": "default",
        "name": "默认 · 想到什么补一句",
        "text": SPLIT_RULE_DEFAULT,
    },
    {
        "id": "escalate",
        "name": "情绪递进 · 先反应再补真心话",
        "text": (
            "**本轮请写成 2 条**（最多 {max_messages} 条）。\n"
            "第一条先给出当下的直接反应（惊讶、吐槽、反问），要短、要冲；\n"
            "第二条再补上真实的态度或心情，可以犹豫、可以嘴硬。\n"
            "两条合起来才是一个完整的意思。每条不超过 {chars} 字，不要包含换行。"
        ),
    },
    {
        "id": "half",
        "name": "欲言又止 · 说一半停住",
        "text": (
            "**本轮请写成 2 条**（最多 {max_messages} 条）。\n"
            "第一条把话说一半就停住（说完就后悔，或者故意吊人胃口），用省略号或破折号收尾；\n"
            "第二条才补上后半句，或者干脆反悔改口。\n"
            "每条不超过 {chars} 字，不要包含换行。"
        ),
    },
    {
        "id": "punch",
        "name": "反问 + 补刀",
        "text": (
            "**本轮请写成 2 条**（最多 {max_messages} 条）。\n"
            "第一条用一个反问把话头递回去；\n"
            "第二条紧跟一句吐槽或补刀，把真正的意思说出来。\n"
            "每条不超过 {chars} 字，不要包含换行。"
        ),
    },
    {
        "id": "snap",
        "name": "先甩结论 · 再慢慢解释",
        "text": (
            "**本轮请写成 2 条**（最多 {max_messages} 条）。\n"
            "第一条极短（2~6 个字），像脱口而出的结论、拒绝或感叹；\n"
            "第二条再解释为什么。\n"
            "每条不超过 {chars} 字，不要包含换行。"
        ),
    },
)

#: 「发送表情」的内置模板。占位符：``{emotions}``
MEME_PRESETS: tuple[dict[str, str], ...] = (
    {
        "id": "default",
        "name": "默认 · 挑最贴合的",
        "text": MEME_RULE_DEFAULT,
    },
    {
        "id": "strong",
        "name": "只在情绪明显时配",
        "text": (
            "**本轮请配一个表情**，从这些里选一个：{emotions}\n"
            "先掂量这句话的情绪强度：只有明显高兴、生气、委屈、害羞、无语的时候才配；\n"
            "平淡的陈述（回答事实、说明情况）就省略这个键。"
        ),
    },
    {
        "id": "vivid",
        "name": "优先夸张的",
        "text": (
            "**本轮请配一个表情**，从这些里选一个：{emotions}\n"
            "优先挑情绪最外放、最夸张的那一个，别挑温和的；实在没有贴切的就省略这个键。"
        ),
    },
    {
        "id": "rare",
        "name": "极少配（会让实际频率更低）",
        "text": (
            "从这些里选一个：{emotions}\n"
            "**大多数时候不要配这个键。**只有在这句话非常好笑、或者你真的生气了的时候才加上。\n"
            "（注意：这会让实际配图频率明显低于设置里的概率。）"
        ),
    },
)


def preset_text(presets: Sequence[dict[str, str]], preset_id: str) -> str:
    """按 id 取模板文本；找不到返回空串（= 用第一个内置的）。"""
    wanted = str(preset_id or "").strip()
    for item in presets:
        if item.get("id") == wanted:
            return item.get("text", "")
    return ""

#: ``` 或 ```json 围栏
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")
#: 尾随逗号：{"a": 1,} / [1, 2,]
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")
#: 第一个看起来像 JSON 对象的片段
_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
#: 双引号字符串（抢救用）
_QUOTED_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')


# --------------------------------------------------------------------------- #
# 输出规则提示词
# --------------------------------------------------------------------------- #
def _fill(template: str, **values: Any) -> str:
    """给模板填占位符。

    **填不进去就原样返回**：用户可能在自己写的提示词里用了花括号（比如给模型的
    示例 JSON），`str.format` 会直接抛 ``KeyError`` / ``IndexError``。
    为了一个占位符把整轮回复搞崩是不划算的，所以这里容错。
    """
    try:
        return str(template).format(**values)
    except (KeyError, IndexError, ValueError):
        return str(template)


def build_output_rules(
    *,
    split: bool,
    max_messages: int,
    meme: bool,
    emotions: Sequence[str],
    split_prompt: str = "",
    meme_prompt: str = "",
) -> str:
    """拼出注入 system 的【输出规则】块。

    两个功能都关时返回**空串** —— 一个字都不提，模型自然输出普通文本。
    这是"关掉之后不会被带跑"的第一道保障。

    Args:
        split_prompt: 人设里自定义的「多段对话」提示词；留空用
            :data:`SPLIT_RULE_DEFAULT`。
        meme_prompt: 同上，对应 :data:`MEME_RULE_DEFAULT`。

    .. important::
       **骰子已经掷过了，这里是命令式，不是许可式。**

       踩过的坑：第一版写的是"默认只写 1 条 / 多数时候不要填"，
       结果模型老老实实照办 —— 骰子明明掷中了，它还是只发一条、不配表情，
       实际触发率接近 0。**随机该由代码负责，提示词一旦开了口子，
       频率就失控了**：要么像那样几乎不触发，要么（如果不加约束）每次都触发。
       所以这里要直接说"请写成几条 / 请配一个表情"。
    """
    if not split and not meme:
        return ""

    limit = max(1, int(max_messages))
    lines: list[str] = [OUTPUT_RULES_TITLE]
    lines.append("你必须只输出一个 JSON 对象，不要有任何其它文字，也不要用代码块包裹。")
    lines.append('格式：{"messages": ["第一条"], "meme": "表情名"}')
    lines.append("")

    # ---- ① messages ----
    if split and limit > 1:
        body = _fill(
            split_prompt or SPLIT_RULE_DEFAULT,
            max_messages=limit,
            chars=SUGGESTED_MESSAGE_CHARS,
        )
    else:
        body = SINGLE_MESSAGE_RULE
    lines.append(f"1. messages：必填，你要说的话，每条一个字符串。")
    lines.append(body)

    # ---- ② meme ----
    if meme:
        names = "、".join(str(name) for name in emotions)
        lines.append(f"2. meme：选填。" + _fill(meme_prompt or MEME_RULE_DEFAULT, emotions=names))
    else:
        lines.append(f"2. {NO_MEME_RULE}")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
@dataclass
class ParsedReply:
    """解析结果。"""

    #: 是不是"干净地解析出了 JSON"
    ok: bool
    messages: list[str] = field(default_factory=list)
    meme: str | None = None
    #: json | repaired | salvaged | plain
    mode: str = "plain"
    error: str | None = None
    raw: str = ""

    @property
    def text(self) -> str:
        return "\n".join(self.messages)


def _as_messages(value: Any, *, limit: int) -> list[str]:
    """把 ``messages`` 字段归一化成字符串列表。"""
    items: list[Any]
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    elif value is None:
        items = []
    else:
        items = [value]

    out: list[str] = []
    for item in items:
        if isinstance(item, (dict, list)):
            # 模型偶尔会写 {"text": "..."} 这种，尽量把文字抠出来
            if isinstance(item, dict):
                item = item.get("text") or item.get("content") or ""
            else:
                item = " ".join(str(x) for x in item)
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out[: max(1, int(limit))]


def _as_meme(value: Any) -> str | None:
    """把 ``meme`` 字段归一化成单个情绪名。

    只取第一个：模型有时会写 ``["angry", "happy"]``，而一轮只配一个表情。
    """
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    text = str(value or "").strip().lstrip("/").strip()
    return text or None


def _candidates(text: str) -> list[tuple[str, str]]:
    """给出若干"可能是 JSON"的候选文本，按可信度排序。"""
    raw = str(text or "").strip()
    if not raw:
        return []
    out: list[tuple[str, str]] = [("json", raw)]

    stripped = _FENCE_RE.sub("", raw).strip()
    if stripped and stripped != raw:
        out.append(("repaired", stripped))

    # 前后有寒暄时，抠出第一个 {...}
    match = _OBJECT_RE.search(stripped or raw)
    if match:
        candidate = match.group(0).strip()
        if candidate not in {value for _, value in out}:
            out.append(("repaired", candidate))

    # 尾随逗号（模型的高频手误）
    fixed: list[tuple[str, str]] = []
    for mode, value in out:
        cleaned = _TRAILING_COMMA_RE.sub(r"\1", value)
        if cleaned != value:
            fixed.append(("repaired", cleaned))
    out.extend(fixed)
    return out


def parse_reply(
    text: str,
    *,
    expect_json: bool,
    allow_split: bool,
    allow_meme: bool,
    max_messages: int = 2,
    emotions: Sequence[str] = (),
) -> ParsedReply:
    """解析模型输出。**永远调用它**，与功能开关无关。

    Args:
        expect_json: 提示词里**有没有要求** JSON。两个功能都关时是 ``False`` ——
            那种情况下模型本来就该输出普通文本，把它当"格式错误"去重试/兜底
            是错的（会让每一句回复都白跑一次重试）。
        allow_split: 关掉时，即使模型给了多条也**合并成一条**发出去 ——
            用户关了分段，就不该收到多条消息。
        allow_meme: 关掉时忽略 ``meme``。
        emotions: 当前可用的情绪名，用来校验模型有没有瞎编。

    注意：**不管 expect_json 是什么，都会先试一次 JSON 解析**。万一模型自发
    输出了合法的 ``{"messages": [...]}``，也会被拆成普通文本再发、再落库 ——
    这样 JSON 泄漏到聊天界面和记忆里在结构上就不可能发生。
    """
    raw = str(text or "")
    if not raw.strip():
        return ParsedReply(ok=False, mode="plain", error="模型返回了空内容", raw=raw)

    errors: list[str] = []
    for mode, candidate in _candidates(raw):
        try:
            payload = json.loads(candidate)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if not isinstance(payload, dict):
            errors.append("顶层不是 JSON 对象")
            continue

        # 不是我们要的 JSON（比如模型回了个普通对象）→ 当作普通文本
        if "messages" not in payload:
            if expect_json:
                errors.append("缺少 messages 字段")
                continue
            break

        messages = _as_messages(payload.get("messages"), limit=max_messages)
        if not messages:
            errors.append("messages 为空")
            continue

        meme = _as_meme(payload.get("meme"))
        if meme and emotions:
            matched = _match_emotion(meme, emotions)
            if matched is None:
                log.info("模型给的表情 %r 不在可用列表里，已忽略", meme)
                meme = None
            else:
                meme = matched

        if not allow_split and len(messages) > 1:
            # 关了分段就合并成一条：用换行保留原来的断句，比硬拼自然
            messages = ["\n".join(messages)]

        return ParsedReply(
            ok=True,
            messages=messages,
            meme=meme if allow_meme else None,
            mode=mode,
            raw=raw,
        )

    # 没要求 JSON：这就是一段普通回复，正常得很
    if not expect_json:
        plain = raw.strip()
        return ParsedReply(ok=True, messages=[plain], mode="plain", raw=raw)

    detail = errors[0] if errors else "不是合法的 JSON 对象"
    return ParsedReply(ok=False, mode="plain", error=detail, raw=raw)


def _match_emotion(name: str, emotions: Sequence[str]) -> str | None:
    """把模型给的情绪名对齐到实际可用的名字（忽略大小写与首尾空白）。"""
    wanted = str(name or "").strip().lower()
    if not wanted:
        return None
    table = {str(item).strip().lower(): str(item) for item in emotions if str(item).strip()}
    if wanted in table:
        return table[wanted]
    for key, original in table.items():
        if key in wanted or wanted in key:
            return original
    return None


def salvage(text: str, *, max_messages: int = 2) -> list[str]:
    """从**坏掉的 JSON** 里抢救出可读的短句。

    走这一步的典型输入是 ``{"messages": ["a" "b"]}``（少了逗号）——
    ``json.loads`` 救不了，但引号里的内容其实是好的，抠出来照样能发。

    **只对"看得出 JSON 结构"的内容生效**：完全不含 ``{ } [ ] "`` 的文本不是
    "坏掉的 JSON"，而是模型压根没按格式来。那种情况不该抢救（硬把整段话当成
    消息发出去，会让"格式不对→重新生成→兜底"这条链路永远走不到），返回空列表，
    交给调用方走兜底文案。
    """
    raw = _FENCE_RE.sub("", str(text or "")).strip()
    if not raw:
        return []
    if not re.search(r'[{}\[\]"]', raw):
        return []

    candidates = [match.group(1) for match in _QUOTED_RE.finditer(raw)]
    # 去掉像键名的那些（messages / meme 本身不是内容）
    skip = {"messages", "meme", "text", "content"}
    phrases = [
        item.replace('\\"', '"').replace("\\n", " ").strip()
        for item in candidates
        if item.strip().lower() not in skip
    ]
    phrases = [item for item in phrases if item and not item.startswith("{")]
    if phrases:
        return phrases[: max(1, int(max_messages))]

    # 有 JSON 结构但连引号内容都没抠出来：把标点去掉，剩下的话还能用就用
    plain = re.sub(r'[{}\[\]"]', " ", raw)
    plain = re.sub(r"\b(messages|meme)\b\s*:", " ", plain)
    plain = " ".join(plain.split()).strip(" ,:")
    return [plain] if len(plain) >= 2 else []


#: 句末标点。拼接两条消息时用它判断该不该补逗号
_TAIL_PUNCT = ("。", "！", "？", "…", "~", "～", "!", "?", "，", ",", "；", ";", "：", ":")


def compose_memory_text(messages: Sequence[str]) -> str:
    """机器人说过的话怎么存进记忆：**拼成一行普通文本，不记表情**。

    .. code-block:: text

        快到中午了，你不会刚睡醒吧？

    两条规矩，都是被同一个 bug 逼出来的：

    1. **不记表情**。以前存成 ``…\\n[表情：嫌弃]``，结果没掷中表情的那些轮次里，
       模型看着历史就照着模仿，把 ``[表情：xxx]`` 当正文吐出来发给用户。
       历史里只要出现过一次某种格式，模型就会学 —— 这跟 JSON 泄漏是同一回事。
    2. **拼成一行**。多行文本同样会被模仿，而且把两条并成一句更接近"真人一口气说完"。

    代价是模型不知道自己上次配了什么表情，理论上可能连着配同一个。相比"把标注
    当正文发出去"，这个代价可以接受。
    """
    parts = [str(item).strip() for item in messages if str(item).strip()]
    if not parts:
        return ""
    out = parts[0]
    for part in parts[1:]:
        # 上一句已经有句末标点就直接接，否则补个逗号 —— 免得拼出 "。，"
        out += part if out.endswith(_TAIL_PUNCT) else "，" + part
    return out


def build_preview(
    *,
    system_prompt: str,
    rules: str,
    enabled_note: str,
    sample_message_id: str = "示例",
) -> dict[str, Any]:
    """拼出"即将发给 AI 的全部内容"的预览。

    纯函数，方便测试与复用。真正发请求时的消息顺序是：

    ``system(人设) → system(输出规则) → system(长期记忆摘要) → 历史 → user(当前消息)``
    """
    blocks: list[dict[str, str]] = [
        {
            "role": "system",
            "title": "人设提示词",
            "content": str(system_prompt or "").strip() or "（这个人设还没写提示词）",
        }
    ]
    if rules:
        blocks.append(
            {"role": "system", "title": "【输出规则】（按掷中拼出）", "content": rules}
        )
    blocks.append(
        {
            "role": "system",
            "title": "长期记忆摘要",
            "content": "（每个会话一份，由自动压缩生成；这里只做占位）",
        }
    )
    blocks.append(
        {
            "role": "user",
            "title": "用户消息（示例）",
            "content": (
                "昵称：毛糙蛋\n时间：2026-9-25 19:30\n消息：早上好啊"
                if sample_message_id
                else ""
            ),
        }
    )
    return {
        "blocks": blocks,
        "note": enabled_note,
        "text": "\n\n".join(
            f"──── {item['role']} · {item['title']} ────\n{item['content']}" for item in blocks
        ),
    }


__all__ = [
    "MEME_PRESETS",
    "MEME_RULE_DEFAULT",
    "OUTPUT_RULES_TITLE",
    "SPLIT_PRESETS",
    "SPLIT_RULE_DEFAULT",
    "SUGGESTED_MESSAGE_CHARS",
    "ParsedReply",
    "build_output_rules",
    "build_preview",
    "compose_memory_text",
    "parse_reply",
    "preset_text",
    "salvage",
]
