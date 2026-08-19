"""Compose slide PNGs: background photo + outlined text, TikTok-sticker style.

Text is drawn deterministically here rather than generated, which is the whole
cost-and-quality argument:

    image model renders text   ~5 retries to get legible letters, drifts
                               between slides, ~$0.67 per 7-slide deck
    text drawn here            pixel-identical across every slide, rewriting
                               a hook costs nothing, $0.00

The look is copied from what actually wins in this niche: heavy sans, pure
white fill, thick black stroke, no container box, sitting over real photography.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from . import imagery
from .imagery import SLIDE_H, SLIDE_W

# Heaviest reliable system faces, best first. Swap by setting SLIDE_FONT.
FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Bold.ttf",
]

MARGIN = 72
MAX_FONT = 108
MIN_FONT = 44
STROKE_RATIO = 0.085          # stroke thickness as a fraction of font size
SCRIM_ALPHA = 90              # darkening over the photo so white type holds


@dataclass
class SlideSpec:
    index: int
    role: str
    text: str
    query: Optional[str] = None


def _font_path() -> str:
    import os

    override = os.environ.get("SLIDE_FONT", "").strip()
    if override and Path(override).exists():
        return override
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise RuntimeError(
        "No heavy sans-serif font found. Set SLIDE_FONT in .env to a .ttf path."
    )


def _gradient(seed: int) -> Image.Image:
    """Fallback ground when no stock photo is available.

    Deep desaturated blues and slates, matching the reference posts' colour
    world rather than introducing a palette of our own.
    """
    tops = [(18, 26, 40), (26, 30, 38), (16, 32, 34), (30, 26, 34)]
    bottoms = [(46, 62, 88), (58, 62, 74), (38, 68, 68), (62, 54, 70)]
    top = tops[seed % len(tops)]
    bottom = bottoms[seed % len(bottoms)]

    base = Image.new("RGB", (1, SLIDE_H))
    draw = ImageDraw.Draw(base)
    for y in range(SLIDE_H):
        t = y / SLIDE_H
        draw.point(
            (0, y),
            fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
        )
    return base.resize((SLIDE_W, SLIDE_H), Image.BILINEAR)


def _cover(img: Image.Image) -> Image.Image:
    """Scale and centre-crop to fill the slide without distortion."""
    img = img.convert("RGB")
    scale = max(SLIDE_W / img.width, SLIDE_H / img.height)
    new = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
    img = img.resize(new, Image.LANCZOS)
    left = (img.width - SLIDE_W) // 2
    top = (img.height - SLIDE_H) // 2
    return img.crop((left, top, left + SLIDE_W, top + SLIDE_H))


def _background(spec: SlideSpec) -> Image.Image:
    query = spec.query or imagery.query_for(spec.role)
    path = imagery.fetch_background(query, spec.index)
    if path and path.exists():
        try:
            img = _cover(Image.open(path))
            # Desaturate slightly: the reference posts are all near-monochrome,
            # which is also what keeps white type readable.
            img = ImageEnhance.Color(img).enhance(0.55)
            return img
        except OSError:
            pass
    return _gradient(spec.index)


def wrap_text(text: str, width: int) -> List[str]:
    """Wrap to `width` characters, respecting explicit breaks and keeping
    numbers intact.

    Two rules that plain `textwrap.wrap` gets wrong for slide copy:

    `break_on_hyphens=False` — the default splits "$150,000-$200,000" across
    lines as "$150,000-$20" / "0,000". That renders a salary that reads as
    wrong, with no error to notice. Observed on the first real render.

    A literal "|" is an explicit line break, matching the reference format
    where the role sits on one line and the figure directly beneath it.
    """
    segments = [seg.strip() for seg in text.split("|")]
    lines: List[str] = []
    for seg in segments:
        if not seg:
            continue
        lines.extend(
            textwrap.wrap(
                seg,
                width=max(6, width),
                break_on_hyphens=False,
                break_long_words=False,
            )
            or [seg]
        )
    return lines or [text]


def _fit_text(
    draw: ImageDraw.ImageDraw, text: str, max_w: int, max_h: int
) -> Tuple[ImageFont.FreeTypeFont, List[str]]:
    """Largest font size at which the wrapped text fits the box."""
    path = _font_path()
    for size in range(MAX_FONT, MIN_FONT - 1, -4):
        font = ImageFont.truetype(path, size)
        # Estimate characters per line from average glyph width.
        avg = max(1.0, draw.textlength("ABCDEFGHIJabcdefghij", font=font) / 20)
        lines = wrap_text(text, int(max_w / avg))
        widest = max(draw.textlength(line, font=font) for line in lines)
        height = len(lines) * size * 1.16
        if widest <= max_w and height <= max_h:
            return font, lines
    font = ImageFont.truetype(path, MIN_FONT)
    return font, wrap_text(text, 24)


def render_slide(spec: SlideSpec, out_path: Path) -> Path:
    """Draw one slide and write it as a PNG."""
    canvas = _background(spec)

    # Scrim only behind the type band, not the whole frame, so the photograph
    # still reads at the top and bottom.
    scrim = Image.new("RGBA", (SLIDE_W, SLIDE_H), (0, 0, 0, 0))
    ImageDraw.Draw(scrim).rectangle(
        [0, int(SLIDE_H * 0.30), SLIDE_W, int(SLIDE_H * 0.70)],
        fill=(0, 0, 0, SCRIM_ALPHA),
    )
    scrim = scrim.filter(ImageFilter.GaussianBlur(60))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), scrim).convert("RGB")

    draw = ImageDraw.Draw(canvas)
    box_w = SLIDE_W - 2 * MARGIN
    font, lines = _fit_text(draw, spec.text, box_w, int(SLIDE_H * 0.34))

    line_h = font.size * 1.16
    total_h = line_h * len(lines)
    y = (SLIDE_H - total_h) / 2
    stroke = max(3, int(font.size * STROKE_RATIO))

    for line in lines:
        w = draw.textlength(line, font=font)
        draw.text(
            ((SLIDE_W - w) / 2, y),
            line,
            font=font,
            fill=(255, 255, 255),
            stroke_width=stroke,
            stroke_fill=(0, 0, 0),
        )
        y += line_h

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, "PNG", optimize=True)
    return out_path


def render_deck(specs: List[SlideSpec], out_dir: Path) -> List[Path]:
    """Render every slide in an idea. Returns the paths in slide order."""
    return [
        render_slide(spec, out_dir / f"slide-{spec.index:02d}.png")
        for spec in specs
    ]
