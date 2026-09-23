"""生成 assets/arxiver.ico（多尺寸）与 arxiver.png 预览.

运行: .venv/Scripts/python assets/make_icon.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arxiver.icon import make_icon

out = Path(__file__).parent
img = make_icon(256)
img.save(out / "arxiver.png")
# bitmap_format="bmp"：传统 BMP 帧编码。Pillow 新版默认用 PNG 压缩帧，
# 而 .NET System.Drawing.Icon（pywebview WinForms 窗口图标用）不支持 PNG
# 压缩的 ICO 条目，会导致主窗口/任务栏图标回退成 Python 默认图标。
img.save(out / "arxiver.ico", bitmap_format="bmp",
         sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("saved:", out / "arxiver.ico")
