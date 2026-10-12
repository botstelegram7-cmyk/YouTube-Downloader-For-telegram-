#!/usr/bin/env python3
"""Generate the bot's images: the start picture and the animated progress GIFs.

Run it whenever the artwork or the wording changes:

    python scripts/make_assets.py            # rewrite assets/
    python scripts/make_assets.py --check    # verify they are present and valid

The GIFs are drawn from code rather than committed blind, so they can be regenerated
identically (no binary blob nobody can edit) and their file size stays small enough to
travel through Telegram quickly. ``assets/start.jpg`` is the one hand-made picture — it is
only resized and re-encoded here, never redrawn.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageSequence

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"

#: Telegram renders a photo at most this large; bigger files only cost bandwidth.
START_SIZE = (1280, 720)
GIF_SIZE = (480, 270)

# Palette, kept in step with the start picture.
BG_TOP = (14, 19, 43)
BG_BOTTOM = (38, 22, 74)
ACCENT = (56, 214, 245)  # cyan
ACCENT_DIM = (40, 120, 150)
TRACK = (44, 52, 86)
TEXT = (238, 243, 255)
MUTED = (150, 163, 200)

FRAMES = 36
FRAME_MS = 60


def _background() -> Image.Image:
    """The vertical gradient every frame shares."""
    base = Image.new("RGB", GIF_SIZE, BG_TOP)
    draw = ImageDraw.Draw(base)
    for y in range(GIF_SIZE[1]):
        blend = y / (GIF_SIZE[1] - 1)
        draw.line(
            [(0, y), (GIF_SIZE[0], y)],
            fill=tuple(round(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * blend) for i in range(3)),
        )
    # Faint diagonal stripes for a bit of depth.
    overlay = Image.new("RGBA", GIF_SIZE, (255, 255, 255, 0))
    stripes = ImageDraw.Draw(overlay)
    for x in range(-GIF_SIZE[1], GIF_SIZE[0], 46):
        stripes.line([(x, GIF_SIZE[1]), (x + GIF_SIZE[1], 0)], fill=(255, 255, 255, 8), width=2)
    return Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")


def _load_font(size: int):
    """A sans font at ``size``, falling back to Pillow's bitmap default."""
    from PIL import ImageFont

    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf", "Arial.ttf", "LiberationSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _progress_gif(label: str, *, chevrons: bool = True) -> Image.Image:
    """An endless sweeping bar with a moving highlight — the 'work in progress' animation."""
    base = _background()
    title_font = _load_font(30)
    label_font = _load_font(19)
    chip_font = _load_font(17)

    bar_left, bar_right = 60, GIF_SIZE[0] - 60
    bar_top, bar_height = 168, 14
    radius = bar_height // 2

    frames: list[Image.Image] = []
    for index in range(FRAMES):
        phase = index / FRAMES
        frame = base.copy()
        draw = ImageDraw.Draw(frame)

        # Title with an animated three-dot ellipsis.
        dots = "." * (1 + (index // (FRAMES // 6)) % 3)
        title = f"{label}{dots}"
        text_width = draw.textlength(title, font=title_font)
        draw.text(((GIF_SIZE[0] - text_width) / 2, 58), title, font=title_font, fill=TEXT)

        draw.text(
            (60, 108),
            "Turbo mode · parallel connections",
            font=label_font,
            fill=MUTED,
        )

        # Track + moving head.
        draw.rounded_rectangle(
            [bar_left, bar_top, bar_right, bar_top + bar_height], radius=radius, fill=TRACK
        )
        span = bar_right - bar_left
        head = bar_left + span * (0.5 + 0.5 * math.sin(phase * 2 * math.pi))
        width = 96
        start = max(bar_left, min(bar_right - width, head - width / 2))
        draw.rounded_rectangle(
            [start, bar_top, start + width, bar_top + bar_height],
            radius=radius,
            fill=ACCENT,
        )
        # A brighter core inside the head, like a comet.
        core_start = max(bar_left, min(bar_right - width / 3, head - width / 6))
        draw.rounded_rectangle(
            [core_start, bar_top + 4, core_start + width / 3, bar_top + bar_height - 4],
            radius=radius - 3,
            fill=(226, 252, 255),
        )

        # Bottom row: a marching chevron on the left, a short status chip on the right.
        # The real numbers (percent, speed, ETA) live in the message caption — a GIF cannot
        # carry live text, so it must not pretend to.
        if chevrons:
            chevron_shift = int(5 * math.sin(phase * 2 * math.pi))
            for offset, alpha in ((0, ACCENT), (15, ACCENT_DIM), (30, ACCENT_DIM)):
                x = 60 + offset + chevron_shift
                draw.polygon(
                    [(x, bar_top + 40), (x + 9, bar_top + 46), (x, bar_top + 52)],
                    fill=alpha,
                )
        chip_text = "live ETA + speed"
        chip_width = draw.textlength(chip_text, font=chip_font)
        chip_right = GIF_SIZE[0] - 60
        draw.rounded_rectangle(
            [chip_right - chip_width - 26, bar_top + 32, chip_right, bar_top + 60],
            radius=14,
            fill=(26, 34, 64),
            outline=(58, 74, 122),
        )
        draw.text((chip_right - chip_width - 13, bar_top + 39), chip_text, font=chip_font, fill=MUTED)

        frames.append(frame)

    frames[0].save(
        ASSETS / "tmp.gif",
        save_all=True,
        append_images=frames[1:],
        duration=FRAME_MS,
        loop=0,
        optimize=True,
    )
    return Image.open(ASSETS / "tmp.gif")


def _write_gif(name: str, label: str, *, chevrons: bool) -> Path:
    """Render one animation and save it with a 128-colour palette (small + smooth)."""
    animation = _progress_gif(label, chevrons=chevrons)
    target = ASSETS / name
    palette_frames = [
        frame.convert("RGB").quantize(colors=128, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG)
        for frame in ImageSequence.Iterator(animation)
    ]
    palette_frames[0].save(
        target,
        save_all=True,
        append_images=palette_frames[1:],
        duration=FRAME_MS,
        loop=0,
        optimize=True,
        disposal=2,
    )
    temporary = ASSETS / "tmp.gif"
    temporary.unlink(missing_ok=True)
    return target


def write_start_image(source: Path | None) -> Path | None:
    """Resize/re-encode the start picture to a size Telegram is happy with."""
    target = ASSETS / "start.jpg"
    if source is None or not source.exists():
        return target if target.exists() else None
    with Image.open(source) as image:
        image = image.convert("RGB")
        image.thumbnail(START_SIZE, Image.Resampling.LANCZOS)
        image.save(target, "JPEG", quality=88, optimize=True, progressive=True)
    return target


def describe(path: Path) -> str:
    if not path.exists():
        return f"{path.name}: MISSING"
    if path.suffix == ".gif":
        with Image.open(path) as image:
            frames = getattr(image, "n_frames", 1)
            return (
                f"{path.name}: {image.size[0]}×{image.size[1]}, {frames} frames, "
                f"{path.stat().st_size / 1024:.0f} KB"
            )
    with Image.open(path) as image:
        return f"{path.name}: {image.size[0]}×{image.size[1]}, {path.stat().st_size / 1024:.0f} KB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the bot's start image and progress GIFs")
    parser.add_argument("--check", action="store_true", help="only verify that the assets exist and are valid")
    parser.add_argument("--start-source", type=Path, help="a picture to use as assets/start.jpg")
    args = parser.parse_args(argv)

    ASSETS.mkdir(exist_ok=True)

    if args.check:
        missing = []
        for name in ("start.jpg", "progress.gif", "upload.gif"):
            path = ASSETS / name
            print(describe(path))
            if not path.exists():
                missing.append(name)
        if missing:
            print(f"\nMissing: {', '.join(missing)} — run python scripts/make_assets.py", file=sys.stderr)
            return 1
        return 0

    start = write_start_image(args.start_source)
    if start is not None:
        print(describe(start))
    _write_gif("progress.gif", "DOWNLOADING", chevrons=True)
    print(describe(ASSETS / "progress.gif"))
    _write_gif("upload.gif", "UPLOADING TO TELEGRAM", chevrons=True)
    print(describe(ASSETS / "upload.gif"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
