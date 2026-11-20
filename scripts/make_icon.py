"""Render the Era One icon (normal + debug) as multi-size .ico and preview PNGs."""

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

S = 1024  # master canvas
OUT = Path(sys.argv[1])
SIZES = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(len(a)))


def gradient(size, c1, c2, angle_deg=135):
    w, h = size
    img = Image.new("RGBA", size)
    px = img.load()
    a = math.radians(angle_deg)
    dx, dy = math.cos(a), math.sin(a)
    span = abs(dx) * w + abs(dy) * h
    for y in range(h):
        for x in range(w):
            t = ((x * dx + y * dy) - min(0, dx * w) - min(0, dy * h)) / span
            px[x, y] = lerp(c1, c2, max(0.0, min(1.0, t)))
    return img


def tile_mask(r=230):
    m = Image.new("L", (S, S), 0)
    ImageDraw.Draw(m).rounded_rectangle((24, 24, S - 24, S - 24), r, fill=255)
    return m


def glyph_mask():
    """Ring + circuit 'E' as a single-colour mask."""
    m = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(m)
    cx = cy = S // 2
    R, w = 360, 58
    # open ring: gap at upper right, where a node dot sits
    d.arc((cx - R, cy - R, cx + R, cy + R), start=-20, end=300, fill=255, width=w)
    a = math.radians(-35)
    nx, ny = cx + R * math.cos(a), cy + R * math.sin(a)
    d.ellipse((nx - 50, ny - 50, nx + 50, ny + 50), fill=255)
    # circuit E: spine + three arms ending in nodes
    lw = 54
    x0, x1 = cx - 170, cx + 120
    ys = [cy - 170, cy, cy + 170]
    d.line((x0, ys[0], x0, ys[2]), fill=255, width=lw)
    for i, y in enumerate(ys):
        end = x1 if i != 1 else x1 - 70
        d.line((x0, y, end, y), fill=255, width=lw)
        d.ellipse((end - 52, y - 52, end + 52, y + 52), fill=255)
        d.ellipse((end - 22, y - 22, end + 22, y + 22), fill=0)  # hollow node
    for y in (ys[0], ys[2]):
        d.ellipse((x0 - lw // 2, y - lw // 2, x0 + lw // 2, y + lw // 2), fill=255)
    return m


def render(debug=False):
    bg = gradient((S, S), (18, 12, 46, 255), (52, 20, 110, 255))
    base = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    base.paste(bg, (0, 0), tile_mask())

    gm = glyph_mask()
    fg = gradient((S, S), (196, 120, 255, 255), (64, 214, 255, 255), 45)
    # glow
    glow = Image.new("RGBA", (S, S), (150, 90, 255, 0))
    glow.putalpha(gm.filter(ImageFilter.GaussianBlur(38)).point(lambda v: int(v * 0.85)))
    base.alpha_composite(glow)
    g = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    g.paste(fg, (0, 0), gm)
    base.alpha_composite(g)

    if debug:
        d = ImageDraw.Draw(base)
        bx, by, br = S - 210, S - 210, 160
        d.ellipse((bx - br - 18, by - br - 18, bx + br + 18, by + br + 18), fill=(18, 12, 46, 255))
        d.ellipse((bx - br, by - br, bx + br, by + br), fill=(255, 176, 32, 255))
        # wrench-free "bug" mark: body + legs
        d.ellipse((bx - 55, by - 75, bx + 55, by + 85), fill=(40, 20, 10, 255))
        d.ellipse((bx - 38, by - 120, bx + 38, by - 55), fill=(40, 20, 10, 255))
        for dy in (-30, 15, 60):
            d.line((bx - 105, by + dy - 15, bx - 50, by + dy), fill=(40, 20, 10, 255), width=20)
            d.line((bx + 50, by + dy, bx + 105, by + dy - 15), fill=(40, 20, 10, 255), width=20)
        d.line((bx, by - 50, bx, by + 80), fill=(255, 176, 32, 255), width=12)
    return base


OUT.mkdir(parents=True, exist_ok=True)
for name, dbg in (("eraone-v2", False), ("eraone-v2-debug", True)):
    img = render(dbg)
    img.resize((256, 256), Image.LANCZOS).save(OUT / f"{name}.png")
    imgs = [img.resize((s, s), Image.LANCZOS) for s in SIZES]
    imgs[-1].save(OUT / f"{name}.ico", sizes=[(s, s) for s in SIZES], append_images=imgs[:-1])
print("ok")
