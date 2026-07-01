from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from supaclip.core.edl import CaptionPosition, CaptionStyleName, EDLCaptions
from supaclip.stitch.overlay import (
    SUPERSAMPLE,
    _anim_steps,
    _draw_box,
    _font_identity,
    _materialize_variant,
    _measure_block,
    _paint_with_shadow,
    _res_scale,
    _resolve_font,
    _scale_style,
    _wrap_text,
)
from supaclip.stitch.tts.base import Alignment

RGBA = tuple[int, int, int, int]

CAPTION_FONT_FIT_MIN = 28
CAPTION_FONT_FIT_STEP = 4

# Default persistent zoom of the current word in "active_word" highlight when the
# EDL leaves active_word_scale unset. karaoke_fill stays at 1.0 (no zoom) so its
# existing output is unchanged.
DEFAULT_ACTIVE_WORD_SCALE = 1.15

# Extra tracking between individually-placed karaoke words, as a fraction of the
# font size on top of the font's own space glyph. Condensed display faces (Anton)
# have a narrow space, so words otherwise crowd — and the active-word zoom needs
# room not to touch its neighbours.
WORD_SPACING_FRAC = 0.18


@dataclass(frozen=True)
class CaptionVisualStyle:
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


CAPTION_STYLE_PRESETS: dict[CaptionStyleName, CaptionVisualStyle] = {
    "clean_white": CaptionVisualStyle(
        bg=(0, 0, 0, 0),
        fg=(255, 255, 255, 255),
        stroke=(0, 0, 0, 235),
        stroke_width=5,
        font_size=58,
        padding_x=24,
        padding_y=12,
        corner_radius=0,
        line_spacing=6,
        uppercase=False,
    ),
    "boxed_dark": CaptionVisualStyle(
        bg=(0, 0, 0, 200),
        fg=(255, 255, 255, 255),
        stroke=None,
        stroke_width=0,
        font_size=54,
        padding_x=24,
        padding_y=14,
        corner_radius=14,
        line_spacing=6,
        uppercase=False,
    ),
    "karaoke_yellow": CaptionVisualStyle(
        bg=(0, 0, 0, 0),
        fg=(255, 214, 0, 255),
        stroke=(0, 0, 0, 240),
        stroke_width=6,
        font_size=62,
        padding_x=24,
        padding_y=12,
        corner_radius=0,
        line_spacing=6,
        uppercase=True,
    ),
}


CAPTION_POSITION_FRACTION: dict[CaptionPosition, float] = {
    "top": 0.18,
    "middle": 0.50,
    "lower_third": 0.70,
    "bottom": 0.88,
}


@dataclass(frozen=True)
class CaptionWord:
    text: str
    start: float
    end: float


@dataclass(frozen=True)
class CaptionChunk:
    text: str
    start: float
    end: float
    words: tuple[CaptionWord, ...] = ()


@dataclass(frozen=True)
class CaptionRender:
    chunk_index: int
    png_path: Path
    x: int
    y: int
    start: float
    end: float


_HARD_BREAK_CHARS = frozenset(".!?")
_SOFT_BREAK_CHARS = frozenset(",;:")


def chunk_alignment(
    alignment: Alignment,
    max_words: int = 4,
    max_chars: int = 28,
    min_chunk_duration: float = 0.4,
) -> list[CaptionChunk]:
    """Group characters into short phrase chunks suitable for caption overlays.

    Breaks on hard punctuation (.!?), then soft punctuation (,;:), then on
    word boundaries when reaching the word/char limit. Very short chunks are
    extended to `min_chunk_duration` so they're readable.
    """
    chars = alignment.characters
    starts = alignment.start_times
    ends = alignment.end_times
    if not chars:
        return []

    chunks: list[CaptionChunk] = []
    buf: list[str] = []
    buf_start: float | None = None
    word_count = 0
    last_char_was_space = True

    def _flush(end_time: float) -> None:
        nonlocal buf, buf_start, word_count, last_char_was_space
        text = "".join(buf).strip()
        has_word = any(c.isalnum() for c in text)
        if text and has_word and buf_start is not None:
            chunks.append(CaptionChunk(text=text, start=buf_start, end=end_time))
        buf = []
        buf_start = None
        word_count = 0
        last_char_was_space = True

    for i, ch in enumerate(chars):
        if buf_start is None and ch.strip():
            buf_start = starts[i]
        buf.append(ch)

        is_space = ch.isspace()
        if is_space and not last_char_was_space and any(c.strip() for c in buf):
            word_count += 1
        last_char_was_space = is_space

        trimmed_len = len("".join(buf).strip())
        hit_hard = ch in _HARD_BREAK_CHARS
        hit_soft = ch in _SOFT_BREAK_CHARS and word_count >= max(2, max_words - 1)
        hit_word_limit = is_space and word_count >= max_words
        hit_char_limit = is_space and trimmed_len >= max_chars

        if hit_hard or hit_soft or hit_word_limit or hit_char_limit:
            _flush(ends[i])

    if buf:
        _flush(ends[-1])

    cleaned: list[CaptionChunk] = []
    for c in chunks:
        if c.end - c.start < min_chunk_duration:
            cleaned.append(CaptionChunk(
                text=c.text, start=c.start, end=c.start + min_chunk_duration,
            ))
        else:
            cleaned.append(c)

    for i in range(len(cleaned) - 1):
        if cleaned[i].end > cleaned[i + 1].start:
            cleaned[i] = CaptionChunk(
                text=cleaned[i].text,
                start=cleaned[i].start,
                end=cleaned[i + 1].start,
            )

    return _attach_words(cleaned, _extract_words(alignment))


def chunks_from_cues(cues) -> list[CaptionChunk]:
    """Build caption chunks from pre-timed cues (e.g. source subtitles).

    One chunk per cue, using the cue's own timing. No per-word timing is
    available, so these render as whole-phrase captions (karaoke fill, which
    needs word times, degrades to whole-phrase automatically).
    """
    out: list[CaptionChunk] = []
    for c in cues:
        text = " ".join(str(c.text).split())
        if text and c.end > c.start:
            out.append(CaptionChunk(text=text, start=float(c.start), end=float(c.end)))
    out.sort(key=lambda c: c.start)
    return out


def _extract_words(alignment: Alignment) -> list[CaptionWord]:
    """Split the character-level alignment into whitespace-delimited words,
    each timed from its first character's start to its last character's end."""
    words: list[CaptionWord] = []
    buf: list[str] = []
    start: float | None = None
    end = 0.0
    for ch, s, e in zip(alignment.characters, alignment.start_times, alignment.end_times):
        if ch.isspace():
            if buf:
                words.append(CaptionWord("".join(buf), start, end))
                buf, start = [], None
        else:
            if start is None:
                start = s
            buf.append(ch)
            end = e
    if buf:
        words.append(CaptionWord("".join(buf), start, end))
    return words


def _attach_words(
    chunks: list[CaptionChunk], words: list[CaptionWord]
) -> list[CaptionChunk]:
    """Bucket words into the chunks they belong to by start time. Chunk starts
    are word-aligned, so a greedy pointer assigns each word to its chunk."""
    out: list[CaptionChunk] = []
    wi = 0
    for i, chunk in enumerate(chunks):
        next_start = chunks[i + 1].start if i + 1 < len(chunks) else float("inf")
        bucket: list[CaptionWord] = []
        while wi < len(words) and words[wi].start < next_start - 1e-6:
            bucket.append(words[wi])
            wi += 1
        out.append(CaptionChunk(
            text=chunk.text, start=chunk.start, end=chunk.end,
            words=tuple(bucket),
        ))
    return out


def _hash(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _png_filename(text: str, style: CaptionStyleName, font_size: int,
                  out_w: int, out_h: int, font_id: str) -> str:
    key = f"caption|{text}|{style}|{font_size}|{out_w}x{out_h}|{font_id}|ss{SUPERSAMPLE}"
    return f"cap-{_hash(key)}.png"


def _karaoke_png_filename(
    text: str, active: int, style: CaptionStyleName, font_size: int,
    highlight_color: str, out_w: int, out_h: int, font_id: str, pill: str,
    mode: str,
) -> str:
    key = (f"karaoke|{mode}|{text}|{active}|{style}|{font_size}"
           f"|{highlight_color}|{out_w}x{out_h}|{font_id}|ss{SUPERSAMPLE}|{pill}")
    return f"capk-{_hash(key)}.png"


def _word_pop_filename(
    text: str, active: int, scale: float, style: CaptionStyleName, font_size: int,
    highlight_color: str, out_w: int, out_h: int, font_id: str,
) -> str:
    key = (f"kpop|{text}|{active}|{scale:.3f}|{style}|{font_size}"
           f"|{highlight_color}|{out_w}x{out_h}|{font_id}|ss{SUPERSAMPLE}")
    return f"capk-{_hash(key)}.png"


def _fit_font_size(font_path: str, base_size: int, min_size: int, step: int,
                   measure, max_width: int) -> int:
    """Shrink the font until the measured block fits `max_width`."""
    size = base_size
    while size > min_size:
        if measure(ImageFont.truetype(font_path, size)) <= max_width:
            break
        size -= step
    return max(min_size, size)


def _render_caption_png(
    text: str,
    style_target: CaptionVisualStyle,
    g: float,
    out_w: int,
    fontfile: str | None,
    dest: Path,
) -> tuple[int, int]:
    display_text = text.upper() if style_target.uppercase else text
    font_path = _resolve_font(fontfile)

    max_text_width = int(out_w * 0.88) - 2 * style_target.padding_x
    min_font = max(8, round(CAPTION_FONT_FIT_MIN * g))
    step = max(1, round(CAPTION_FONT_FIT_STEP * g))

    def measure(font: ImageFont.FreeTypeFont) -> int:
        lines = _wrap_text(display_text, font, max_text_width)
        block_w, _, _ = _measure_block(lines, font, style_target.line_spacing)
        return block_w

    size = _fit_font_size(font_path, style_target.font_size, min_font, step,
                          measure, max_text_width)

    ss = SUPERSAMPLE
    style = _scale_style(dataclasses.replace(style_target, font_size=size), ss)
    font = ImageFont.truetype(font_path, style.font_size)
    lines = _wrap_text(display_text, font, max_text_width * ss)
    block_w, block_h, line_h = _measure_block(lines, font, style.line_spacing)

    png_w = block_w + 2 * style.padding_x
    png_h = block_h + 2 * style.padding_y

    img = Image.new("RGBA", (png_w, png_h), (0, 0, 0, 0))
    _draw_box(img, style)

    positioned: list[tuple[int, int, str]] = []
    y = style.padding_y
    for line in lines:
        x = (png_w - int(font.getlength(line))) // 2
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


def _hex_to_rgba(value: str) -> RGBA:
    s = value.lstrip("#")
    r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    a = int(s[6:8], 16) if len(s) == 8 else 255
    return (r, g, b, a)


def _space_width(font: ImageFont.FreeTypeFont) -> float:
    """Effective gap between words: the font's space glyph plus default tracking.

    Used identically for wrapping, width measurement, and placement so the three
    always agree on where words sit.
    """
    return font.getlength(" ") + WORD_SPACING_FRAC * font.size


def _pack_words(
    display_words: list[str], font: ImageFont.FreeTypeFont, max_width: int
) -> list[list[tuple[int, str]]]:
    """Greedy word-wrap that keeps each word's original index for coloring."""
    space_w = _space_width(font)
    lines: list[list[tuple[int, str]]] = []
    cur: list[tuple[int, str]] = []
    cur_w = 0.0
    for idx, word in enumerate(display_words):
        word_w = font.getlength(word)
        advance = word_w if not cur else space_w + word_w
        if cur and cur_w + advance > max_width:
            lines.append(cur)
            cur, cur_w = [], 0.0
            advance = word_w
        cur.append((idx, word))
        cur_w += advance
    if cur:
        lines.append(cur)
    return lines


def _line_width(line: list[tuple[int, str]], font: ImageFont.FreeTypeFont) -> float:
    if not line:
        return 0.0
    space_w = _space_width(font)
    return sum(font.getlength(w) for _, w in line) + space_w * (len(line) - 1)


@dataclass(frozen=True)
class _KaraokeLayout:
    style: CaptionVisualStyle          # at supersample scale
    font: ImageFont.FreeTypeFont       # at supersample scale
    png_w: int
    png_h: int
    line_h: int
    boxes: dict[int, tuple[int, int, float]]   # word index -> (x, y, width)


def _karaoke_layout(
    display_words: list[str],
    style_target: CaptionVisualStyle,
    g: float,
    out_w: int,
    font_path: str,
) -> _KaraokeLayout:
    """Lay a karaoke phrase out at supersample scale, returning per-word boxes.

    Shared by the phrase render and the per-word "pop" overlay so the two align
    pixel-for-pixel after the identical LANCZOS downscale.
    """
    max_text_width = int(out_w * 0.88) - 2 * style_target.padding_x
    min_font = max(8, round(CAPTION_FONT_FIT_MIN * g))
    step = max(1, round(CAPTION_FONT_FIT_STEP * g))

    def measure(font: ImageFont.FreeTypeFont) -> int:
        lines = _pack_words(display_words, font, max_text_width)
        return int(max((_line_width(line, font) for line in lines), default=0))

    size = _fit_font_size(font_path, style_target.font_size, min_font, step,
                          measure, max_text_width)

    ss = SUPERSAMPLE
    style = _scale_style(dataclasses.replace(style_target, font_size=size), ss)
    font = ImageFont.truetype(font_path, style.font_size)
    lines = _pack_words(display_words, font, max_text_width * ss)
    space_w = _space_width(font)
    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    block_w = int(max((_line_width(line, font) for line in lines), default=0))
    block_h = line_h * len(lines) + style.line_spacing * max(0, len(lines) - 1)

    png_w = block_w + 2 * style.padding_x
    png_h = block_h + 2 * style.padding_y

    boxes: dict[int, tuple[int, int, float]] = {}
    y = style.padding_y
    for line in lines:
        x = (png_w - int(_line_width(line, font))) // 2
        for idx, word in line:
            w = font.getlength(word)
            boxes[idx] = (x, y, w)
            x += int(w + space_w)
        y += line_h + style.line_spacing

    return _KaraokeLayout(style, font, png_w, png_h, line_h, boxes)


def _render_caption_karaoke_png(
    words: list[str],
    active_index: int,
    style_target: CaptionVisualStyle,
    g: float,
    highlight: RGBA,
    out_w: int,
    fontfile: str | None,
    dest: Path,
    pill: RGBA | None = None,
    pill_radius: int = 0,
    active_only: bool = False,
) -> tuple[int, int]:
    """Render the full phrase, coloring words individually.

    `active_only=False` is the cumulative karaoke fill (words 0..active_index in
    `highlight`); `active_only=True` colors just the current word, so the
    highlight tracks the spoken word instead of trailing behind it.
    """
    display_words = [w.upper() for w in words] if style_target.uppercase else list(words)
    font_path = _resolve_font(fontfile)
    lay = _karaoke_layout(display_words, style_target, g, out_w, font_path)
    style, font = lay.style, lay.font

    img = Image.new("RGBA", (lay.png_w, lay.png_h), (0, 0, 0, 0))
    _draw_box(img, style)

    if pill is not None and active_index in lay.boxes:
        bx, by, bw = lay.boxes[active_index]
        pad = round(lay.line_h * 0.14)
        rad = max(0, round(pill_radius * SUPERSAMPLE))
        pill_layer = Image.new("RGBA", (lay.png_w, lay.png_h), (0, 0, 0, 0))
        ImageDraw.Draw(pill_layer).rounded_rectangle(
            (bx - pad, by - pad, bx + int(bw) + pad, by + lay.line_h + pad),
            radius=rad, fill=pill,
        )
        img.alpha_composite(pill_layer)

    def draw_glyphs(draw, dx, dy, forced):
        for idx, (bx, by, _bw) in lay.boxes.items():
            word = display_words[idx]
            if forced is not None:
                draw.text((bx + dx, by + dy), word, font=font, fill=forced,
                          stroke_width=style.stroke_width, stroke_fill=forced)
                continue
            is_lit = idx == active_index if active_only else idx <= active_index
            color = highlight if is_lit else style.fg
            if style.stroke is not None and style.stroke_width > 0:
                draw.text((bx, by), word, font=font, fill=color,
                          stroke_width=style.stroke_width, stroke_fill=style.stroke)
            else:
                draw.text((bx, by), word, font=font, fill=color)

    _paint_with_shadow(img, style, draw_glyphs)

    final_w, final_h = round(lay.png_w / SUPERSAMPLE), round(lay.png_h / SUPERSAMPLE)
    img = img.resize((final_w, final_h), Image.LANCZOS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, format="PNG")
    return final_w, final_h


def _render_word_pop_png(
    words: list[str],
    active_index: int,
    scale: float,
    style_target: CaptionVisualStyle,
    g: float,
    highlight: RGBA,
    out_w: int,
    fontfile: str | None,
    dest: Path,
) -> tuple[int, int]:
    """Render only the active word, scaled about its own centre, on a phrase-
    sized transparent canvas. Overlaid on the settled phrase it pops that one
    word without reflowing the line (the scaled glyph covers the settled one)."""
    display_words = [w.upper() for w in words] if style_target.uppercase else list(words)
    font_path = _resolve_font(fontfile)
    lay = _karaoke_layout(display_words, style_target, g, out_w, font_path)
    style = lay.style
    bx, by, bw = lay.boxes[active_index]
    word = display_words[active_index]

    scaled_font = ImageFont.truetype(font_path, max(1, round(style.font_size * scale)))
    sw = scaled_font.getlength(word)
    s_ascent, s_descent = scaled_font.getmetrics()
    s_line_h = s_ascent + s_descent
    cx = bx + bw / 2
    cy = by + lay.line_h / 2
    x = round(cx - sw / 2)
    y = round(cy - s_line_h / 2)

    img = Image.new("RGBA", (lay.png_w, lay.png_h), (0, 0, 0, 0))

    def draw_glyphs(draw, dx, dy, forced):
        color = forced if forced is not None else highlight
        if style.stroke is not None and style.stroke_width > 0:
            draw.text((x + dx, y + dy), word, font=scaled_font, fill=color,
                      stroke_width=style.stroke_width,
                      stroke_fill=(forced if forced is not None else style.stroke))
        else:
            draw.text((x + dx, y + dy), word, font=scaled_font, fill=color)

    _paint_with_shadow(img, style, draw_glyphs)

    final_w, final_h = round(lay.png_w / SUPERSAMPLE), round(lay.png_h / SUPERSAMPLE)
    img = img.resize((final_w, final_h), Image.LANCZOS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, format="PNG")
    return final_w, final_h


def _style_target(config: EDLCaptions, out_h: int) -> tuple[CaptionVisualStyle, float]:
    """Resolve the preset into an output-resolution-aware style plus its scale.

    When `font_size` is unset the whole style scales by out_h/reference so
    captions keep their relative size at any resolution; when it is set (already
    resolution-scaled by scale_edl) the geometry scales with the chosen font.
    """
    preset = CAPTION_STYLE_PRESETS[config.style]
    if config.font_size is not None:
        g = config.font_size / preset.font_size if preset.font_size else 1.0
    else:
        g = _res_scale(out_h)
    return _scale_style(preset, g), g


def render_caption_pngs(
    chunks: list[CaptionChunk],
    config: EDLCaptions,
    out_w: int,
    out_h: int,
    cache_dir: Path,
    voiceover_offset: float = 0.0,
    fontfile: str | None = None,
    fps: int = 30,
) -> list[CaptionRender]:
    """Render every caption chunk to a PNG and return placement info.

    `voiceover_offset` shifts chunk times to the caption track's timeline
    position (i.e. the start time of the voiceover audio cue).
    """
    style_target, g = _style_target(config, out_h)
    font_id = _font_identity(_resolve_font(fontfile))

    def _place(png_w: int, png_h: int) -> tuple[int, int]:
        x = (out_w - png_w) // 2
        anchor = CAPTION_POSITION_FRACTION[config.position]
        y = int(out_h * anchor) - png_h // 2
        return x, max(0, min(y, out_h - png_h))

    fade_in = max(
        config.animate_duration if config.animate == "fade" else 0.0,
        config.fade_ms / 1000.0,
    )
    fade_out = config.fade_ms / 1000.0

    if config.highlight in ("karaoke_fill", "active_word"):
        highlight_rgba = _hex_to_rgba(config.highlight_color)
        pill = _hex_to_rgba(config.active_word_bg) if config.active_word_bg else None
        pill_radius = round(config.active_word_bg_radius * g)
        active_only = config.highlight == "active_word"
        if config.active_word_scale is not None:
            settle_scale = config.active_word_scale
        elif active_only:
            settle_scale = DEFAULT_ACTIVE_WORD_SCALE
        else:
            settle_scale = 1.0
        mode = "active" if active_only else "fill"
        renders: list[CaptionRender] = []
        for i, chunk in enumerate(chunks):
            renders.extend(_karaoke_chunk_renders(
                chunk, i, config, style_target, g, highlight_rgba,
                out_w, out_h, font_id, cache_dir, voiceover_offset, fontfile,
                _place, fps, pill, pill_radius, fade_in, fade_out,
                active_only, settle_scale, mode,
            ))
        return renders

    renders = []
    for i, chunk in enumerate(chunks):
        renders.extend(_plain_chunk_renders(
            chunk, i, config, style_target, g, out_w, out_h, font_id,
            cache_dir, voiceover_offset, fontfile, _place, fps, fade_in, fade_out,
        ))
    return renders


def _plain_chunk_renders(
    chunk: CaptionChunk, chunk_index: int, config: EDLCaptions,
    style_target: CaptionVisualStyle, g: float, out_w: int, out_h: int,
    font_id: str, cache_dir: Path, voiceover_offset: float, fontfile: str | None,
    place, fps: int, fade_in: float, fade_out: float,
) -> list[CaptionRender]:
    png_path = cache_dir / _png_filename(
        chunk.text, config.style, style_target.font_size, out_w, out_h, font_id,
    )
    png_w, png_h = _render_caption_png(
        text=chunk.text, style_target=style_target, g=g, out_w=out_w,
        fontfile=fontfile, dest=png_path,
    )
    x, y = place(png_w, png_h)
    start = chunk.start + voiceover_offset
    end = chunk.end + voiceover_offset

    renders = _entrance_renders(
        config.animate, png_path, x, y, png_w, png_h, start, end,
        config.animate_duration, config.animate_overshoot, cache_dir, fps, chunk_index,
    )
    return _apply_fades(renders, start, end, fade_in, fade_out, cache_dir, fps)


def _entrance_renders(
    animate: str, png_path: Path, x: int, y: int, png_w: int, png_h: int,
    start: float, end: float, duration: float, overshoot: float,
    cache_dir: Path, fps: int, chunk_index: int,
) -> list[CaptionRender]:
    """Whole-phrase entrance for plain captions: a pop settles the phrase in,
    everything else is a single static render."""
    if animate != "pop" or duration <= 0 or end - start <= duration:
        return [CaptionRender(chunk_index, png_path, x, y, start, end)]

    steps = _anim_steps("pop", duration, fps, "in", overshoot=overshoot)
    seg = duration / len(steps)
    renders: list[CaptionRender] = []
    for k, (scale, alpha, y_frac) in enumerate(steps):
        s0 = start + k * seg
        s1 = start + duration if k == len(steps) - 1 else start + (k + 1) * seg
        variant, dx, dy = _materialize_variant(
            png_path, cache_dir, scale, alpha, y_frac, png_w, png_h,
        )
        renders.append(CaptionRender(chunk_index, variant, x + dx, y + dy, s0, s1))
    renders.append(CaptionRender(chunk_index, png_path, x, y, start + duration, end))
    return renders


def _apply_fades(
    renders: list[CaptionRender], start: float, end: float,
    fade_in: float, fade_out: float, cache_dir: Path, fps: int,
) -> list[CaptionRender]:
    """Alpha-ramp the leading `fade_in` and trailing `fade_out` of a chunk by
    splitting its first/last render into alpha-stepped windows."""
    if fade_in <= 0 and fade_out <= 0 or not renders:
        return renders
    out = list(renders)
    if fade_in > 0:
        out = _fade_edge(out, "in", fade_in, cache_dir, fps)
    if fade_out > 0:
        out = _fade_edge(out, "out", fade_out, cache_dir, fps)
    return out


def _fade_edge(
    renders: list[CaptionRender], direction: str, dur: float,
    cache_dir: Path, fps: int,
) -> list[CaptionRender]:
    """Alpha-ramp the render that actually touches the chunk edge.

    Picked by time (earliest start / latest end), not list position, so a
    per-word pop overlay near the last word's *start* never gets mistaken for
    the chunk's trailing render.
    """
    if direction == "in":
        idx = min(range(len(renders)), key=lambda k: renders[k].start)
    else:
        idx = max(range(len(renders)), key=lambda k: renders[k].end)
    r = renders[idx]
    dur = min(dur, r.end - r.start)
    if dur <= 1e-4:
        return renders

    steps = _anim_steps("fade", dur, fps, direction)
    seg = dur / len(steps)
    faded: list[CaptionRender] = []

    if direction == "in":
        t = r.start
        for k, (_s, alpha, _y) in enumerate(steps):
            t1 = r.start + dur if k == len(steps) - 1 else t + seg
            variant, _, _ = _materialize_variant(r.png_path, cache_dir, 1.0, alpha, 0.0, 1, 1)
            faded.append(dataclasses.replace(r, png_path=variant, start=t, end=t1))
            t = t1
        remainder = dataclasses.replace(r, start=r.start + dur)
        replacement = faded + ([remainder] if remainder.end - remainder.start > 1e-4 else [])
    else:
        t = r.end - dur
        remainder = dataclasses.replace(r, end=t)
        for k, (_s, alpha, _y) in enumerate(steps):
            t1 = r.end if k == len(steps) - 1 else t + seg
            variant, _, _ = _materialize_variant(r.png_path, cache_dir, 1.0, alpha, 0.0, 1, 1)
            faded.append(dataclasses.replace(r, png_path=variant, start=t, end=t1))
            t = t1
        replacement = ([remainder] if remainder.end - remainder.start > 1e-4 else []) + faded

    return renders[:idx] + replacement + renders[idx + 1:]


def _karaoke_chunk_renders(
    chunk: CaptionChunk,
    chunk_index: int,
    config: EDLCaptions,
    style_target: CaptionVisualStyle,
    g: float,
    highlight: RGBA,
    out_w: int,
    out_h: int,
    font_id: str,
    cache_dir: Path,
    voiceover_offset: float,
    fontfile: str | None,
    place,
    fps: int,
    pill: RGBA | None,
    pill_radius: int,
    fade_in: float,
    fade_out: float,
    active_only: bool,
    settle_scale: float,
    mode: str,
) -> list[CaptionRender]:
    """One base render per word, each visible for that word's spoken window,
    plus optional per-word scale overlays (entrance pop and/or persistent zoom)."""
    words = chunk.words
    start = chunk.start + voiceover_offset
    end = chunk.end + voiceover_offset
    if not words:
        png_path = cache_dir / _png_filename(
            chunk.text, config.style, style_target.font_size, out_w, out_h, font_id,
        )
        png_w, png_h = _render_caption_png(
            text=chunk.text, style_target=style_target, g=g, out_w=out_w,
            fontfile=fontfile, dest=png_path,
        )
        x, y = place(png_w, png_h)
        base = [CaptionRender(chunk_index, png_path, x, y, start, end)]
        return _apply_fades(base, start, end, fade_in, fade_out, cache_dir, fps)

    word_texts = [w.text for w in words]
    pill_key = f"{pill}-{pill_radius}" if pill is not None else "none"
    renders: list[CaptionRender] = []
    for j, word in enumerate(words):
        png_path = cache_dir / _karaoke_png_filename(
            chunk.text, j, config.style, style_target.font_size,
            config.highlight_color, out_w, out_h, font_id, pill_key, mode,
        )
        png_w, png_h = _render_caption_karaoke_png(
            words=word_texts, active_index=j, style_target=style_target, g=g,
            highlight=highlight, out_w=out_w, fontfile=fontfile, dest=png_path,
            pill=pill, pill_radius=pill_radius, active_only=active_only,
        )
        x, y = place(png_w, png_h)
        win_start = word.start + voiceover_offset
        win_end = (words[j + 1].start if j + 1 < len(words) else chunk.end) + voiceover_offset
        renders.append(CaptionRender(chunk_index, png_path, x, y, win_start, win_end))
        renders.extend(_active_word_overlays(
            word_texts, j, config, style_target, g, highlight, out_w, out_h,
            font_id, cache_dir, fontfile, x, y, win_start, win_end, fps,
            chunk_index, settle_scale,
        ))

    return _apply_fades(renders, start, end, fade_in, fade_out, cache_dir, fps)


def _active_word_overlays(
    word_texts: list[str], active_index: int, config: EDLCaptions,
    style_target: CaptionVisualStyle, g: float, highlight: RGBA, out_w: int,
    out_h: int, font_id: str, cache_dir: Path, fontfile: str | None,
    x: int, y: int, win_start: float, win_end: float, fps: int, chunk_index: int,
    settle_scale: float,
) -> list[CaptionRender]:
    """Scale overlays for the active word, drawn on the settled phrase.

    Two composable pieces, both scale >= 1 so the overlay covers the settled
    glyph without reflowing the line: a `pop` entrance that overshoots then
    settles to `settle_scale`, and a persistent hold at `settle_scale` for the
    rest of the word (the "active_word" zoom). Nothing is emitted when the word
    neither pops nor zooms — the base render already shows it at 1.0.
    """
    window = win_end - win_start
    pop = config.animate == "pop" and config.animate_duration > 0
    pop_dur = min(config.animate_duration, window) if pop else 0.0
    out: list[CaptionRender] = []

    persist_start = win_start
    if pop and pop_dur > 1e-4:
        steps = _anim_steps("pop", pop_dur, fps, "in", overshoot=config.animate_overshoot)
        seg = pop_dur / len(steps)
        for k, (base_scale, _a, _y) in enumerate(steps):
            scale = settle_scale * base_scale
            if scale <= 1.0 + 1e-3:
                continue
            s0 = win_start + k * seg
            s1 = win_start + pop_dur if k == len(steps) - 1 else win_start + (k + 1) * seg
            out.append(_emit_word_scale(
                word_texts, active_index, scale, config, style_target, g,
                highlight, out_w, out_h, font_id, cache_dir, fontfile,
                x, y, s0, s1, chunk_index,
            ))
        persist_start = win_start + pop_dur

    if settle_scale > 1.0 + 1e-3 and win_end - persist_start > 1e-4:
        out.append(_emit_word_scale(
            word_texts, active_index, settle_scale, config, style_target, g,
            highlight, out_w, out_h, font_id, cache_dir, fontfile,
            x, y, persist_start, win_end, chunk_index,
        ))
    return out


def _emit_word_scale(
    word_texts: list[str], active_index: int, scale: float, config: EDLCaptions,
    style_target: CaptionVisualStyle, g: float, highlight: RGBA, out_w: int,
    out_h: int, font_id: str, cache_dir: Path, fontfile: str | None,
    x: int, y: int, start: float, end: float, chunk_index: int,
) -> CaptionRender:
    dest = cache_dir / _word_pop_filename(
        "".join(word_texts), active_index, scale, config.style,
        style_target.font_size, config.highlight_color, out_w, out_h, font_id,
    )
    _render_word_pop_png(
        words=word_texts, active_index=active_index, scale=scale,
        style_target=style_target, g=g, highlight=highlight, out_w=out_w,
        fontfile=fontfile, dest=dest,
    )
    return CaptionRender(chunk_index, dest, x, y, start, end)


def build_caption_overlay_chain(
    renders: list[CaptionRender],
    input_indices: list[int],
    base_label: str,
    final_label: str,
) -> list[str]:
    """Build ffmpeg filter_complex chains overlaying each caption PNG.

    Mirrors build_ost_overlay_chain. base_label feeds the first overlay;
    each subsequent overlay chains off the prior one.
    """
    if not renders:
        return [f"{base_label}null{final_label}"]
    if len(renders) != len(input_indices):
        raise ValueError("renders and input_indices length mismatch")

    chains: list[str] = []
    cur = base_label
    for n, (r, idx) in enumerate(zip(renders, input_indices)):
        is_last = n == len(renders) - 1
        out_label = final_label if is_last else f"[vcap{n}]"
        chains.append(
            f"{cur}[{idx}:v]"
            f"overlay=x={r.x}:y={r.y}:format=auto"
            f":enable='between(t,{r.start:.3f},{r.end:.3f})'"
            f"{out_label}"
        )
        cur = out_label
    return chains
