"""应用图标绘制（托盘 / ico / exe logo 共用同一设计）.

设计：靛蓝渐变圆角方块 + 白色 "A" + 底部纸页横线 + 右上角绿色圆点（新论文提示）。
"""
from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

__all__ = ["make_icon"]

_C1 = (99, 102, 241)        # indigo-500
_C2 = (67, 56, 202)         # indigo-700
_ACCENT = (16, 185, 129)    # emerald-500


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in ("arialbd.ttf", "arial.ttf", "msyhbd.ttc"):
        try:
            return ImageFont.truetype(f"C:/Windows/Fonts/{name}", size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_icon(size: int = 256) -> Image.Image:
    """生成 RGBA 图标."""
    # 1) 纵向渐变背景
    base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(base)
    for y in range(size):
        t = y / max(size - 1, 1)
        c = tuple(int(_C1[i] + (_C2[i] - _C1[i]) * t) for i in range(3))
        d.line([(0, y), (size, y)], fill=c + (255,))
    # 2) 圆角裁剪
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(size * 0.22), fill=255)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    img.paste(base, (0, 0), mask)
    d = ImageDraw.Draw(img)
    # 3) 底部“纸页”横线
    y = int(size * 0.76)
    d.rounded_rectangle(
        [int(size * 0.22), y, int(size * 0.78), y + max(int(size * 0.045), 2)],
        radius=max(int(size * 0.02), 1), fill=(255, 255, 255, 230))
    # 4) 白色 A
    f = _font(int(size * 0.56))
    bbox = d.textbbox((0, 0), "A", font=f)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((size - w) / 2 - bbox[0], (size - h) / 2 - bbox[1] - size * 0.07),
           "A", font=f, fill=(255, 255, 255, 255))
    # 5) 右上角绿色圆点
    r = max(int(size * 0.085), 3)
    d.ellipse([size - 2 * r - 2, 2, size - 2, 2 + 2 * r], fill=_ACCENT + (255,))
    return img
