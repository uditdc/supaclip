from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from supaclip.core.edl import CaptionPosition, CaptionStyleName, EDLCaptions
from supaclip.stitch.overlay import (
    DEFAULT_FONT_CANDIDATES,
    _measure_block,
    _resolve_font,
    _wrap_text,
)
from supaclip.stitch.tts.base import Alignment


RGBA = tuple[int, int, int, int]


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


def _png_filename(chunk: CaptionChunk, style: CaptionStyleName,
                  font_size: int, out_w: int, out_h: int) -> str:
    key = f"caption|{chunk.text}|{style}|{font_size}|{out_w}x{out_h}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"cap-{digest}.png"


def _render_caption_png(
    text: str,
    style: CaptionVisualStyle,
    out_w: int,
    fontfile: str | None,
    dest: Path,
) -> tuple[int, int]:
    display_text = text.upper() if style.uppercase else text
    font_path = _resolve_font(fontfile)

    max_text_width = int(out_w * 0.88) - 2 * style.padding_x
    font_size = style.font_size
    while font_size >= 28:
        font = ImageFont.truetype(font_path, font_size)
        lines = _wrap_text(display_text, font, max_text_width)
        block_w, _, _ = _measure_block(lines, font, style.line_spacing)
        if block_w <= max_text_width or font_size == 28:
            break
        font_size -= 4

    font = ImageFont.truetype(font_path, font_size)
    lines = _wrap_text(display_text, font, max_text_width)
    block_w, block_h, line_h = _measure_block(lines, font, style.line_spacing)

    png_w = block_w + 2 * style.padding_x
    png_h = block_h + 2 * style.padding_y

    img = Image.new("RGBA", (png_w, png_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if style.bg[3] > 0:
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
                (x, y), line, font=font, fill=style.fg,
                stroke_width=style.stroke_width, stroke_fill=style.stroke,
            )
        else:
            draw.text((x, y), line, font=font, fill=style.fg)
        y += line_h + style.line_spacing

    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, format="PNG")
    return png_w, png_h


def _hex_to_rgba(value: str) -> RGBA:
    s = value.lstrip("#")
    r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    a = int(s[6:8], 16) if len(s) == 8 else 255
    return (r, g, b, a)


def _pack_words(
    display_words: list[str], font: ImageFont.FreeTypeFont, max_width: int
) -> list[list[tuple[int, str]]]:
    """Greedy word-wrap that keeps each word's original index for coloring."""
    space_w = font.getlength(" ")
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


def _karaoke_png_filename(
    chunk: CaptionChunk, active: int, style: CaptionStyleName,
    font_size: int, highlight_color: str, out_w: int, out_h: int,
) -> str:
    key = (f"karaoke|{chunk.text}|{active}|{style}|{font_size}"
           f"|{highlight_color}|{out_w}x{out_h}")
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"capk-{digest}.png"


def _render_caption_karaoke_png(
    words: list[str],
    active_index: int,
    style: CaptionVisualStyle,
    highlight: RGBA,
    out_w: int,
    fontfile: str | None,
    dest: Path,
) -> tuple[int, int]:
    """Render the full phrase with words 0..active_index in `highlight` and the
    rest in `style.fg`, laying words out individually so each can be colored."""
    display_words = [w.upper() for w in words] if style.uppercase else list(words)
    font_path = _resolve_font(fontfile)

    max_text_width = int(out_w * 0.88) - 2 * style.padding_x
    font_size = style.font_size
    while font_size >= 28:
        font = ImageFont.truetype(font_path, font_size)
        lines = _pack_words(display_words, font, max_text_width)
        block_w = max(_line_width(line, font) for line in lines)
        if block_w <= max_text_width or font_size == 28:
            break
        font_size -= 4

    font = ImageFont.truetype(font_path, font_size)
    lines = _pack_words(display_words, font, max_text_width)
    space_w = font.getlength(" ")
    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    block_w = max(_line_width(line, font) for line in lines)
    block_h = line_h * len(lines) + style.line_spacing * max(0, len(lines) - 1)

    png_w = int(block_w) + 2 * style.padding_x
    png_h = block_h + 2 * style.padding_y

    img = Image.new("RGBA", (png_w, png_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if style.bg[3] > 0:
        draw.rounded_rectangle(
            (0, 0, png_w - 1, png_h - 1), radius=style.corner_radius, fill=style.bg,
        )

    y = style.padding_y
    for line in lines:
        x = (png_w - int(_line_width(line, font))) // 2
        for idx, word in line:
            color = highlight if idx <= active_index else style.fg
            if style.stroke is not None and style.stroke_width > 0:
                draw.text((x, y), word, font=font, fill=color,
                          stroke_width=style.stroke_width, stroke_fill=style.stroke)
            else:
                draw.text((x, y), word, font=font, fill=color)
            x += int(font.getlength(word) + space_w)
        y += line_h + style.line_spacing

    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, format="PNG")
    return png_w, png_h


def _line_width(line: list[tuple[int, str]], font: ImageFont.FreeTypeFont) -> float:
    if not line:
        return 0.0
    space_w = font.getlength(" ")
    return sum(font.getlength(w) for _, w in line) + space_w * (len(line) - 1)


def render_caption_pngs(
    chunks: list[CaptionChunk],
    config: EDLCaptions,
    out_w: int,
    out_h: int,
    cache_dir: Path,
    voiceover_offset: float = 0.0,
    fontfile: str | None = None,
) -> list[CaptionRender]:
    """Render every caption chunk to a PNG and return placement info.

    `voiceover_offset` shifts chunk times to the caption track's timeline
    position (i.e. the start time of the voiceover audio cue).
    """
    preset = CAPTION_STYLE_PRESETS[config.style]
    if config.font_size is not None:
        preset = CaptionVisualStyle(
            bg=preset.bg, fg=preset.fg, stroke=preset.stroke,
            stroke_width=preset.stroke_width, font_size=config.font_size,
            padding_x=preset.padding_x, padding_y=preset.padding_y,
            corner_radius=preset.corner_radius, line_spacing=preset.line_spacing,
            uppercase=preset.uppercase,
        )

    def _place(png_w: int, png_h: int) -> tuple[int, int]:
        x = (out_w - png_w) // 2
        anchor = CAPTION_POSITION_FRACTION[config.position]
        y = int(out_h * anchor) - png_h // 2
        return x, max(0, min(y, out_h - png_h))

    if config.highlight == "karaoke_fill":
        highlight_rgba = _hex_to_rgba(config.highlight_color)
        renders: list[CaptionRender] = []
        for i, chunk in enumerate(chunks):
            for r in _karaoke_chunk_renders(
                chunk, i, config, preset, highlight_rgba,
                out_w, out_h, cache_dir, voiceover_offset, fontfile, _place,
            ):
                renders.append(r)
        return renders

    renders = []
    for i, chunk in enumerate(chunks):
        png_path = cache_dir / _png_filename(
            chunk, config.style, preset.font_size, out_w, out_h,
        )
        png_w, png_h = _render_caption_png(
            text=chunk.text,
            style=preset,
            out_w=out_w,
            fontfile=fontfile,
            dest=png_path,
        )
        x, y = _place(png_w, png_h)
        renders.append(CaptionRender(
            chunk_index=i,
            png_path=png_path,
            x=x,
            y=y,
            start=chunk.start + voiceover_offset,
            end=chunk.end + voiceover_offset,
        ))
    return renders


def _karaoke_chunk_renders(
    chunk: CaptionChunk,
    chunk_index: int,
    config: EDLCaptions,
    preset: CaptionVisualStyle,
    highlight: RGBA,
    out_w: int,
    out_h: int,
    cache_dir: Path,
    voiceover_offset: float,
    fontfile: str | None,
    place,
) -> list[CaptionRender]:
    """One render per word: a progressively-filled variant of the whole phrase,
    each visible for that word's spoken window."""
    words = chunk.words
    if not words:
        png_path = cache_dir / _png_filename(
            chunk, config.style, preset.font_size, out_w, out_h,
        )
        png_w, png_h = _render_caption_png(
            text=chunk.text, style=preset, out_w=out_w,
            fontfile=fontfile, dest=png_path,
        )
        x, y = place(png_w, png_h)
        return [CaptionRender(
            chunk_index=chunk_index, png_path=png_path, x=x, y=y,
            start=chunk.start + voiceover_offset, end=chunk.end + voiceover_offset,
        )]

    word_texts = [w.text for w in words]
    renders: list[CaptionRender] = []
    for j, word in enumerate(words):
        png_path = cache_dir / _karaoke_png_filename(
            chunk, j, config.style, preset.font_size,
            config.highlight_color, out_w, out_h,
        )
        png_w, png_h = _render_caption_karaoke_png(
            words=word_texts, active_index=j, style=preset, highlight=highlight,
            out_w=out_w, fontfile=fontfile, dest=png_path,
        )
        x, y = place(png_w, png_h)
        window_end = words[j + 1].start if j + 1 < len(words) else chunk.end
        renders.append(CaptionRender(
            chunk_index=chunk_index, png_path=png_path, x=x, y=y,
            start=word.start + voiceover_offset,
            end=window_end + voiceover_offset,
        ))
    return renders


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
