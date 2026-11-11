"""Draws mac/icon.png: the Rojo Launcher icon on Apple's app icon grid (a rounded square
824 px wide on a 1024 canvas, with a soft shadow), so it sits right next to other Mac apps.
Same design as icon.png. Run: python3 mac/make_icon.py  (needs Pillow and numpy)"""
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

S = 4                      # supersampling for smooth edges
N = 1024 * S
OUT = Path(__file__).with_name("icon.png")


def squircle(x0, y0, size):
    """Apple's app icon plate: a rounded square with about 22.5% corners, the corner curve
    eased into the straight edges (blur and re-cut) so it reads as a continuous curve."""
    m = Image.new("L", (N, N), 0)
    ImageDraw.Draw(m).rounded_rectangle([x0, y0, x0 + size - 1, y0 + size - 1], radius=size * 0.2245, fill=255)
    return m.filter(ImageFilter.GaussianBlur(size * 0.012)).point(lambda v: 255 if v >= 128 else 0)


def rounded(box, r):
    m = Image.new("L", (N, N), 0)
    ImageDraw.Draw(m).rounded_rectangle([c * S for c in box], radius=r * S, fill=255)
    return m


def gradient(top, bottom):
    t = np.linspace(0, 1, N, dtype=np.float32)[:, None, None]
    a, b = np.array(top, np.float32), np.array(bottom, np.float32)
    rgb = (a + (b - a) * t) * np.ones((1, N, 1), np.float32)
    return Image.fromarray(rgb.clip(0, 255).astype(np.uint8), "RGB").convert("RGBA")


canvas = Image.new("RGBA", (N, N), (0, 0, 0, 0))
plate = squircle(100 * S, 100 * S, 824 * S)

# soft drop shadow under the plate
shadow = Image.new("RGBA", (N, N), (0, 0, 0, 0))
shadow.putalpha(plate.point(lambda v: v * 0.32))
shadow = shadow.filter(ImageFilter.GaussianBlur(12 * S))
canvas.alpha_composite(shadow, (0, 10 * S))

canvas.paste(gradient((30, 35, 45), (17, 20, 26)), (0, 0), plate)          # dark plate
canvas.paste(gradient((236, 86, 72), (214, 60, 49)), (0, 0), rounded((268, 268, 760, 760), 44))  # red frame
canvas.paste(gradient((26, 30, 38), (19, 22, 28)), (0, 0), rounded((369, 369, 655, 655), 22))    # screen

# white play triangle with softly rounded corners (draw it small, then grow it back round)
tri = Image.new("L", (N, N), 0)
pts = [(458, 446), (458, 578), (574, 512)]
ImageDraw.Draw(tri).polygon([(x * S, y * S) for x, y in pts], fill=255)
tri = tri.filter(ImageFilter.GaussianBlur(5 * S)).point(lambda v: 255 if v > 60 else 0)
canvas.paste(Image.new("RGBA", (N, N), (255, 255, 255, 255)), (0, 0), tri)

canvas.resize((1024, 1024), Image.LANCZOS).save(OUT)
print("wrote", OUT)
