"""视频理解：模型原生吃 ``video_url``，QLark 不下载、不抽帧。

实现的三个要点（也是这份测试覆盖的三件事）：

1. **解析层不再把视频扔进垃圾桶。** 以前 ``video`` 段只变成 ``[视频]`` 三个字，
   直链直接丢掉；现在会带在 ``ParsedMessage.videos`` 里，引用的视频同理。
2. **发给模型的是 ``video_url`` 段。** 不下载 mp4、不调 ffmpeg、不抽帧 ——
   服务端自己按直链拉取解码（实测 1MB 短视频约 1100 video token）。
   记忆里只存**直链字符串**，绝不存视频本体。
3. **整个上下文只保留最近一段视频。** 视频比图片贵四倍多，而且"刚才那个视频"
   指的一定是最新那段；同一轮里发了新视频时，历史里的旧视频必须让位。
"""

from __future__ import annotations

import json

import pytest

from conftest import FakeOpenAI, wait_for, wait_memory

from qlark.chat.images import build_user_content
from qlark.chat.quoted import render_segments
from qlark.onebot.segments import VideoRef, parse_segments

from test_m2_integration import _inject, _reply_texts, _reset, ready  # noqa: F401
from test_m5_integration import _content_parts, _last_request, vision_ready  # noqa: F401

KEY = "private:20000002:p1"

VIDEO_URL = "https://multimedia.nt.qq.com.cn/download?appid=1413&format=origin&rkey=abc"

JSON_REPLY = '{"messages": ["好"]}'


# --------------------------------------------------------------------------- #
# 解析 / 拼装（纯函数）
# --------------------------------------------------------------------------- #
def test_parse_video_keeps_url_and_bumps_placeholder() -> None:
    parsed = parse_segments([
        {"type": "text", "data": {"text": "看看这个"}},
        {
            "type": "video",
            "data": {"file": "v.mp4", "url": VIDEO_URL, "file_size": "1140167"},
        },
    ])

    assert parsed.has_video
    assert parsed.text == "看看这个[视频]"
    assert parsed.videos[0].url == VIDEO_URL
    assert parsed.videos[0].file == "v.mp4"
    assert parsed.videos[0].size == 1140167
    assert parsed.videos[0].to_segment() == {
        "type": "video_url",
        "video_url": {"url": VIDEO_URL},
    }


def test_parse_video_without_url_is_still_parsed() -> None:
    """没有直链时不要炸 —— 至少还知道"这里有个视频"。"""
    parsed = parse_segments([{"type": "video", "data": {"file": "v.mp4"}}])
    assert parsed.text == "[视频]"
    assert parsed.videos[0].url is None
    assert parsed.videos[0].to_segment()["video_url"]["url"] == ""


def test_quoted_video_keeps_a_ref() -> None:
    content = render_segments([
        {"type": "video", "data": {"url": VIDEO_URL, "file_size": "1140167"}},
    ])
    assert "[视频]" in content.text
    assert content.videos[0].url == VIDEO_URL
    assert content.ok


def test_build_user_content_appends_video_part() -> None:
    """``build_user_content`` 要能把文本 + 视频拼成多模态数组（图片可以没有）。"""
    content = build_user_content("这是什么", [], [VideoRef(url=VIDEO_URL)])
    assert content[0] == {"type": "text", "text": "这是什么"}
    assert content[1] == {"type": "video_url", "video_url": {"url": VIDEO_URL}}


# --------------------------------------------------------------------------- #
# 流水线（假 NapCat + 假 OpenAI）
# --------------------------------------------------------------------------- #
def _video_segments(url: str = VIDEO_URL, text: str = "看看这个视频"):
    return [
        {"type": "text", "data": {"text": text}},
        {"type": "video", "data": {"file": "v.mp4", "url": url, "file_size": "1140167"}},
    ]


def _send_video(env, *, url: str = VIDEO_URL, message_id: int = 910, text: str = "看看这个视频"):
    _reset(env.home)
    _inject(
        env.home,
        message_type="private",
        user_id="20000002",
        message_id=message_id,
        text=text,
        segments=_video_segments(url, text),
    )
    assert wait_for(lambda: _reply_texts(env.home), timeout=60), "视频消息没有回复"


def _video_parts(payload: dict) -> list[dict]:
    parts: list[dict] = []
    for message in payload.get("messages") or []:
        content = message.get("content")
        if isinstance(content, list):
            parts.extend(
                part for part in content if isinstance(part, dict) and part.get("type") == "video_url"
            )
    return parts


def _all_text(payload: dict) -> str:
    pieces: list[str] = []
    for message in payload.get("messages") or []:
        content = message.get("content")
        if isinstance(content, str):
            pieces.append(content)
        elif isinstance(content, list):
            pieces.extend(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
    return "\n".join(pieces)


@pytest.fixture
def video_ready(vision_ready, fake_openai: FakeOpenAI):
    """每次请求都回一个合法 JSON，免得"非法 JSON 重新生成"污染请求序列。"""
    fake_openai.replies(JSON_REPLY, repeat_last=True)
    return vision_ready


def test_video_message_is_sent_as_video_url(video_ready, fake_openai: FakeOpenAI) -> None:
    """直发视频：请求里必须出现 ``video_url`` 段，且直链原样带过去。"""
    fake_openai.reset()
    _send_video(video_ready)

    payload = _last_request(fake_openai)
    videos = _video_parts(payload)
    assert len(videos) == 1, f"应当只有一段视频，实际 {len(videos)}"
    assert videos[0]["video_url"]["url"] == VIDEO_URL

    # 真身附上了，正文里就不该再留 [视频] 占位符（否则模型会说自己看不到）
    assert "[视频]" not in _all_text(payload)
    assert video_ready.bot.pipeline.stats.videos >= 1


def test_memory_stores_url_not_video_body(video_ready, fake_openai: FakeOpenAI) -> None:
    """记忆里存 ``[视频]`` 占位 + 直链，绝不存视频本体。"""
    fake_openai.reset()
    _send_video(video_ready)

    store = video_ready.bot.pipeline.store
    assert wait_memory(store, KEY, min_rows=2)

    rows = store.memory_messages(KEY)
    user_row = next(row for row in rows if row["role"] == "user")
    assert "[视频]" in user_row["content"], user_row["content"][:120]
    assert user_row["video"] == VIDEO_URL
    assert len(user_row["content"]) < 300


def test_previous_video_is_replayed_for_a_follow_up(video_ready, fake_openai: FakeOpenAI) -> None:
    """紧接着追问一句"这视频讲了啥"，历史里的那段视频要能回放。"""
    fake_openai.reset()
    _send_video(video_ready, message_id=911)

    fake_openai.reset()
    _reset(video_ready.home)
    _inject(
        video_ready.home,
        message_type="private",
        user_id="20000002",
        message_id=912,
        text="这视频讲了啥",
    )
    assert wait_for(lambda: _reply_texts(video_ready.home), timeout=60)

    videos = _video_parts(_last_request(fake_openai))
    assert len(videos) == 1, f"追问时应当还能看到那段视频，实际 {len(videos)} 段"
    assert videos[0]["video_url"]["url"] == VIDEO_URL


def test_only_the_latest_video_survives(video_ready, fake_openai: FakeOpenAI) -> None:
    """连发两段视频：第二轮请求里只该剩最新的那段。"""
    first = "https://example.com/first.mp4"
    second = "https://example.com/second.mp4"

    fake_openai.reset()
    _send_video(video_ready, url=first, message_id=913)
    _send_video(video_ready, url=second, message_id=914)

    videos = _video_parts(_last_request(fake_openai))
    assert len(videos) == 1, f"上下文里只该保留最近一段视频，实际 {len(videos)}"
    assert videos[0]["video_url"]["url"] == second

    # 再追问一句：仍然只回放最新那段
    fake_openai.reset()
    _reset(video_ready.home)
    _inject(
        video_ready.home,
        message_type="private",
        user_id="20000002",
        message_id=915,
        text="第二个视频呢",
    )
    assert wait_for(lambda: _reply_texts(video_ready.home), timeout=60)
    videos = _video_parts(_last_request(fake_openai))
    assert len(videos) == 1
    assert videos[0]["video_url"]["url"] == second


def test_expired_video_url_is_not_replayed(video_ready, fake_openai: FakeOpenAI) -> None:
    """直链带 rkey、约两小时过期：过期了就别再往请求里塞。"""
    fake_openai.reset()
    _send_video(video_ready, message_id=916)

    # 把这条记忆的创建时间挪到 3 小时前（超过 VIDEO_URL_TTL_S）
    video_ready.db.execute(
        "UPDATE messages SET created_at = created_at - 10800 WHERE video IS NOT NULL"
    )

    fake_openai.reset()
    _reset(video_ready.home)
    _inject(
        video_ready.home,
        message_type="private",
        user_id="20000002",
        message_id=917,
        text="还记得那个视频吗",
    )
    assert wait_for(lambda: _reply_texts(video_ready.home), timeout=60)

    assert _video_parts(_last_request(fake_openai)) == [], "过期的直链不该再回放"
    assert "[视频]" in _all_text(_last_request(fake_openai)), "至少要留下占位符"


def test_video_understanding_can_be_disabled(video_ready, fake_openai: FakeOpenAI) -> None:
    video_ready.settings.update(video_understanding=False)
    fake_openai.reset()
    _send_video(video_ready, message_id=918)

    assert _video_parts(_last_request(fake_openai)) == [], "关掉后不该再把视频交给模型"


def test_vision_unsupported_falls_back_without_video(
    video_ready, fake_openai: FakeOpenAI
) -> None:
    """模型明确说不支持视频时：降级成纯文本重试，并且不能把回复弄丢。"""
    fake_openai.reset()
    fake_openai.fail_next(times=1, status=400, message="This model does not support video input")

    _send_video(video_ready, message_id=919)

    requests = fake_openai.requests()
    assert len(requests) >= 2, f"应当带视频失败后再试一次纯文本，实际 {len(requests)} 次"
    assert _video_parts(requests[0]), "第一次应当是带视频的"
    assert _video_parts(requests[1]) == [], "重试时不该再带视频"
    assert "[视频]" in _all_text(requests[1]), "降级后要保留 [视频] 占位"

    assert video_ready.bot.pipeline.stats.vision_fallbacks >= 1
    assert video_ready.bot.pipeline.stats.replied >= 1, "降级之后仍然要回复"
