"""图片理解链路：下载 → 缩放 → 转 JPEG → base64 data URL。

需求规格 v1.1 §6.5（已冻结）：

.. code-block:: text

    收到 image 段
      → 【立即下载】（URL 约 2 小时过期）：超时保护 + 大小上限
      → 预处理：等比缩放到长边 ≤ 1024，转 JPEG（base64 膨胀 33%）
      → 转 data URL 交给视觉模型
      → 记忆库中只存 [图片]

**"立即下载"不是可选项**：QQ 的图片 URL 约 2 小时后失效（技术文档坑 17）。
等用户过一会儿再问"刚才那张图是什么"，URL 早就没用了 —— 所以收到消息就得抓下来。

**记忆库只存 ``[图片]``**：base64 动辄几百 KB，写进 SQLite 会让库迅速膨胀，
而且历史图片对后续对话几乎没有价值。
"""

from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import requests

from ..onebot.segments import ImageRef

log = logging.getLogger(__name__)

#: 缩放后的长边上限
MAX_SIDE = 1024
#: JPEG 质量（85 在体积与清晰度之间比较平衡）
JPEG_QUALITY = 85
#: 单张图片下载体积上限
MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024
#: 下载超时（连接 / 读取）
DOWNLOAD_TIMEOUT = 15.0
#: 单条消息最多处理几张图
DEFAULT_MAX_IMAGES = 3
#: 视觉模型单张图的说明文字上限（防止它长篇大论把上下文撑爆）
DESCRIPTION_MAX_CHARS = 300

#: 能直接交给视觉模型的图片格式
_SUPPORTED_INPUT = frozenset({"JPEG", "PNG", "WEBP", "BMP", "GIF", "TIFF", "MPO"})


@dataclass(frozen=True)
class PreparedImage:
    """可以直接塞进 ``image_url`` 段的图片。"""

    data_url: str
    width: int
    height: int
    #: 编码后的字节数（base64 之前）
    size: int
    #: 来源描述，仅用于日志
    source: str

    def to_segment(self) -> dict[str, Any]:
        return {"type": "image_url", "image_url": {"url": self.data_url}}


class ImageError(RuntimeError):
    """图片处理失败（下载不到、格式不支持、太大等）。"""


def is_local_url(url: str) -> bool:
    """判断是不是"本机"地址（这些请求必须绕开系统代理）。"""
    try:
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.startswith("127.")


def build_user_content(text: str, images: Iterable[PreparedImage]) -> list[dict[str, Any]]:
    """把文本与图片拼成 OpenAI 兼容的多模态 ``content`` 数组。

    文本放在最前面：多数视觉模型对"先给指令再给图"的遵循度更好，而且这样
    在模型不支持图片时，至少还能看到用户在说什么。
    """
    parts: list[dict[str, Any]] = []
    clean = str(text or "").strip()
    if clean:
        parts.append({"type": "text", "text": clean})
    for image in images:
        parts.append(image.to_segment())
    return parts


class ImagePreparer:
    """下载并把图片转成 data URL。

    代理策略**按目标主机分流**（这是踩过的坑）：

    * QQ 的图片 CDN 是**远端**，必须走系统代理 —— 很多网络环境下不走就下不动；
    * 但 ``get_image`` 也可能返回**本机**地址（NapCat 本地缓存、或测试用的假服务）。
      Windows 的 ProxyOverride 白名单在 Python 里不生效，那种请求会被丢给代理，
      表现为莫名其妙的超时。

    所以远端用默认会话，本机用 ``trust_env=False`` 的会话。
    """

    def __init__(
        self,
        *,
        max_side: int = MAX_SIDE,
        max_bytes: int = MAX_DOWNLOAD_BYTES,
        timeout: float = DOWNLOAD_TIMEOUT,
        session: requests.Session | None = None,
        local_session: requests.Session | None = None,
    ) -> None:
        self.max_side = int(max_side)
        self.max_bytes = int(max_bytes)
        self.timeout = float(timeout)
        #: 远端图片（走系统代理）
        self._session = session or requests.Session()
        #: 本机图片（绕开代理）
        self._local_session = local_session or requests.Session()
        self._local_session.trust_env = False

    def _session_for(self, url: str) -> requests.Session:
        return self._local_session if is_local_url(url) else self._session

    # ------------------------------------------------------------------ 主流程
    def prepare(self, ref: ImageRef, *, resolve: Callable[[ImageRef], str | None] | None = None) -> PreparedImage:
        """把一条图片引用变成 data URL；失败抛 :class:`ImageError`。"""
        target = self._resolve(ref, resolve)
        if not target:
            raise ImageError("图片没有可用地址（url 与 file 都取不到）")

        if target.startswith(("http://", "https://")):
            raw = self._download(target)
        else:
            raw = self._read_local(target)

        return self._encode(raw, source=target)

    def prepare_all(
        self,
        refs: Iterable[ImageRef],
        *,
        limit: int = DEFAULT_MAX_IMAGES,
        resolve: Callable[[ImageRef], str | None] | None = None,
    ) -> tuple[list[PreparedImage], list[str]]:
        """批量处理，返回 ``(成功的图片, 失败原因列表)``。

        **不因为一张图失败就放弃整条消息**：能处理几张算几张，剩下的降级成占位。
        """
        prepared: list[PreparedImage] = []
        problems: list[str] = []
        for index, ref in enumerate(refs):
            if len(prepared) >= max(0, int(limit)):
                problems.append(f"超过 {limit} 张上限，其余图片已跳过")
                break
            try:
                prepared.append(self.prepare(ref, resolve=resolve))
            except ImageError as exc:
                problems.append(f"第 {index + 1} 张：{exc}")
            except Exception as exc:  # noqa: BLE001 - 单张图的意外不该拖垮整条消息
                log.exception("处理图片时发生未预期异常")
                problems.append(f"第 {index + 1} 张：{exc}")
        return prepared, problems

    # ------------------------------------------------------------------ 取材
    def _resolve(self, ref: ImageRef, resolve: Callable[[ImageRef], str | None] | None) -> str | None:
        url = (ref.url or "").strip()
        if url:
            return url
        if resolve is not None:
            return resolve(ref)
        file_value = (ref.file or "").strip()
        return file_value or None

    def _read_local(self, path: str) -> bytes:
        candidate = path
        if candidate.startswith("file://"):
            from urllib.parse import unquote, urlparse

            parsed = urlparse(candidate)
            candidate = unquote(parsed.path)
            if parsed.netloc and parsed.netloc not in ("", "localhost"):
                # file://server/share/... → UNC 路径
                candidate = f"//{parsed.netloc}{candidate}"
            elif len(candidate) > 2 and candidate[0] == "/" and candidate[2] == ":":
                # Windows 上 urlparse 会把 C:/x 变成 /C:/x，多一个前导斜杠
                candidate = candidate[1:]
        from pathlib import Path

        target = Path(candidate)
        if not target.is_file():
            raise ImageError(f"本地文件不存在：{candidate}")
        size = target.stat().st_size
        if size > self.max_bytes:
            raise ImageError(f"图片过大（{size // 1024} KB）")
        return target.read_bytes()

    def _download(self, url: str) -> bytes:
        try:
            response = self._session_for(url).get(url, timeout=self.timeout, stream=True)
        except requests.RequestException as exc:
            raise ImageError(f"下载失败：{exc}") from exc

        with response:
            if response.status_code != 200:
                raise ImageError(f"下载失败：HTTP {response.status_code}")

            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > self.max_bytes:
                raise ImageError(f"图片过大（{int(declared) // 1024} KB）")

            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > self.max_bytes:
                    # 边下边判，避免"声明很小、实际很大"把内存吃爆
                    raise ImageError(f"图片超过 {self.max_bytes // 1024 // 1024} MB 上限")
                chunks.append(chunk)
            if not chunks:
                raise ImageError("下载到空内容")
            return b"".join(chunks)

    # ------------------------------------------------------------------ 编码
    def _encode(self, raw: bytes, *, source: str) -> PreparedImage:
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - 依赖缺失时给出可读提示
            raise ImageError("缺少 Pillow，无法处理图片（pip install Pillow）") from exc

        try:
            with Image.open(io.BytesIO(raw)) as opened:
                fmt = (opened.format or "").upper()
                if fmt and fmt not in _SUPPORTED_INPUT:
                    raise ImageError(f"不支持的图片格式：{fmt}")

                # 动图 / 表情包：只取首帧（需求规格 §6.5）
                try:
                    opened.seek(0)
                except (EOFError, ValueError):
                    pass

                image = self._flatten(opened)
                image = self._resize(image)

                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
                payload = buffer.getvalue()
                width, height = image.size
        except ImageError:
            raise
        except Exception as exc:  # noqa: BLE001 - Pillow 的异常种类很多，统一降级
            raise ImageError(f"图片解码失败：{exc}") from exc

        encoded = base64.b64encode(payload).decode("ascii")
        return PreparedImage(
            data_url=f"data:image/jpeg;base64,{encoded}",
            width=width,
            height=height,
            size=len(payload),
            source=source,
        )

    def _flatten(self, opened: Any) -> Any:
        """把任意色彩模式摊平成 RGB。

        带透明通道的图（RGBA / P 带 transparency）直接转 RGB 会得到一片黑底，
        所以先贴到白底上 —— 截图类图片绝大多数是这种情况。
        """
        from PIL import Image

        if opened.mode in ("RGBA", "LA") or (
            opened.mode == "P" and "transparency" in opened.info
        ):
            rgba = opened.convert("RGBA")
            canvas = Image.new("RGB", rgba.size, (255, 255, 255))
            canvas.paste(rgba, mask=rgba.split()[-1])
            return canvas
        if opened.mode != "RGB":
            return opened.convert("RGB")
        return opened.copy()

    def _resize(self, image: Any) -> Any:
        from PIL import Image

        width, height = image.size
        longest = max(width, height)
        if longest <= self.max_side:
            return image
        scale = self.max_side / float(longest)
        target = (max(1, int(width * scale)), max(1, int(height * scale)))
        return image.resize(target, Image.LANCZOS)


#: 判断一个异常是不是"模型不支持图片"
_VISION_HINTS = (
    "image",
    "vision",
    "multimodal",
    "content type",
    "image_url",
    # 中文服务端的措辞
    "图片",
    "图像",
    "视觉",
    "多模态",
)


def looks_like_vision_unsupported(message: str) -> bool:
    """启发式判断：服务端是不是在抱怨"我不支持图片"。

    只认明确的措辞，避免把普通的 400 也误判成"不支持视觉"而白白降级一次。
    """
    text = (message or "").lower()
    if not any(hint in text for hint in _VISION_HINTS):
        return False
    return any(
        marker in text
        for marker in (
            "not support",
            "unsupported",
            "invalid content",
            "does not support",
            "不支持",
            "无法处理",
            "invalid type",
        )
    )


__all__ = [
    "DEFAULT_MAX_IMAGES",
    "DESCRIPTION_MAX_CHARS",
    "DOWNLOAD_TIMEOUT",
    "ImageError",
    "ImagePreparer",
    "JPEG_QUALITY",
    "MAX_DOWNLOAD_BYTES",
    "MAX_SIDE",
    "PreparedImage",
    "build_user_content",
    "is_local_url",
    "looks_like_vision_unsupported",
]
