"""生成 QLark 的程序图标（``src/qlark/assets/qlark.ico``）。

为什么要用脚本生成而不是塞一个二进制进仓库：

* 图标是**多尺寸 ico**（16/24/32/48/64/128/256），手工维护很难；
* 生成过程本身就是文档 —— 想换配色改这里的常量即可，不用找设计源文件。

配色跟界面一致：深色底 + 青绿色（--accent 一系的色相）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "src" / "qlark" / "assets" / "qlark.ico"

#: 界面主色（与 app.css 的 --accent 同色相）
ACCENT = (56, 189, 168)
ACCENT_DARK = (24, 96, 88)
BACKGROUND = (24, 27, 33)
SIZES = (16, 24, 32, 48, 64, 128, 256)


def draw(size: int):
    """画一枚图标：圆角方块底 + 一只侧身的抽象小鸟。

    只用四种形状（身体、头、喙、尾），且全部按比例缩放 —— 16px 时依然认得出
    是"一只鸟"，而不是一个辨认不出的色块。第一版画成了三角形翅膀，结果整体
    看起来像播放按钮，所以这里改成明确的头 / 喙 / 尾结构。
    """
    from PIL import Image, ImageDraw

    scale = 4  # 先画大图再降采样，边缘更干净
    canvas = size * scale
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    ctx = ImageDraw.Draw(image)

    def box(x0, y0, x1, y1):
        return [canvas * x0, canvas * y0, canvas * x1, canvas * y1]

    # 圆角底
    ctx.rounded_rectangle(
        [(0, 0), (canvas - 1, canvas - 1)], radius=int(canvas * 0.22), fill=BACKGROUND
    )

    # 尾巴（左下）
    ctx.polygon(
        [
            (canvas * 0.20, canvas * 0.72),
            (canvas * 0.44, canvas * 0.58),
            (canvas * 0.34, canvas * 0.84),
        ],
        fill=ACCENT_DARK,
    )

    # 身体
    ctx.ellipse(box(0.30, 0.42, 0.74, 0.82), fill=ACCENT)

    # 头（右上，与身体略有重叠，看起来才像连着的）
    ctx.ellipse(box(0.46, 0.22, 0.78, 0.58), fill=ACCENT)

    # 喙（右侧尖角）
    ctx.polygon(
        [
            (canvas * 0.76, canvas * 0.36),
            (canvas * 0.92, canvas * 0.44),
            (canvas * 0.75, canvas * 0.49),
        ],
        fill=ACCENT_DARK,
    )

    # 眼睛
    eye = canvas * 0.048
    center = (canvas * 0.63, canvas * 0.37)
    ctx.ellipse(
        [center[0] - eye, center[1] - eye, center[0] + eye, center[1] + eye],
        fill=BACKGROUND,
    )

    return image.resize((size, size), Image.LANCZOS)


def main() -> int:
    try:
        from PIL import Image
    except ImportError:
        print("需要 Pillow：pip install Pillow", file=sys.stderr)
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    frames = [draw(size) for size in SIZES]
    # 以最大尺寸为主图，其余作为附加尺寸写进同一个 ico
    frames[-1].save(OUT, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"已生成图标：{OUT}（{OUT.stat().st_size} 字节，{len(SIZES)} 种尺寸）")

    # 顺带导出一张 png，界面里也许用得上
    png = OUT.with_suffix(".png")
    frames[-1].save(png, format="PNG")
    print(f"已生成预览：{png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
