"""Cyberpunk palette, the color-cycling clock, and pre-rendered neon artwork."""
from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# --------------------------------------------------------------------------- palette
BG = "#07060f"
SIDEBAR = "#0a0817"
PANEL = "#0e0b1f"
CARD = "#151031"
CARD_HI = "#1e1745"
BORDER = "#2a2154"
GRID = "#151129"
TEXT = "#ece9ff"
MUTED = "#8c86ba"
DIM = "#565083"

CYAN = "#00e5ff"
PURPLE = "#b026ff"
PINK = "#ff2a6d"
GREEN = "#39ff88"
AMBER = "#ffd23f"
RED = "#ff4f7b"

# The UI's neon colors ping-pong through these, so everything drifts blue -> purple -> pink.
STOPS = [CYAN, PURPLE, PINK, PURPLE, CYAN]
CYCLE_SECONDS = 7.0

FONT_UI = "Bahnschrift"
FONT_UI_BOLD = "Bahnschrift SemiBold"
FONT_MONO = "Cascadia Mono"
FONT_ICON = "Segoe Fluent Icons"

GLYPH = {
    "text": "\ue8a5", "image": "\ueb9f", "media": "\ue714", "audio": "\ue8d6",
    "upload": "\ue898", "folder": "\ue838", "check": "\ue73e", "error": "\ue783",
    "close": "\ue711", "bolt": "\ue945", "switch": "\ue8ab", "add": "\ue710", "edit": "\ue70f",
}

_FONTS_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"


def pick_fonts(families: set[str]) -> None:
    """Fall back gracefully on PCs without the Windows 10/11 fonts."""
    global FONT_UI, FONT_UI_BOLD, FONT_MONO, FONT_ICON
    if "Bahnschrift" not in families:
        FONT_UI = FONT_UI_BOLD = "Segoe UI"
    elif "Bahnschrift SemiBold" not in families:
        FONT_UI_BOLD = "Bahnschrift"
    if "Cascadia Mono" not in families:
        FONT_MONO = "Consolas"
    if "Segoe Fluent Icons" not in families:
        FONT_ICON = "Segoe MDL2 Assets"


# --------------------------------------------------------------------------- colors

def _rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _hex(c) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(v))) for v in c)


def mix(a: str, b: str, t: float) -> str:
    ra, rb = _rgb(a), _rgb(b)
    return _hex([x + (y - x) * t for x, y in zip(ra, rb)])


def neon(phase: float) -> str:
    """Color at a point of the cycle (any float; wraps around)."""
    p = (phase % 1.0) * (len(STOPS) - 1)
    i = min(int(p), len(STOPS) - 2)
    return mix(STOPS[i], STOPS[i + 1], p - i)


def shade(h: str, factor: float, base: str = BG) -> str:
    """Blend a neon color toward the background (factor 0 = background, 1 = full neon)."""
    return mix(base, h, factor)


# --------------------------------------------------------------------------- artwork

def _font(name: str, size: int, variation: str | None = None) -> ImageFont.FreeTypeFont:
    try:
        f = ImageFont.truetype(str(_FONTS_DIR / name), size)
        if variation:
            try:
                f.set_variation_by_name(variation)
            except Exception:
                pass
        return f
    except Exception:
        return ImageFont.load_default(size)


def gradient(width: int, height: int, stops=STOPS) -> Image.Image:
    width = max(2, width)
    row = Image.new("RGB", (width, 1))
    px = row.load()
    for x in range(width):
        px[x, 0] = _rgb(neon(x / width) if stops is STOPS else _interp(stops, x / (width - 1)))
    return row.resize((width, max(1, height)), Image.Resampling.NEAREST)


def _interp(stops, t: float) -> str:
    p = t * (len(stops) - 1)
    i = min(int(p), len(stops) - 2)
    return mix(stops[i], stops[i + 1], p - i)


def logo_frames(scale: float, n: int = 48) -> list[Image.Image]:
    """The sidebar logo: glowing NEON wordmark whose colors flow through the cycle."""
    s = scale
    big = _font("bahnschrift.ttf", int(46 * s), "Bold")
    small = _font("bahnschrift.ttf", int(12.5 * s), "SemiBold")
    w, h = int(236 * s), int(104 * s)
    word, sub = "NEON", "C O N V E R T E R"
    pad = int(14 * s)

    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    d.text((pad, int(10 * s)), word, font=big, fill=255)
    bbox = d.textbbox((pad, int(10 * s)), word, font=big)
    glow_mask = mask.filter(ImageFilter.GaussianBlur(9 * s))
    glow_mask = glow_mask.point(lambda v: min(255, int(v * 2.2)))
    tight = mask.filter(ImageFilter.GaussianBlur(2.2 * s))

    sub_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(sub_layer).text((pad + int(3 * s), bbox[3] + int(10 * s)), sub, font=small,
                                   fill=_rgb(TEXT) + (210,))
    bar_y = bbox[3] + int(4 * s)

    frames = []
    bg = Image.new("RGB", (w, h), _rgb(SIDEBAR))
    white = Image.new("RGB", (w, h), (255, 255, 255))
    for k in range(n):
        ph = k / n
        # horizontal gradient that slides with the phase
        grad = Image.new("RGB", (w, 1))
        gp = grad.load()
        for x in range(w):
            gp[x, 0] = _rgb(neon(ph + x / w * 0.45))
        grad = grad.resize((w, h), Image.Resampling.NEAREST)
        frame = bg.copy()
        frame.paste(grad, (0, 0), glow_mask.point(lambda v: int(v * 0.55)))
        frame.paste(grad, (0, 0), tight)
        frame.paste(Image.blend(grad, white, 0.4), (0, 0), mask)  # hot white-ish core
        # thin underline bar
        ImageDraw.Draw(frame).rectangle([pad, bar_y, bbox[2], bar_y + max(1, int(2 * s))],
                                        fill=_rgb(neon(ph + 0.3)))
        frame.paste(sub_layer, (0, 0), sub_layer)
        frames.append(frame)
    return frames


def app_icon(size: int = 256) -> Image.Image:
    """Square app icon: dark tile, neon gradient frame, glowing switch arrows.

    Drawn at 4x and downsampled for clean edges. Small sizes (taskbar, Explorer lists) get
    bolder strokes, a heavier frame and less glow so they stay readable at 16-32 px.
    """
    ss = 4
    S = size * ss
    s = S / 256
    small = size <= 32
    tiny = size <= 20
    medium = 32 < size <= 64
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    inset = 4 * s if small else 8 * s
    tile = Image.new("L", (S, S), 0)
    ImageDraw.Draw(tile).rounded_rectangle([inset, inset, S - inset, S - inset], radius=(44 if small else 52) * s,
                                           fill=255)
    img.paste(Image.new("RGBA", (S, S), _rgb("#0c0820") + (255,)), (0, 0), tile)

    # diagonal cyan -> purple -> pink gradient (built small, then scaled up smoothly)
    g = 64
    diag = Image.new("RGB", (g, g))
    gp = diag.load()
    for y in range(g):
        for x in range(g):
            gp[x, y] = _rgb(_interp([CYAN, PURPLE, PINK], (x + y) / (2 * g - 2)))
    diag = diag.resize((S, S), Image.Resampling.BICUBIC)

    ring_w = (22 if tiny else 16 if small else 12 if medium else 9) * s
    ring = Image.new("L", (S, S), 0)
    r_in = inset + (4 if small else 6) * s
    ImageDraw.Draw(ring).rounded_rectangle([r_in, r_in, S - r_in, S - r_in], radius=(40 if small else 46) * s,
                                           outline=255, width=max(ss, int(ring_w)))
    if not tiny:
        img.paste(diag, (0, 0), ring.filter(ImageFilter.GaussianBlur((3 if small else 7) * s)))
    img.paste(diag, (0, 0), ring)

    glyph = Image.new("L", (S, S), 0)
    gsize = int((178 if tiny else 168 if small else 150) * s)
    gf = _font("SegoeIcons.ttf", gsize) if (_FONTS_DIR / "SegoeIcons.ttf").exists() else _font("segmdl2.ttf", gsize)
    gd = ImageDraw.Draw(glyph)
    bb = gd.textbbox((0, 0), GLYPH["switch"], font=gf)
    gw, gh = bb[2] - bb[0], bb[3] - bb[1]
    gd.text(((S - gw) / 2 - bb[0], (S - gh) / 2 - bb[1]), GLYPH["switch"], font=gf, fill=255)
    if small or medium:  # thicken the arrow strokes so they survive the downscale
        glyph = glyph.filter(ImageFilter.MaxFilter(9 if tiny else 7 if small else 5))
    if not tiny:
        img.paste(diag, (0, 0), glyph.filter(ImageFilter.GaussianBlur((4 if small else 10) * s)))
    img.paste(Image.new("RGB", (S, S), (255, 255, 255)), (0, 0), glyph)
    return img.resize((size, size), Image.Resampling.LANCZOS)


ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
