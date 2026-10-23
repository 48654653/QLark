"""历史里的图片：以**真身**回到上下文，而不是 ``[图片]`` 占位符。

v2 的做法：
  · 收到的图片落盘到 ``<用户数据目录>/media/``，数据库里只记**文件名**
  · 组装上下文时，最近 ``media_history_rounds``（默认 2）轮的图片读回来，
    作为真正的 ``image_url`` 段放进历史
  · 更早的轮次退化成纯文本，占位符被去掉，不留"这里曾经有图"的空壳

依据：实测 token ≈ 像素数 ÷ 1024 —— 512px 只有 258 token，
而 system prompt 就占 800+，所以回放两轮图片在成本上完全划得来。
"""

from __future__ import annotations

import json

import pytest

from conftest import FakeOpenAI, wait_for

from test_m2_integration import _inject, _reply_texts, _reset, ready  # noqa: F401
from test_m5_integration import IMAGE_SEGMENTS, _send_image, vision_ready  # noqa: F401


def _user_content(payload: dict):
    return payload["messages"][-1]["content"]


def _has_image(payload: dict) -> bool:
    for message in payload["messages"]:
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
    return False


def test_image_is_saved_to_disk_with_a_name(ready, fake_openai: FakeOpenAI) -> None:
    """收到的图要落盘，数据库里只存文件名（base64 绝不入库）。"""
    _send_image(ready)

    rows = ready.db.query("SELECT media, has_image FROM messages WHERE role = 'user'")
    assert rows, "没有写入记忆"
    row = rows[-1]
    assert int(row["has_image"]) == 1
    names = json.loads(row["media"] or "[]")
    assert len(names) == 1, f"应该记一个文件名：{row['media']!r}"

    folder = ready.bot.pipeline._media_dir()
    saved = folder / names[0]
    assert saved.is_file(), "图片没有落盘"
    assert saved.stat().st_size > 0
    # 数据库里只允许出现文件名，不允许出现 base64
    assert "base64" not in str(row["media"])
    assert len(str(row["media"])) < 200


def test_history_replays_the_real_image(ready, fake_openai: FakeOpenAI) -> None:
    """下一轮请求里，历史中的那条图片应当是 image_url 段。"""
    _send_image(ready, message_id=901)

    fake_openai.reset()
    _reset(ready.home)
    _inject(
        ready.home,
        message_type="private",
        user_id="20000002",
        message_id=902,
        text="刚才那张图好看吗",
    )
    assert wait_for(lambda: _reply_texts(ready.home), timeout=60)

    payload = fake_openai.requests()[-1]
    key = "private:20000002:p1"
    history = ready.bot.pipeline._history_for_context(key, 50)
    kinds = [
        [part.get("type") for part in content] if isinstance(content, list) else "text"
        for _, content in history
    ]
    assert _has_image(payload), f"历史里没有把真实图片放回去；组装结果={kinds}"


def test_old_images_fall_back_to_text(ready, fake_openai: FakeOpenAI) -> None:
    """``media_history_rounds=0`` 时历史不带图 —— 更早的轮次就是纯文本。"""
    ready.settings.update(media_history_rounds=0)
    _send_image(ready, message_id=903)

    fake_openai.reset()
    _reset(ready.home)
    _inject(
        ready.home,
        message_type="private",
        user_id="20000002",
        message_id=904,
        text="还在吗",
    )
    assert wait_for(lambda: _reply_texts(ready.home), timeout=60)

    assert not _has_image(fake_openai.requests()[-1]), "回溯轮数为 0 却带了图"


def test_placeholder_is_gone_from_replayed_text(ready, fake_openai: FakeOpenAI) -> None:
    """带上真图之后，文本里不该再留 ``[图片]``。"""
    _send_image(ready, message_id=905)

    fake_openai.reset()
    _reset(ready.home)
    _inject(
        ready.home,
        message_type="private",
        user_id="20000002",
        message_id=906,
        text="嗯",
    )
    assert wait_for(lambda: _reply_texts(ready.home), timeout=60)

    payload = fake_openai.requests()[-1]
    for message in payload["messages"]:
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    # 这条消息的文本段里不该有占位符
                    for other in content:
                        if isinstance(other, dict) and other.get("type") == "text":
                            assert "[图片]" not in str(other.get("text") or "")


def test_missing_media_file_does_not_break_the_reply(ready, fake_openai: FakeOpenAI) -> None:
    """图片文件被人删了也不能把回复搞崩 —— 降级成纯文本。"""
    _send_image(ready, message_id=907)

    folder = ready.bot.pipeline._media_dir()
    for stale in folder.glob("*.jpg"):
        stale.unlink()

    fake_openai.reset()
    _reset(ready.home)
    _inject(
        ready.home,
        message_type="private",
        user_id="20000002",
        message_id=908,
        text="还看得见吗",
    )
    assert wait_for(lambda: _reply_texts(ready.home), timeout=60), "回复不该失败"
    assert not _has_image(fake_openai.requests()[-1])


def test_media_history_rounds_default_is_two(ready) -> None:
    assert int(ready.settings.get("media_history_rounds")) == 2


def test_image_max_side_default_is_512(ready) -> None:
    assert int(ready.settings.get("image_max_side")) == 512
    assert ready.bot.pipeline._images.max_side == 512
