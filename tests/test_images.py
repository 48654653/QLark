"""M5 图片链路测试：下载 → 缩放 → JPEG → data URL（需求规格 §6.5）。

覆盖的边界都是会真实发生的：透明通道、动图、超大图、下不动、格式不对、
以及"url 缺失只能问 NapCat 要"。
"""

from __future__ import annotations

import base64
import io
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from qlark.chat.images import (
    ImageError,
    ImagePreparer,
    build_user_content,
    is_local_url,
    looks_like_vision_unsupported,
)
from qlark.onebot.segments import ImageRef


def make_png(width: int, height: int, mode: str = "RGB") -> bytes:
    from PIL import Image

    color = (200, 30, 30, 255) if mode == "RGBA" else (200, 30, 30)
    image = Image.new(mode, (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def make_gif(frames: int = 3) -> bytes:
    from PIL import Image

    images = [Image.new("RGB", (60, 40), (10 * index, 200, 50)) for index in range(frames)]
    buffer = io.BytesIO()
    images[0].save(buffer, format="GIF", save_all=True, append_images=images[1:], duration=100)
    return buffer.getvalue()


def decode_data_url(data_url: str) -> tuple[bytes, tuple[int, int], str]:
    from PIL import Image

    assert data_url.startswith("data:image/jpeg;base64,"), data_url[:40]
    raw = base64.b64decode(data_url.split(",", 1)[1])
    with Image.open(io.BytesIO(raw)) as image:
        return raw, image.size, image.format


# --------------------------------------------------------------------------- #
# 本地文件
# --------------------------------------------------------------------------- #
def test_prepare_local_file(tmp_path: Path) -> None:
    path = tmp_path / "a.png"
    path.write_bytes(make_png(300, 200))

    prepared = ImagePreparer().prepare(ImageRef(file=str(path)))
    _raw, size, fmt = decode_data_url(prepared.data_url)
    assert size == (300, 200)
    assert fmt == "JPEG", "统一转成 JPEG（视觉模型对它的兼容性最好）"


def test_prepare_file_url_scheme(tmp_path: Path) -> None:
    path = tmp_path / "b.png"
    path.write_bytes(make_png(50, 50))
    prepared = ImagePreparer().prepare(ImageRef(file=path.as_uri()))
    assert prepared.width == 50


def test_missing_local_file_raises() -> None:
    with pytest.raises(ImageError, match="不存在"):
        ImagePreparer().prepare(ImageRef(file="E:/definitely/not/here.png"))


def test_no_address_raises() -> None:
    with pytest.raises(ImageError, match="没有可用地址"):
        ImagePreparer().prepare(ImageRef())


# --------------------------------------------------------------------------- #
# 缩放
# --------------------------------------------------------------------------- #
def test_large_image_is_downscaled_to_max_side(tmp_path: Path) -> None:
    path = tmp_path / "big.png"
    path.write_bytes(make_png(3000, 1500))
    prepared = ImagePreparer(max_side=1024).prepare(ImageRef(file=str(path)))
    assert max(prepared.width, prepared.height) == 1024
    assert (prepared.width, prepared.height) == (1024, 512), "必须等比缩放"


def test_small_image_is_not_upscaled(tmp_path: Path) -> None:
    path = tmp_path / "small.png"
    path.write_bytes(make_png(120, 90))
    prepared = ImagePreparer().prepare(ImageRef(file=str(path)))
    assert (prepared.width, prepared.height) == (120, 90), "小图不该被放大（白费 token）"


def test_portrait_image_keeps_ratio(tmp_path: Path) -> None:
    path = tmp_path / "tall.png"
    path.write_bytes(make_png(800, 2400))
    prepared = ImagePreparer(max_side=1024).prepare(ImageRef(file=str(path)))
    assert (prepared.width, prepared.height) == (341, 1024)


# --------------------------------------------------------------------------- #
# 透明通道 / 动图
# --------------------------------------------------------------------------- #
def test_transparent_png_is_flattened_to_white(tmp_path: Path) -> None:
    """带透明的图直接转 RGB 会变成一片黑，必须先贴到白底上。"""
    from PIL import Image

    path = tmp_path / "t.png"
    # 左半边完全不透明（红），右半边完全透明 —— 透明处必须是白的，不能是黑的
    image = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    for y in range(40):
        for x in range(20):
            image.putpixel((x, y), (200, 30, 30, 255))
    image.save(path, format="PNG")

    prepared = ImagePreparer().prepare(ImageRef(file=str(path)))
    raw, _size, _fmt = decode_data_url(prepared.data_url)
    with Image.open(io.BytesIO(raw)) as result:
        assert result.mode == "RGB"
        opaque = result.getpixel((5, 20))
        transparent = result.getpixel((35, 20))
        assert opaque[0] > 150, f"不透明区域应当还是红的：{opaque}"
        assert min(transparent) > 200, f"透明区域应当贴成白底而不是黑底：{transparent}"


def test_animated_gif_takes_first_frame(tmp_path: Path) -> None:
    path = tmp_path / "a.gif"
    path.write_bytes(make_gif(4))

    prepared = ImagePreparer().prepare(ImageRef(file=str(path)))
    _raw, size, fmt = decode_data_url(prepared.data_url)
    assert fmt == "JPEG"
    assert size == (60, 40), "动图只取首帧"


# --------------------------------------------------------------------------- #
# 上限与失败
# --------------------------------------------------------------------------- #
def test_oversized_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "huge.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 5000)
    with pytest.raises(ImageError, match="过大"):
        ImagePreparer(max_bytes=1000).prepare(ImageRef(file=str(path)))


def test_non_image_content_raises(tmp_path: Path) -> None:
    path = tmp_path / "notimage.png"
    path.write_bytes(b"this is definitely not an image")
    with pytest.raises(ImageError, match="解码失败"):
        ImagePreparer().prepare(ImageRef(file=str(path)))


# --------------------------------------------------------------------------- #
# HTTP 下载
# --------------------------------------------------------------------------- #
@pytest.fixture
def image_server(tmp_path: Path):
    (tmp_path / "remote.png").write_bytes(make_png(800, 600))
    (tmp_path / "bigger.png").write_bytes(make_png(2000, 1000))

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(tmp_path), **kwargs)

        def log_message(self, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_download_from_http(image_server: str) -> None:
    prepared = ImagePreparer().prepare(ImageRef(url=f"{image_server}/remote.png"))
    _raw, size, fmt = decode_data_url(prepared.data_url)
    assert size == (800, 600)
    assert fmt == "JPEG"
    assert prepared.size > 0


def test_downloaded_big_image_is_scaled(image_server: str) -> None:
    prepared = ImagePreparer(max_side=1024).prepare(ImageRef(url=f"{image_server}/bigger.png"))
    assert max(prepared.width, prepared.height) == 1024


def test_download_404_raises(image_server: str) -> None:
    with pytest.raises(ImageError, match="HTTP 404"):
        ImagePreparer().prepare(ImageRef(url=f"{image_server}/nope.png"))


def test_download_timeout_raises(image_server: str, monkeypatch) -> None:
    import time

    class SlowHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_GET(self):  # noqa: N802
            time.sleep(2.0)
            super().do_GET()

    slow = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    threading.Thread(target=slow.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{slow.server_address[1]}/remote.png"
        with pytest.raises(ImageError, match="下载失败"):
            ImagePreparer(timeout=0.4).prepare(ImageRef(url=url))
    finally:
        slow.shutdown()
        slow.server_close()


def test_local_url_bypasses_proxy(monkeypatch) -> None:
    """本机图片不能走系统代理（Windows 的绕过白名单在 Python 里不生效）。"""
    preparer = ImagePreparer()
    assert preparer._session_for("http://127.0.0.1:8080/a.png") is preparer._local_session
    assert preparer._session_for("https://multimedia.nt.qq.com.cn/a.png") is preparer._session
    assert preparer._local_session.trust_env is False


def test_is_local_url() -> None:
    assert is_local_url("http://127.0.0.1:3000/a.png")
    assert is_local_url("http://localhost/a.png")
    assert is_local_url("http://127.5.5.5/a.png")
    assert not is_local_url("https://multimedia.nt.qq.com.cn/a.png")
    assert not is_local_url("")


# --------------------------------------------------------------------------- #
# 批量
# --------------------------------------------------------------------------- #
def test_prepare_all_respects_limit(tmp_path: Path) -> None:
    refs = []
    for index in range(5):
        path = tmp_path / f"{index}.png"
        path.write_bytes(make_png(50, 50))
        refs.append(ImageRef(file=str(path)))

    images, problems = ImagePreparer().prepare_all(refs, limit=2)
    assert len(images) == 2
    assert any("上限" in item for item in problems)


def test_prepare_all_survives_one_failure(tmp_path: Path) -> None:
    """一张图坏掉不该让整条消息失去所有图片。"""
    good = tmp_path / "ok.png"
    good.write_bytes(make_png(60, 60))

    refs = [
        ImageRef(file=str(good)),
        ImageRef(file="E:/not/here.png"),
        ImageRef(file=str(good)),
    ]
    images, problems = ImagePreparer().prepare_all(refs, limit=5)
    assert len(images) == 2, "坏的那张被跳过，好的两张都要保留"
    assert len(problems) == 1


def test_resolve_callback_is_used_when_url_missing(tmp_path: Path) -> None:
    """url 缺失时要回调出去问（真实场景是 OneBot 的 get_image）。"""
    path = tmp_path / "resolved.png"
    path.write_bytes(make_png(70, 70))
    calls: list[str] = []

    def resolve(ref: ImageRef) -> str:
        calls.append(str(ref.file))
        return str(path)

    prepared = ImagePreparer().prepare(ImageRef(file="fake-file-id"), resolve=resolve)
    assert calls == ["fake-file-id"]
    assert prepared.width == 70


# --------------------------------------------------------------------------- #
# 组装与降级判定
# --------------------------------------------------------------------------- #
def test_build_user_content_shape(tmp_path: Path) -> None:
    path = tmp_path / "x.png"
    path.write_bytes(make_png(30, 30))
    prepared = ImagePreparer().prepare(ImageRef(file=str(path)))

    content = build_user_content("看看这个", [prepared])
    assert content[0] == {"type": "text", "text": "看看这个"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_build_user_content_omits_empty_text(tmp_path: Path) -> None:
    path = tmp_path / "y.png"
    path.write_bytes(make_png(30, 30))
    prepared = ImagePreparer().prepare(ImageRef(file=str(path)))
    content = build_user_content("   ", [prepared])
    assert len(content) == 1 and content[0]["type"] == "image_url"


def test_vision_unsupported_detection() -> None:
    assert looks_like_vision_unsupported("This model does not support image input")
    assert looks_like_vision_unsupported("invalid content type: image_url")
    assert looks_like_vision_unsupported("当前模型不支持图片")
    # 普通错误不能被误判成"不支持视觉"，否则会白白降级一次
    assert not looks_like_vision_unsupported("rate limit exceeded")
    assert not looks_like_vision_unsupported("500 internal server error")
    assert not looks_like_vision_unsupported("")
