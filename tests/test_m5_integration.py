"""M5 端到端：图片 → 立即下载 → 预处理 → 视觉模型，以及不支持时的降级。

验收标准（需求规格 §11 M5）：**群聊发图能描述内容；2 小时后仍可处理（证明已即时下载）**。

"即时下载"这条在这里的对应验证是：注入的图片段**只有一个 file 标识、没有 url**，
QLark 必须自己向 NapCat 要地址（``get_image``）并把图片真的抓下来。等到"2 小时后"
URL 早就失效了 —— 所以只能在收到消息的那一刻抓。
"""

from __future__ import annotations

import pytest

from conftest import FakeOpenAI, _get, _post, wait_for
from qlark.chat.store import memory_key
from qlark.core.models import Phase

from test_m2_integration import _inject, _reply_texts, _reset, ready  # noqa: F401

KEY = memory_key("private:20000002", 1)

IMAGE_SEGMENTS = [
    {"type": "text", "data": {"text": "看看这张图"}},
    # 故意不给 url：真实场景里 NapCat 有时只给 file，必须靠 get_image 换地址
    {"type": "image", "data": {"file": "fake-image-id"}},
]


def _actions(home) -> list[dict]:
    return _get(f"http://127.0.0.1:{home.port}/_qlark/sent").get("actions", [])


def _last_request(fake: FakeOpenAI) -> dict:
    requests = fake.requests()
    assert requests, "模型一次都没被调用"
    return requests[-1]


def _user_content(payload: dict):
    """取最后一条 user 消息的 content（可能是字符串，也可能是多模态数组）。"""
    for message in payload.get("messages") or []:
        if message.get("role") == "user":
            return message.get("content")
    return None


def _content_parts(payload: dict) -> list[dict]:
    """把 user content 归一化成"parts 数组"，两种形态都能统一断言。"""
    content = _user_content(payload)
    if isinstance(content, list):
        return content
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def _content_text(payload: dict) -> str:
    return " ".join(
        str(part.get("text") or "") for part in _content_parts(payload) if part.get("type") == "text"
    )


def _send_image(env, *, message_id: int = 900) -> None:
    _reset(env.home)
    _inject(
        env.home,
        message_type="private",
        user_id="20000002",
        message_id=message_id,
        text="看看这张图",
        segments=IMAGE_SEGMENTS,
    )
    assert wait_for(lambda: _reply_texts(env.home), timeout=60), "图片消息没有回复"


@pytest.fixture
def vision_ready(ready, fake_openai: FakeOpenAI):
    """配上视觉模型，并把记忆轮数调大避免触发压缩干扰断言。"""
    ready.settings.update(
        vision_model="fake-vision-model",
        image_understanding=True,
        max_images_per_message=3,
        memory_rounds=50,
        auto_summary=False,
    )
    ready.bot.pipeline.invalidate_clients()
    return ready


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #
def test_image_is_downloaded_and_sent_to_vision_model(vision_ready, fake_openai: FakeOpenAI) -> None:
    fake_openai.reset()
    _send_image(vision_ready)

    # 1) QLark 真的向 NapCat 索取了图片地址
    actions = [item["action"] for item in _actions(vision_ready.home)]
    assert "get_image" in actions, f"没有调用 get_image，实际动作：{actions}"

    # 2) 交给模型的是 image_url 段，且是 data URL
    parts = _content_parts(_last_request(fake_openai))
    kinds = [part.get("type") for part in parts]
    assert "image_url" in kinds, f"请求里没有图片：{kinds}"
    data_url = parts[kinds.index("image_url")]["image_url"]["url"]
    assert data_url.startswith("data:image/jpeg;base64,"), data_url[:60]

    # 3) 图片被缩放到长边 ≤ 1024（样例图是 1200x800）
    import base64
    import io

    from PIL import Image

    raw = base64.b64decode(data_url.split(",", 1)[1])
    with Image.open(io.BytesIO(raw)) as image:
        assert max(image.size) == 1024, f"没有缩放到 1024：{image.size}"


def test_image_message_gets_a_reply(vision_ready, fake_openai: FakeOpenAI) -> None:
    fake_openai.reset()
    _send_image(vision_ready, message_id=901)
    assert _reply_texts(vision_ready.home), "图片消息应当得到回复"
    assert vision_ready.bot.pipeline.stats.images >= 1


def test_memory_stores_placeholder_not_base64(vision_ready, fake_openai: FakeOpenAI) -> None:
    """记忆里只许存 [图片]，绝不能把 base64 写进数据库。"""
    fake_openai.reset()
    _send_image(vision_ready, message_id=902)

    store = vision_ready.bot.pipeline.store
    rows = store.memory_messages(KEY)
    assert rows, "图片消息没有落库"

    user_row = next(row for row in rows if row["role"] == "user")
    assert "[图片]" in user_row["content"], user_row["content"][:120]
    assert "base64" not in user_row["content"]
    assert len(user_row["content"]) < 200, "记忆里不该出现超长内容"
    assert user_row["has_image"] == 1


def test_image_understanding_can_be_disabled(vision_ready, fake_openai: FakeOpenAI) -> None:
    vision_ready.settings.update(image_understanding=False)
    fake_openai.reset()
    _send_image(vision_ready, message_id=903)

    parts = _content_parts(_last_request(fake_openai))
    assert not any(part.get("type") == "image_url" for part in parts), "关掉后不该再传图"


def test_image_limit_is_respected(vision_ready, fake_openai: FakeOpenAI) -> None:
    vision_ready.settings.update(max_images_per_message=1)
    fake_openai.reset()

    segments = [{"type": "text", "data": {"text": "三张图"}}] + [
        {"type": "image", "data": {"file": f"img-{index}"}} for index in range(3)
    ]
    _reset(vision_ready.home)
    _inject(
        vision_ready.home,
        message_type="private",
        user_id="20000002",
        message_id=904,
        text="三张图",
        segments=segments,
    )
    assert wait_for(lambda: _reply_texts(vision_ready.home), timeout=60)

    parts = _content_parts(_last_request(fake_openai))
    images = [part for part in parts if part.get("type") == "image_url"]
    assert len(images) == 1, f"上限是 1，实际传了 {len(images)} 张"


# --------------------------------------------------------------------------- #
# 降级
# --------------------------------------------------------------------------- #
def test_vision_unsupported_falls_back_to_text(vision_ready, fake_openai: FakeOpenAI) -> None:
    """模型明确说不支持图片时：降级成纯文本重试一次，并且要留下明确的日志提示。

    直接报错会让用户莫名其妙 —— 他明明看到"消息发出了"，却只收到一句错误。
    """
    fake_openai.reset()
    # 只让第一次请求失败（就是那次带图的）
    fake_openai.fail_next(times=1, status=400, message="This model does not support image input")

    _send_image(vision_ready, message_id=905)

    requests = fake_openai.requests()
    assert len(requests) >= 2, f"应当带图失败后再试一次纯文本，实际只有 {len(requests)} 次"

    first = _content_parts(requests[0])
    second = _content_parts(requests[1])
    assert any(part.get("type") == "image_url" for part in first), "第一次应当是带图的"
    assert not any(part.get("type") == "image_url" for part in second), "重试应当是纯文本"
    assert "[图片]" in _content_text(requests[1]), "降级后要保留 [图片] 占位，别让上下文缺一块"

    assert vision_ready.bot.pipeline.stats.vision_fallbacks >= 1
    assert any(
        "视觉模型" in line.text for line in vision_ready.logbus.tail(4000)
    ), "必须提示用户去配视觉模型，否则他永远以为图片被看懂了"


def test_ordinary_error_is_not_treated_as_vision_failure(vision_ready, fake_openai: FakeOpenAI) -> None:
    """普通的 500 不能被误判成"不支持图片"而白白降级一次。"""
    fake_openai.reset()
    fake_openai.fail_next(times=99, status=500, message="internal server error")

    _reset(vision_ready.home)
    _inject(
        vision_ready.home,
        message_type="private",
        user_id="20000002",
        message_id=906,
        text="看看这张图",
        segments=IMAGE_SEGMENTS,
    )
    assert wait_for(lambda: vision_ready.bot.pipeline.stats.failed >= 1, timeout=60)

    assert vision_ready.bot.pipeline.stats.vision_fallbacks == 0, "不该降级"


def test_broken_image_falls_back_to_placeholder(vision_ready, fake_openai: FakeOpenAI) -> None:
    """图片下不动时，消息照常回复，只是按 [图片] 占位处理。"""
    vision_ready.settings.update(vision_model="")
    fake_openai.reset()

    segments = [
        {"type": "text", "data": {"text": "坏图"}},
        {"type": "image", "data": {"file": ""}},  # 既没有 url 也没有 file
    ]
    _reset(vision_ready.home)
    _inject(
        vision_ready.home,
        message_type="private",
        user_id="20000002",
        message_id=907,
        text="坏图",
        segments=segments,
    )
    assert wait_for(lambda: _reply_texts(vision_ready.home), timeout=60), "坏图不该让消息失联"

    parts = _content_parts(_last_request(fake_openai))
    assert not any(part.get("type") == "image_url" for part in parts)
    assert "[图片]" in _content_text(_last_request(fake_openai)), "应当降级成 [图片] 占位文本"


# --------------------------------------------------------------------------- #
# 真机才会出现的一种情况：URL 直接给好了
# --------------------------------------------------------------------------- #
def test_direct_url_is_used_without_get_image(vision_ready, fake_openai: FakeOpenAI) -> None:
    """段里已经带了 url 时，不该再多余地问 NapCat 一次。"""
    fake_openai.reset()
    url = f"http://127.0.0.1:{vision_ready.home.port}/_qlark/sample.png"

    _reset(vision_ready.home)
    _inject(
        vision_ready.home,
        message_type="private",
        user_id="20000002",
        message_id=908,
        text="带 url 的图",
        segments=[
            {"type": "text", "data": {"text": "带 url 的图"}},
            {"type": "image", "data": {"file": "x", "url": url}},
        ],
    )
    assert wait_for(lambda: _reply_texts(vision_ready.home), timeout=60)

    kinds = [part.get("type") for part in _content_parts(_last_request(fake_openai))]
    assert "image_url" in kinds
    assert not _actions(vision_ready.home), "已经有 url 了就不该再调 get_image"
