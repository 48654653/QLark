"""一条消息里不许出现空行。

模型偶尔会在 JSON 字符串里写 ``\\n\\n``，解码后就是真换行，发到 QQ 里变成：

    白米饭。别的免谈

    交朋友就算了

**真人不会这么聊天** —— 要么一句说完，要么分两条发。
这个 bug 是用户在实际使用里发现的，光靠提示词管不住（模型时好时坏），
所以必须在发送前压平。
"""

from __future__ import annotations

import pytest

from qlark.chat.reply_format import (
    normalize_message_text,
    parse_reply,
    salvage,
)

SPEC = dict(expect_json=True, allow_split=True, allow_meme=True, max_messages=2, emotions=[])


# --------------------------------------------------------------------------- #
# 归一化函数本身
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("白米饭。别的免谈\n\n交朋友就算了", "白米饭。别的免谈 交朋友就算了"),
        ("累了就歇会儿\n先去吃饭吧", "累了就歇会儿 先去吃饭吧"),
        ("一\n\n\n\n二", "一 二"),
        ("  前后有空白  ", "前后有空白"),
        ("制表\t符", "制表 符"),
        ("正常一句话", "正常一句话"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_collapses_to_one_line(raw, expected) -> None:
    assert normalize_message_text(raw) == expected


def test_normalize_keeps_normal_spaces() -> None:
    """机器人本来就有空格断句的语感，别把正常空格也吃掉。"""
    assert normalize_message_text("在啊 咋了") == "在啊 咋了"


# --------------------------------------------------------------------------- #
# 解析路径
# --------------------------------------------------------------------------- #
def test_blank_line_is_collapsed() -> None:
    # JSON 源码里是 \\n 转义，json.loads 之后才是真换行
    parsed = parse_reply('{"messages": ["白米饭。别的免谈\\n\\n交朋友就算了"]}', **SPEC)
    assert parsed.ok
    assert len(parsed.messages) == 1
    assert "\n" not in parsed.messages[0]
    assert parsed.messages[0] == "白米饭。别的免谈 交朋友就算了"


def test_single_newline_is_collapsed() -> None:
    parsed = parse_reply('{"messages": ["累了就歇会儿\\n先去吃饭吧"]}', **SPEC)
    assert parsed.ok
    assert parsed.messages == ["累了就歇会儿 先去吃饭吧"]


def test_multiple_messages_each_collapsed() -> None:
    parsed = parse_reply('{"messages": ["一", "二\\n\\n三"]}', **SPEC)
    assert parsed.ok
    assert parsed.messages == ["一", "二 三"]


def test_no_newline_survives_any_message() -> None:
    parsed = parse_reply(
        '{"messages": ["甲\\n\\n乙", "丙\\n丁"], "meme": "开心"}',
        **SPEC,
    )
    assert parsed.ok
    for message in parsed.messages:
        assert "\n" not in message and "\r" not in message, repr(message)


# --------------------------------------------------------------------------- #
# 抢救路径（坏 JSON）
# --------------------------------------------------------------------------- #
def test_salvage_collapses_literal_escapes() -> None:
    """坏 JSON 里的 ``\\n`` 还是两个字符（反斜杠+n），也要压平。"""
    phrases = salvage('{"messages": ["白米饭。别的免谈\\n\\n交朋友就算了"]}')
    assert phrases == ["白米饭。别的免谈 交朋友就算了"]


def test_salvage_plain_text_has_no_newline() -> None:
    """有 JSON 结构但抠不出引号内容时，走"去标点"那条路，同样不能带换行。"""
    # 注意必须有 {} 或 [] 或引号，否则 salvage 会判定"根本不是 JSON"直接放弃
    # —— 那是之前修的"抢救太宽松、把散文也当成功"的规则
    phrases = salvage("{ messages: 甲\n\n乙 }")
    assert phrases == ["甲 乙"], phrases


# --------------------------------------------------------------------------- #
# 提示词也要说清楚
# --------------------------------------------------------------------------- #
def test_rubric_forbids_newlines() -> None:
    """代码是兜底，提示词里也得说 —— 双管齐下才压得住。"""
    from qlark.chat.reply_format import REPLY_RUBRIC

    assert "换行" in REPLY_RUBRIC
