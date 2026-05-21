from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from supaclip.core.edl import EDLOSTCue, OSTPosition, OSTStyle


RGBA = tuple[int, int, int, int]


@dataclass(frozen=True)
class CaptionStyle:
    bg: RGBA
    fg: RGBA
    stroke: RGBA | None
    stroke_width: int
    font_size: int
    padding_x: int
    padding_y: int
    corner_radius: int
    line_spacing: int
    uppercase: bool


STYLE_PRESETS: dict[OSTStyle, CaptionStyle] = {
    "dark": CaptionStyle(
        bg=(0, 0, 0, 220),
        fg=(255, 255, 255, 255),
        stroke=(0, 0, 0, 230),
        stroke_width=3,
        font_size=72,
        padding_x=36,
        padding_y=22,
        corner_radius=22,
        line_spacing=8,
        uppercase=True,
    ),
    "light": CaptionStyle(
        bg=(255, 255, 255, 235),
        fg=(15, 15, 15, 255),
        stroke=None,
        stroke_width=0,
        font_size=72,
        padding_x=36,
        padding_y=22,
        corner_radius=22,
        line_spacing=8,
        uppercase=True,
    ),
    "yellow_punch": CaptionStyle(
        bg=(0, 0, 0, 220),
        fg=(255, 214, 0, 255),
        stroke=(0, 0, 0, 230),
        stroke_width=3,
        font_size=80,
        padding_x=38,
        padding_y=24,
        corner_radius=22,
        line_spacing=8,
        uppercase=True,
    ),
    "red_alert": CaptionStyle(
        bg=(0, 0, 0, 220),
        fg=(255, 59, 48, 255),
        stroke=(0, 0, 0, 230),
        stroke_width=3,
        font_size=72,
        padding_x=36,
        padding_y=22,
        corner_radius=22,
        line_spacing=8,
        uppercase=True,
    ),
    "pink_reveal": CaptionStyle(
        bg=(0, 0, 0, 220),
        fg=(255, 43, 214, 255),
        stroke=(0, 0, 0, 230),
        stroke_width=3,
        font_size=72,
        padding_x=36,
        padding_y=22,
        corner_radius=22,
        line_spacing=8,
        uppercase=True,
    ),
}


POSITION_Y_FRACTION: dict[OSTPosition, float] = {
    "top": 0.12,
    "middle": 0.50,
    "bottom": 0.78,
}


DEFAULT_FONT_CANDIDATES: tuple[str, ...] = (
    "/usr/share/fonts/truetype/open-sans/OpenSans-ExtraBold.ttf",
    "/usr/share/fonts/opentype/urw-base35/NimbusSans-Bold.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)


@dataclass(frozen=True)
class OSTRender:
    """One rendered OST cue ready to be overlaid by ffmpeg."""
    cue_index: int
    png_path: Path
    x: int
    y: int
    start: float
    end: float


def _resolve_font(fontfile: str | None) -> str:
    if fontfile:
        if Path(fontfile).exists():
            return fontfile
        raise FileNotFoundError(f"OST fontfile not found: {fontfile}")
    for candidate in DEFAULT_FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise FileNotFoundError(
        "No bold OST font found; install Open Sans / DejaVu / Liberation "
        "or pass --fontfile pointing at a TTF."
    )


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Greedy word-wrap by pixel width using the given font."""
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    cur = words[0]
    for w in words[1:]:
        trial = f"{cur} {w}"
        if font.getlength(trial) <= max_width:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


def _measure_block(
    lines: list[str], font: ImageFont.FreeTypeFont, line_spacing: int
) -> tuple[int, int, int]:
    """Return (block_w, block_h, line_h)."""
    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    widths = [int(font.getlength(line)) for line in lines]
    block_w = max(widths) if widths else 0
    block_h = line_h * len(lines) + line_spacing * max(0, len(lines) - 1)
    return block_w, block_h, line_h


def render_caption_png(
    text: str,
    style_name: OSTStyle,
    out_w: int,
    fontfile: str | None,
    dest: Path,
) -> tuple[int, int]:
    """Render the caption to a transparent PNG. Returns (png_w, png_h)."""
    style = STYLE_PRESETS[style_name]
    display_text = text.upper() if style.uppercase else text
    font_path = _resolve_font(fontfile)

    max_text_width = int(out_w * 0.86) - 2 * style.padding_x
    font_size = style.font_size
    while font_size >= 32:
        font = ImageFont.truetype(font_path, font_size)
        lines = _wrap_text(display_text, font, max_text_width)
        block_w, _, _ = _measure_block(lines, font, style.line_spacing)
        if block_w <= max_text_width or font_size == 32:
            break
        font_size -= 4

    font = ImageFont.truetype(font_path, font_size)
    lines = _wrap_text(display_text, font, max_text_width)
    block_w, block_h, line_h = _measure_block(lines, font, style.line_spacing)

    png_w = block_w + 2 * style.padding_x
    png_h = block_h + 2 * style.padding_y

    img = Image.new("RGBA", (png_w, png_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        (0, 0, png_w - 1, png_h - 1),
        radius=style.corner_radius,
        fill=style.bg,
    )

    y = style.padding_y
    for line in lines:
        line_w = int(font.getlength(line))
        x = (png_w - line_w) // 2
        if style.stroke is not None and style.stroke_width > 0:
            draw.text(
                (x, y),
                line,
                font=font,
                fill=style.fg,
                stroke_width=style.stroke_width,
                stroke_fill=style.stroke,
            )
        else:
            draw.text((x, y), line, font=font, fill=style.fg)
        y += line_h + style.line_spacing

    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, format="PNG")
    return png_w, png_h


def _png_filename(cue: EDLOSTCue, out_w: int, out_h: int) -> str:
    key = f"{cue.text}|{cue.style}|{cue.position}|{out_w}x{out_h}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"ost-{digest}.png"


def render_ost_pngs(
    cues: list[EDLOSTCue],
    out_w: int,
    out_h: int,
    cache_dir: Path,
    fontfile: str | None = None,
) -> list[OSTRender]:
    """Render every OST cue to a PNG and return placement info."""
    renders: list[OSTRender] = []
    for i, cue in enumerate(cues):
        png_path = cache_dir / _png_filename(cue, out_w, out_h)
        png_w, png_h = render_caption_png(
            text=cue.text,
            style_name=cue.style,
            out_w=out_w,
            fontfile=fontfile,
            dest=png_path,
        )
        x = (out_w - png_w) // 2
        if cue.position == "middle":
            y = (out_h - png_h) // 2
        else:
            anchor = POSITION_Y_FRACTION[cue.position]
            y = int(out_h * anchor) - png_h // 2
        y = max(0, min(y, out_h - png_h))
        renders.append(OSTRender(
            cue_index=i,
            png_path=png_path,
            x=x,
            y=y,
            start=cue.start,
            end=cue.end,
        ))
    return renders


def build_ost_overlay_chain(
    renders: list[OSTRender],
    input_indices: list[int],
    base_label: str,
    final_label: str,
) -> list[str]:
    """Build ffmpeg filter_complex chains that overlay each OST PNG.

    base_label is the upstream video label (e.g. "[vann]").
    Each PNG is supplied as an extra ffmpeg input; input_indices[i] is the
    ffmpeg -i index for renders[i].png_path. Returns a list of filter chains.
    """
    if not renders:
        return [f"{base_label}null{final_label}"]
    if len(renders) != len(input_indices):
        raise ValueError("renders and input_indices length mismatch")

    chains: list[str] = []
    cur = base_label
    for n, (r, idx) in enumerate(zip(renders, input_indices)):
        is_last = n == len(renders) - 1
        out_label = final_label if is_last else f"[vost{n}]"
        chains.append(
            f"{cur}[{idx}:v]"
            f"overlay=x={r.x}:y={r.y}:format=auto"
            f":enable='between(t,{r.start:.3f},{r.end:.3f})'"
            f"{out_label}"
        )
        cur = out_label
    return chains
