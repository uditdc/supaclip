from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from supaclip.core.edl import EDLOSTCue, EDLWatermark, OSTPosition, OSTStyle

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
    shadow: RGBA | None = None
    shadow_offset: tuple[int, int] = (0, 0)
    shadow_blur: int = 0
    gradient_to: RGBA | None = None
    accent: RGBA | None = None
    accent_width: int = 0
    left_align: bool = False


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
    "yellow_punch_shadow": CaptionStyle(
        bg=(0, 0, 0, 220),
        fg=(255, 214, 0, 255),
        stroke=(0, 0, 0, 235),
        stroke_width=3,
        font_size=80,
        padding_x=38,
        padding_y=24,
        corner_radius=22,
        line_spacing=8,
        uppercase=True,
        shadow=(0, 0, 0, 200),
        shadow_offset=(0, 5),
        shadow_blur=9,
    ),
    "gradient_dark": CaptionStyle(
        bg=(38, 38, 48, 235),
        fg=(255, 255, 255, 255),
        stroke=(0, 0, 0, 160),
        stroke_width=2,
        font_size=72,
        padding_x=38,
        padding_y=24,
        corner_radius=24,
        line_spacing=8,
        uppercase=True,
        shadow=(0, 0, 0, 170),
        shadow_offset=(0, 4),
        shadow_blur=7,
        gradient_to=(6, 6, 10, 235),
    ),
    "accent_bar": CaptionStyle(
        bg=(10, 10, 12, 225),
        fg=(255, 255, 255, 255),
        stroke=None,
        stroke_width=0,
        font_size=68,
        padding_x=34,
        padding_y=22,
        corner_radius=12,
        line_spacing=8,
        uppercase=True,
        shadow=(0, 0, 0, 150),
        shadow_offset=(0, 3),
        shadow_blur=6,
        accent=(255, 214, 0, 255),
        accent_width=12,
        left_align=True,
    ),
}


POSITION_Y_FRACTION: dict[OSTPosition, float] = {
    "top": 0.12,
    "middle": 0.50,
    "bottom": 0.78,
}


WATERMARK_MARGIN_FRACTION = 0.021

# Text is rasterized at SUPERSAMPLE× the target size then downscaled with LANCZOS
# so stroke edges and glyph anti-aliasing stay crisp instead of chunky.
SUPERSAMPLE = 2

# Preset pixel sizes are authored against this output height; at render time the
# whole style geometry scales by out_h / this so captions keep the same relative
# size at 720p/1080p/1440p/4K instead of shrinking on tall canvases.
CAPTION_REFERENCE_HEIGHT = 1920

OST_FONT_FIT_MIN = 32
OST_FONT_FIT_STEP = 4

# OST pop settles from this overshoot; slide travels this fraction of the card.
OST_POP_OVERSHOOT = 0.12
SLIDE_OFFSET_FRACTION = 0.6


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

# Bundled display face (SIL OFL); the deterministic default when no explicit
# fontfile is passed. See supaclip/assets/fonts/Anton-OFL.txt for the license.
BUNDLED_FONT_NAME = "Anton-Regular.ttf"


@dataclass(frozen=True)
class OSTRender:
    """One rendered OST cue ready to be overlaid by ffmpeg."""
    cue_index: int
    png_path: Path
    x: int
    y: int
    start: float
    end: float


@lru_cache(maxsize=1)
def _bundled_font_path() -> str | None:
    try:
        ref = resources.files("supaclip") / "assets" / "fonts" / BUNDLED_FONT_NAME
        path = Path(str(ref))
        return str(path) if path.exists() else None
    except (ModuleNotFoundError, FileNotFoundError, TypeError):
        return None


def _resolve_font(fontfile: str | None) -> str:
    """Font precedence: explicit fontfile → bundled asset → system font → raise.

    An explicit fontfile stays authoritative; the bundled face only becomes the
    default for callers that pass nothing, so it never silently restyles callers
    that already choose a font.
    """
    if fontfile:
        if Path(fontfile).exists():
            return fontfile
        raise FileNotFoundError(f"OST fontfile not found: {fontfile}")
    bundled = _bundled_font_path()
    if bundled is not None:
        return bundled
    for candidate in DEFAULT_FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise FileNotFoundError(
        "No bold OST font found; install Open Sans / DejaVu / Liberation "
        "or pass --fontfile pointing at a TTF."
    )


def _font_identity(font_path: str) -> str:
    """A stable short id for the resolved font, folded into PNG cache keys so a
    font change (bundled vs system vs explicit) never reuses a stale render."""
    return Path(font_path).name


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


def _res_scale(out_h: int | None) -> float:
    """Geometry multiplier that keeps preset sizes constant relative to height."""
    if not out_h:
        return 1.0
    return out_h / CAPTION_REFERENCE_HEIGHT


def _scale_style(style, factor: float):
    """Scale every pixel-space field of a caption/OST style by `factor`.

    Used both for resolution-aware sizing (factor = out_h/reference) and for
    supersampling (factor = SUPERSAMPLE). Colours and flags are untouched.
    """
    if factor == 1.0:
        return style

    def s(value: int, minimum: int = 0) -> int:
        return max(minimum, round(value * factor))

    updates: dict = {
        "font_size": s(style.font_size, 1),
        "padding_x": s(style.padding_x),
        "padding_y": s(style.padding_y),
        "corner_radius": s(style.corner_radius),
        "line_spacing": s(style.line_spacing),
        "stroke_width": s(style.stroke_width) if style.stroke_width else 0,
    }
    if style.shadow is not None:
        ox, oy = style.shadow_offset
        updates["shadow_offset"] = (round(ox * factor), round(oy * factor))
        updates["shadow_blur"] = s(style.shadow_blur)
    field_names = {f.name for f in dataclasses.fields(style)}
    if "accent_width" in field_names and getattr(style, "accent_width", 0):
        updates["accent_width"] = s(style.accent_width)
    return dataclasses.replace(style, **updates)


def _draw_box(img: Image.Image, style) -> None:
    """Paint the (optionally gradient / accent-barred) rounded background box.

    `style` is expressed at the image's own (supersampled) scale.
    """
    w, h = img.size
    if style.bg[3] == 0 and style.gradient_to is None:
        return

    if style.gradient_to is not None:
        top, bottom = style.bg, style.gradient_to
        column = Image.new("RGBA", (1, h))
        px = column.load()
        span = max(1, h - 1)
        for yy in range(h):
            t = yy / span
            px[0, yy] = tuple(round(top[c] + (bottom[c] - top[c]) * t) for c in range(4))
        box = column.resize((w, h))
    else:
        box = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(box).rounded_rectangle(
            (0, 0, w - 1, h - 1), radius=style.corner_radius, fill=style.bg,
        )
        img.alpha_composite(box)
        _draw_accent(img, style)
        return

    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, w - 1, h - 1), radius=style.corner_radius, fill=255,
    )
    img.paste(box, (0, 0), mask)
    _draw_accent(img, style)


def _draw_accent(img: Image.Image, style) -> None:
    if style.accent is None or style.accent_width <= 0:
        return
    w, h = img.size
    accent_mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(accent_mask).rounded_rectangle(
        (0, 0, w - 1, h - 1), radius=style.corner_radius, fill=255,
    )
    ImageDraw.Draw(accent_mask).rectangle((style.accent_width, 0, w - 1, h - 1), fill=0)
    solid = Image.new("RGBA", (w, h), style.accent)
    img.paste(solid, (0, 0), accent_mask)


def _paint_with_shadow(img: Image.Image, style, draw_glyphs) -> None:
    """Draw glyphs onto `img` with an optional blurred drop shadow underneath.

    `draw_glyphs(draw, dx, dy, forced_color)` renders every glyph offset by
    (dx, dy); when `forced_color` is set it paints the shadow silhouette,
    otherwise the real stroke+fill. Style is at the image's scale.
    """
    if style.shadow is not None:
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ox, oy = style.shadow_offset
        draw_glyphs(ImageDraw.Draw(layer), ox, oy, style.shadow)
        if style.shadow_blur > 0:
            layer = layer.filter(ImageFilter.GaussianBlur(style.shadow_blur))
        img.alpha_composite(layer)
    draw_glyphs(ImageDraw.Draw(img), 0, 0, None)


def render_caption_png(
    text: str,
    style_name: OSTStyle,
    out_w: int,
    fontfile: str | None,
    dest: Path,
    out_h: int | None = None,
) -> tuple[int, int]:
    """Render the caption to a transparent PNG. Returns (png_w, png_h)."""
    base = _scale_style(STYLE_PRESETS[style_name], _res_scale(out_h))
    display_text = text.upper() if base.uppercase else text
    font_path = _resolve_font(fontfile)

    extra_left = base.accent_width if base.left_align else 0
    max_text_width = int(out_w * 0.86) - 2 * base.padding_x - extra_left
    min_font = max(8, round(OST_FONT_FIT_MIN * _res_scale(out_h)))
    step = max(1, round(OST_FONT_FIT_STEP * _res_scale(out_h)))

    size = base.font_size
    while size > min_font:
        font = ImageFont.truetype(font_path, size)
        lines = _wrap_text(display_text, font, max_text_width)
        block_w, _, _ = _measure_block(lines, font, base.line_spacing)
        if block_w <= max_text_width:
            break
        size -= step
    size = max(min_font, size)

    ss = SUPERSAMPLE
    style = _scale_style(dataclasses.replace(base, font_size=size), ss)
    font = ImageFont.truetype(font_path, style.font_size)
    lines = _wrap_text(display_text, font, max_text_width * ss)
    block_w, block_h, line_h = _measure_block(lines, font, style.line_spacing)

    accent_ss = style.accent_width if style.left_align else 0
    png_w = block_w + 2 * style.padding_x + accent_ss
    png_h = block_h + 2 * style.padding_y

    img = Image.new("RGBA", (png_w, png_h), (0, 0, 0, 0))
    _draw_box(img, style)

    positioned: list[tuple[int, int, str]] = []
    y = style.padding_y
    for line in lines:
        line_w = int(font.getlength(line))
        x = style.padding_x + accent_ss if style.left_align else (png_w - line_w) // 2
        positioned.append((x, y, line))
        y += line_h + style.line_spacing

    def draw_glyphs(draw, dx, dy, forced):
        for x, ly, line in positioned:
            if forced is not None:
                draw.text((x + dx, ly + dy), line, font=font, fill=forced,
                          stroke_width=style.stroke_width, stroke_fill=forced)
            elif style.stroke is not None and style.stroke_width > 0:
                draw.text((x, ly), line, font=font, fill=style.fg,
                          stroke_width=style.stroke_width, stroke_fill=style.stroke)
            else:
                draw.text((x, ly), line, font=font, fill=style.fg)

    _paint_with_shadow(img, style, draw_glyphs)

    final_w, final_h = round(png_w / ss), round(png_h / ss)
    img = img.resize((final_w, final_h), Image.LANCZOS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, format="PNG")
    return final_w, final_h


def _png_filename(cue: EDLOSTCue, out_w: int, out_h: int, font_id: str) -> str:
    key = (f"{cue.text}|{cue.style}|{cue.position}|{out_w}x{out_h}"
           f"|{font_id}|ss{SUPERSAMPLE}")
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"ost-{digest}.png"


def _place_ost(out_w: int, out_h: int, png_w: int, png_h: int, position: OSTPosition) -> tuple[int, int]:
    x = (out_w - png_w) // 2
    if position == "middle":
        y = (out_h - png_h) // 2
    else:
        y = int(out_h * POSITION_Y_FRACTION[position]) - png_h // 2
    return x, max(0, min(y, out_h - png_h))


def render_ost_pngs(
    cues: list[EDLOSTCue],
    out_w: int,
    out_h: int,
    cache_dir: Path,
    fontfile: str | None = None,
    fps: int = 30,
) -> list[OSTRender]:
    """Render every OST cue to a PNG and return placement info.

    A cue with `animate_in`/`animate_out` emits several placement windows over a
    shared base PNG (scaled / alpha / slid variants) instead of one; a cue with
    no animation emits exactly one render, byte-identical to the un-animated
    path.
    """
    font_id = _font_identity(_resolve_font(fontfile))
    renders: list[OSTRender] = []
    for i, cue in enumerate(cues):
        base_png = cache_dir / _png_filename(cue, out_w, out_h, font_id)
        png_w, png_h = render_caption_png(
            text=cue.text, style_name=cue.style, out_w=out_w,
            fontfile=fontfile, dest=base_png, out_h=out_h,
        )
        x, y = _place_ost(out_w, out_h, png_w, png_h, cue.position)
        renders.extend(_ost_cue_renders(
            cue, i, base_png, x, y, png_w, png_h, cache_dir, fps,
        ))
    return renders


def _ost_cue_renders(
    cue: EDLOSTCue, idx: int, base_png: Path, x: int, y: int,
    png_w: int, png_h: int, cache_dir: Path, fps: int,
) -> list[OSTRender]:
    total = cue.end - cue.start
    in_dur = cue.animate_duration if cue.animate_in != "none" else 0.0
    out_dur = cue.animate_duration if cue.animate_out != "none" else 0.0
    if in_dur + out_dur > total and (in_dur + out_dur) > 0:
        shrink = total / (in_dur + out_dur)
        in_dur *= shrink
        out_dur *= shrink

    settled_start = cue.start + in_dur
    settled_end = cue.end - out_dur

    renders: list[OSTRender] = []
    if cue.animate_in != "none" and in_dur > 1e-4:
        renders += _ost_anim_renders(
            cue.animate_in, "in", cue.start, settled_start,
            base_png, x, y, png_w, png_h, cache_dir, fps, idx,
        )
    if settled_end - settled_start > 1e-4:
        renders.append(OSTRender(idx, base_png, x, y, settled_start, settled_end))
    if cue.animate_out != "none" and out_dur > 1e-4:
        renders += _ost_anim_renders(
            cue.animate_out, "out", settled_end, cue.end,
            base_png, x, y, png_w, png_h, cache_dir, fps, idx,
        )
    if not renders:
        renders.append(OSTRender(idx, base_png, x, y, cue.start, cue.end))
    return renders


def _ost_anim_renders(
    kind: str, direction: str, w_start: float, w_end: float,
    base_png: Path, x: int, y: int, png_w: int, png_h: int,
    cache_dir: Path, fps: int, idx: int,
) -> list[OSTRender]:
    steps = _anim_steps(kind, w_end - w_start, fps, direction)
    n = len(steps)
    seg = (w_end - w_start) / n
    out: list[OSTRender] = []
    for k, (scale, alpha, y_frac) in enumerate(steps):
        s0 = w_start + k * seg
        s1 = w_end if k == n - 1 else w_start + (k + 1) * seg
        png, dx, dy = _materialize_variant(
            base_png, cache_dir, scale, alpha, y_frac, png_w, png_h,
        )
        out.append(OSTRender(idx, png, x + dx, y + dy, s0, s1))
    return out


def _anim_steps(
    kind: str, dur: float, fps: int, direction: str,
    overshoot: float = OST_POP_OVERSHOOT,
) -> list[tuple[float, float, float]]:
    """Return per-step (scale, alpha, y_fraction) transforms, ordered in time."""
    k = max(2, min(5, round(dur * fps))) if dur > 0 else 2
    span = k - 1
    steps: list[tuple[float, float, float]]
    if kind == "pop":
        steps = [(1.0 + overshoot * (1 - i / span), 1.0, 0.0) for i in range(k)]
    elif kind == "fade":
        steps = [(1.0, (i + 1) / k, 0.0) for i in range(k)]
    elif kind == "slide_up":
        steps = [(1.0, 1.0, SLIDE_OFFSET_FRACTION * (1 - i / span)) for i in range(k)]
    else:
        steps = [(1.0, 1.0, 0.0)]
    if direction == "out":
        steps = list(reversed(steps))
    return steps


def _materialize_variant(
    base_png: Path, cache_dir: Path, scale: float, alpha: float, y_frac: float,
    png_w: int, png_h: int,
) -> tuple[Path, int, int]:
    """Return (png_path, dx, dy) for a scaled/alpha/slid variant of a base PNG.

    Scale variants grow about the card centre; slide reuses the base PNG at a
    shifted y; both are keyed by transform so identical variants share a file.
    """
    dy_slide = round(y_frac * png_h)
    if abs(scale - 1.0) < 1e-3 and abs(alpha - 1.0) < 1e-3:
        return base_png, 0, dy_slide

    dest = cache_dir / f"{base_png.stem}-s{scale:.3f}-a{alpha:.3f}.png"
    if not dest.exists():
        img = Image.open(base_png).convert("RGBA")
        if abs(scale - 1.0) >= 1e-3:
            img = img.resize(
                (max(1, round(png_w * scale)), max(1, round(png_h * scale))),
                Image.LANCZOS,
            )
        if abs(alpha - 1.0) >= 1e-3:
            faded = img.getchannel("A").point(lambda v: round(v * alpha))
            img.putalpha(faded)
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest, format="PNG")

    new_w, new_h = max(1, round(png_w * scale)), max(1, round(png_h * scale))
    dx = (png_w - new_w) // 2
    dy = (png_h - new_h) // 2 + dy_slide
    return dest, dx, dy


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


@dataclass(frozen=True)
class WatermarkRender:
    """A rendered watermark PNG ready to be overlaid for the whole output."""
    png_path: Path
    x: int
    y: int


def _watermark_png_filename(wm: EDLWatermark, out_w: int, out_h: int, font_id: str) -> str:
    key = f"{wm.text}|{wm.opacity}|{wm.font_size}|{wm.position}|{out_w}x{out_h}|{font_id}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"watermark-{digest}.png"


def render_watermark_png(
    wm: EDLWatermark,
    out_w: int,
    out_h: int,
    cache_dir: Path,
    fontfile: str | None = None,
) -> WatermarkRender:
    """Render a subtle, box-free semi-transparent watermark line to a PNG.

    Unlike OST cues, there is no background box: the text alone is drawn at the
    configured opacity with a faint stroke so it stays legible over any footage.
    """
    font_path = _resolve_font(fontfile)
    font = ImageFont.truetype(font_path, wm.font_size)

    alpha = int(round(max(0.0, min(1.0, wm.opacity)) * 255))
    stroke_width = max(1, wm.font_size // 24)
    stroke_alpha = int(round(alpha * 0.6))

    ascent, descent = font.getmetrics()
    text_w = int(font.getlength(wm.text))
    text_h = ascent + descent
    pad = stroke_width + 2
    png_w = text_w + 2 * pad
    png_h = text_h + 2 * pad

    img = Image.new("RGBA", (png_w, png_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.text(
        (pad, pad),
        wm.text,
        font=font,
        fill=(255, 255, 255, alpha),
        stroke_width=stroke_width,
        stroke_fill=(0, 0, 0, stroke_alpha),
    )

    dest = cache_dir / _watermark_png_filename(wm, out_w, out_h, _font_identity(font_path))
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, format="PNG")

    x = (out_w - png_w) // 2
    margin = int(out_h * WATERMARK_MARGIN_FRACTION)
    if wm.position == "top":
        y = margin
    elif wm.position == "middle":
        y = (out_h - png_h) // 2
    else:
        y = out_h - png_h - margin
    y = max(0, min(y, out_h - png_h))

    return WatermarkRender(png_path=dest, x=x, y=y)


def build_watermark_overlay_chain(
    render: WatermarkRender | None,
    input_index: int | None,
    base_label: str,
    final_label: str,
) -> list[str]:
    """Overlay the watermark across the entire output (no enable window)."""
    if render is None or input_index is None:
        return [f"{base_label}null{final_label}"]
    return [
        f"{base_label}[{input_index}:v]"
        f"overlay=x={render.x}:y={render.y}:format=auto"
        f"{final_label}"
    ]
