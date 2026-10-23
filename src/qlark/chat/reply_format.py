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

#: 掷中时要额外补的一句极短提醒，**放两处**（末尾 system + 用户消息文本末尾）。
#:
#: 这是实测出来的做法。原来【输出规则】排在消息开头，离生成点隔了二十来条
#: 历史消息，而那些历史里助手自己的回复全是散文 —— 模型就继续写散文，
#: JSON 解析失败率高达 26%，加多少措辞、怎么降温都没用。
#: 把同一句话挪到生成点附近：单放末尾 system 约 83%，**两处都放 30/30 = 100%**。
#:
#: 注意措辞要**短而平**。"必须/任何...都不允许"这种强硬写法实测反而更差（70~80%）。
FORMAT_REMINDER = "（提醒：只输出那个 JSON 对象，不要写别的话。）"


#: 「回复判断表」：教模型**按情境**判断该回多长、该不该认真。
#:
#: 为什么不是掷骰子：长度和语气**完全取决于对方说了什么** ——
#: 对着一句「求你了，不@他我就会死」回一个「嗯」，比说错话更伤人。
#: 这是模型最擅长、代码最不擅长的事，所以交回模型判断。
#:
#: 但也不是把话说完就结束 —— 真人聊天真正的毛病不是"太长"，
#: 而是**每条都塞满人设梗**（回答+傲娇+米饭+哼，一句四个包袱）。
#: 所以这里同时约束"长度"和"信息密度"。
#:
#: 放在**消息末尾**：实测位置决定了指令的效果，离生成点越近越有效。
REPLY_RUBRIC = """【这条消息该怎么回】
先判断对方这句属于哪种：
· 打招呼 / 随口一句 → 一两个词就够，不用把话说圆
· 问事实 → 直接答，别绕，别加梗
· 在情绪里（生气、难过、求助、撒娇）→ 认真回。长度跟着对方的情绪走，**不许敷衍**
· 在逗你 / 开玩笑 → 可以接梗，但**只接一个**，不要一句话塞三个

不管哪种，都守住这三条：
1. **一次只说一件事。** 超过三句话就是在演讲了。
2. **人设是调料，不是主菜。** 口癖、动作描写（尾巴、耳朵、眨眼）、米饭梗，
   十句里偶尔露一次就够，别每句都补一遍。
3. **一条消息里不要换行。** 真人聊天不会在一句话中间敲回车；
   想说的东西多，就分两条发（能不能分由系统决定）。"""


def append_reminder(content: Any, reminder: str = FORMAT_REMINDER) -> Any:
    """把提醒接到用户消息**文本的末尾**；多模态时只动 text 段，图片段不碰。"""
    text = str(reminder or "").strip()
    if not text:
        return content
    if isinstance(content, str):
        return f"{content}\n\n{text}" if content.strip() else text
    if isinstance(content, list):
        parts = [dict(part) for part in content]
        for part in parts:
            if part.get("type") == "text":
                existing = str(part.get("text") or "").rstrip()
                part["text"] = f"{existing}\n\n{text}" if existing else text
                return parts
        # 多模态里没有 text 段（只发了图）→ 补一段
        parts.insert(0, {"type": "text", "text": text})
        return parts
    return content


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
            "**大多数时候不要配这个键。**只有在这句话非常好笑、或者你真的生气了的时候才加上。"
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
    emotions: Sequence[str],
    max_messages: int = 3,
    voice: bool = False,
    voice_max_chars: int = 20,
    voice_emotions: Sequence[str] = ("开心", "嫌弃"),
) -> str:
    """拼出注入 system 的【输出规则】块。**永远是 JSON，永远非空。**

    v2 的重大变化：**不再有骰子。** 以前是"掷中了才把规则发给模型，命令它照做"，
    现在规则**每一轮都在**，由模型自己判断要不要配表情、要不要发语音、要不要分段。

    为什么改：
      · 骰子是上下文盲的（用户说「求你了」，骰到极短档就回一个「嗯」）
      · 更糟的是**能力不一致** —— 没掷中的轮次，模型根本不知道自己能发图，
        用户明确要求发图时它只能干巴巴回一句文字

    代价是失去了固定频率控制，所以提示词里必须给**判断标准**而不是概率：
    写"平时一条就够""看你这句话有没有合适的情绪"，而不是"请写成 2 条"。

    .. important::
       旧版是命令式（"本轮请写成 2 条"），因为那时骰子已经替模型决定过了。
       现在没有骰子，**任何命令式措辞都会导致每次都触发**。
    """
    limit = max(1, int(max_messages))
    lines: list[str] = [OUTPUT_RULES_TITLE]
    lines.append("你必须只输出一个 JSON 对象，不要有任何其它文字，也不要用代码块包裹。")
    lines.append(
        '格式：{"messages": ["第一句"], "meme": "表情名", "voice": false, "emotion": "开心"}'
    )
    lines.append("")

    # ---- ① messages（条数由模型决定）----
    lines.append("1. messages：必填，你要说的话，每条一个字符串。")
    if limit > 1:
        lines.append(
            f"   **平时一条就够。** 只有真的想分成两口气说的时候才写两条"
            f"（比如先应一声再补一句）。最多 {limit} 条。别为了分段而分段，那是刷屏。"
        )
    else:
        lines.append("   只能写 1 条。")

    # ---- ② meme（可选）----
    names = "、".join(str(name) for name in emotions)
    if names:
        lines.append(
            f"2. meme：选填。想配表情就写表情名，不想配就写 null。"
            f"只能从这些里原样挑一个：{names}。"
            f"像真人一样——有时配，有时不配，看你这句话有没有合适的情绪。"
            f"名字必须和列表里一模一样，不要自创、不要翻译。"
        )
    else:
        lines.append("2. meme：固定写 null（当前没有可用表情）。")

    # ---- ③ voice（可选）----
    if voice:
        mood = "、".join(str(name) for name in voice_emotions)
        lines.append(
            f"3. voice：填 true 表示这一句用**语音**发出去。"
            f"只在「这句话很短（{voice_max_chars} 字以内）而且用声音说出来更自然」时才填 true。"
            f"长句子不要用语音——没人愿意听 20 秒。"
            f"voice 为 true 时 messages 只能写 1 条。"
        )
        lines.append(
            f"4. emotion：voice 为 true 时必须填，决定朗读语气。"
            f"只能从这些里选：{mood}。voice 为 false 时可以省略。"
        )
    else:
        lines.append("3. voice：固定写 false。")
        lines.append("4. emotion：可以省略。")

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
    #: 模型要求这条回复用**语音**发出去（并且它给了多段时以第一段为准）
    voice: bool = False
    #: 朗读时的语气标签，例如「开心」「嫌弃」；缺省当「开心」
    emotion: str | None = None
    #: json | repaired | salvaged | plain
    mode: str = "plain"
    error: str | None = None
    raw: str = ""

    @property
    def text(self) -> str:
        return "\n".join(self.messages)


def normalize_message_text(text: Any) -> str:
    """把一条要发出去的消息压成**单行**。

    模型偶尔会在 JSON 字符串里写 ``\\n\\n``，那会变成 QQ 里的空行：

    .. code-block:: text

        白米饭。别的免谈

        交朋友就算了

    **真人不会这么聊天** —— 要么一句话说完，要么分两条发。

    合并方式用**单个空格**，因为机器人本来就有空格断句的语感
    （「在啊 咋了」「二啊 这还用问」），这样最贴近它自己的风格；
    直接粘起来会变成「白米饭。别的免谈交朋友就算了」，反而读不通。
    """
    return " ".join(str(text or "").split())


def _unescape_literals(text: str) -> str:
    """把**字面上的** ``\\n`` / ``\\r\\n`` / ``\\t`` 转成真空白。

    只在"抢救坏 JSON"这条路上需要：那时内容是从没解析成功的原文里抠出来的，
    里面的 ``\\n`` 还是两个字符（反斜杠 + n），不是真换行。
    """
    return (
        str(text)
        .replace("\\r\\n", " ")
        .replace("\\n", " ")
        .replace("\\r", " ")
        .replace("\\t", " ")
    )


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
        text = normalize_message_text(item)
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
    expect_json: bool = True,
    max_messages: int = 3,
    emotions: Sequence[str] = (),
    allow_split: bool = True,
    allow_meme: bool = True,
    allow_voice: bool = False,
    voice_max_chars: int = 20,
    voice_emotions: Sequence[str] = ("开心", "嫌弃"),
) -> ParsedReply:
    """解析模型输出。**永远调用它。**

    Args:
        expect_json: 提示词里有没有要求 JSON。v2 起**总是 True**（规则永远在场）；
            保留这个参数是为了兼容"输出规则整块关掉"的调试场景。
        allow_split: 关掉时把多条合并成一条 —— 这是**护栏**，不是骰子。
        allow_meme: 关掉时忽略 ``meme``。
        allow_voice: 这个角色允不允许发语音。
        voice_max_chars: 语音的字数上限。超过就忽略 ``voice``（发文字）——
            实测 46 字要念 21 秒，长语音没人听。
        voice_emotions: 允许的朗读语气，不认识的回落到第一个。

    语音的两个护栏（**不是骰子，是防模型越界**）：
      · 只允许 1 条消息（语音是一整段音频）
      · 字数不超过 ``voice_max_chars``
    只要有一条不满足，就忽略 ``voice`` 按文字发 —— **绝不删内容去迁就格式**。
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
                log.info("模型给的表情 %r 不在可用列表里，本轮不配图", meme)
                meme = None
            else:
                meme = matched

        if not allow_split and len(messages) > 1:
            # 关了分段就合并成一条：用换行保留原来的断句，比硬拼自然
            messages = ["\n".join(messages)]

        # ---- 语音护栏 ----
        voice = _as_bool(payload.get("voice"))
        emotion = _as_emotion(payload.get("emotion"), voice_emotions)
        if voice:
            joined = "\n".join(messages)
            if not allow_voice:
                voice = False
            elif len(messages) > 1:
                log.info("模型要求语音但给了 %d 条消息，按文字发", len(messages))
                voice = False
            elif len(joined) > voice_max_chars:
                log.info(
                    "模型要求语音但这句话有 %d 字（上限 %d），按文字发",
                    len(joined), voice_max_chars,
                )
                voice = False

        return ParsedReply(
            ok=True,
            messages=messages,
            meme=meme if allow_meme else None,
            voice=voice,
            emotion=emotion if voice else None,
            mode=mode,
            raw=raw,
        )

    # 没要求 JSON：这就是一段普通回复，正常得很
    if not expect_json:
        plain = raw.strip()
        return ParsedReply(ok=True, messages=[plain], mode="plain", raw=raw)

    detail = errors[0] if errors else "不是合法的 JSON 对象"
    return ParsedReply(ok=False, mode="plain", error=detail, raw=raw)


def _as_bool(value: Any) -> bool:
    """把模型写的 ``true`` / ``"true"`` / ``"是"`` 之类统一成布尔。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    return text in {"true", "1", "yes", "y", "是", "要", "用"}


def _as_emotion(value: Any, allowed: Sequence[str]) -> str | None:
    """校验朗读语气；不认识的回落到第一个（默认「开心」）。"""
    names = [str(name) for name in allowed] or ["开心"]
    wanted = str(value or "").strip()
    if not wanted:
        return names[0]
    for name in names:
        if wanted == name:
            return name
    for name in names:
        if name in wanted or wanted in name:
            return name
    return names[0]


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
    # 连引号里的内容也要压成单行：模型会在中间塞换行，发出去就是空行
    phrases = [
        normalize_message_text(_unescape_literals(item.replace('\\"', '"')))
        for item in candidates
        if item.strip().lower() not in skip
    ]
    phrases = [item for item in phrases if item and not item.startswith("{")]
    if phrases:
        return phrases[: max(1, int(max_messages))]

    # 有 JSON 结构但连引号内容都没抠出来：把标点去掉，剩下的话还能用就用
    plain = re.sub(r'[{}\[\]"]', " ", raw)
    plain = re.sub(r"\b(messages|meme)\b\s*:", " ", plain)
    plain = normalize_message_text(_unescape_literals(plain)).strip(" ,:")
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
    reminder: str = "",
    sample_message_id: str = "示例",
) -> dict[str, Any]:
    """拼出"即将发给 AI 的全部内容"的预览。

    纯函数，方便测试与复用。真正发请求时的消息顺序是：

    ``system(人设+规则+摘要) → 历史 → system(格式提醒) → user(当前消息+格式提醒)``

    ``reminder`` 非空时要**如实画在两处** —— 预览如果和实际发出去的不一致，
    那它比没有还糟。
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
            {"role": "system", "title": "【输出规则】（每一轮都注入）", "content": rules}
        )
    blocks.append(
        {
            "role": "system",
            "title": "长期记忆摘要",
            "content": "（每个会话一份，由自动压缩生成；这里只做占位）",
        }
    )
    if reminder:
        blocks.append(
            {
                "role": "system",
                "title": "格式提醒（紧挨用户消息，位置刻意靠后）",
                "content": reminder,
            }
        )

    user_body = (
        "昵称：毛糙蛋\n时间：2026-9-25 19:30\n消息：早上好啊" if sample_message_id else ""
    )
    if reminder and user_body:
        user_body = f"{user_body}\n\n{reminder}"
    blocks.append({"role": "user", "title": "用户消息（示例）", "content": user_body})

    return {
        "blocks": blocks,
        "note": enabled_note,
        "text": "\n\n".join(
            f"──── {item['role']} · {item['title']} ────\n{item['content']}" for item in blocks
        ),
    }


__all__ = [
    "FORMAT_REMINDER",
    "MEME_PRESETS",
    "MEME_RULE_DEFAULT",
    "OUTPUT_RULES_TITLE",
    "REPLY_RUBRIC",
    "SPLIT_PRESETS",
    "SPLIT_RULE_DEFAULT",
    "SUGGESTED_MESSAGE_CHARS",
    "ParsedReply",
    "append_reminder",
    "build_output_rules",
    "build_preview",
    "compose_memory_text",
    "normalize_message_text",
    "parse_reply",
    "preset_text",
    "salvage",
]
