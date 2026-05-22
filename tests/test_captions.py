from __future__ import annotations

from pathlib import Path

import pytest

from supaclip.core.edl import (
    EDL,
    EDLAudioCue,
    EDLCaptions,
    EDLOutput,
    EDLVideoCue,
    EDLVoiceover,
    validate_edl,
)
from supaclip.stitch.captions import (
    CAPTION_POSITION_FRACTION,
    CAPTION_STYLE_PRESETS,
    build_caption_overlay_chain,
    chunk_alignment,
    render_caption_pngs,
)
from supaclip.stitch.tts.base import Alignment


def _alignment_from_text(text: str, char_dur: float = 0.05) -> Alignment:
    starts: list[float] = []
    ends: list[float] = []
    t = 0.0
    for _ in text:
        starts.append(round(t, 4))
        t += char_dur
        ends.append(round(t, 4))
    return Alignment(characters=list(text), start_times=starts, end_times=ends)


def test_chunk_alignment_breaks_on_hard_punctuation():
    align = _alignment_from_text("Hello world. How are you?")
    chunks = chunk_alignment(align, max_words=10, max_chars=100,
                              min_chunk_duration=0.0)
    assert [c.text for c in chunks] == ["Hello world.", "How are you?"]


def test_chunk_alignment_breaks_on_word_limit():
    align = _alignment_from_text("one two three four five six")
    chunks = chunk_alignment(align, max_words=3, max_chars=100,
                              min_chunk_duration=0.0)
    assert [c.text for c in chunks] == ["one two three", "four five six"]


def test_chunk_alignment_breaks_on_char_limit():
    align = _alignment_from_text("aaa bbb ccc ddd eee fff ggg hhh iii")
    chunks = chunk_alignment(align, max_words=20, max_chars=8,
                              min_chunk_duration=0.0)
    assert all(len(c.text) <= 12 for c in chunks)
    assert len(chunks) >= 3


def test_chunk_alignment_min_duration_extends_short_chunks():
    align = _alignment_from_text("Hi.", char_dur=0.05)
    chunks = chunk_alignment(align, max_words=4, max_chars=28,
                              min_chunk_duration=1.0)
    assert chunks[0].end - chunks[0].start >= 1.0


def test_chunk_alignment_drops_punctuation_only_chunks():
    align = _alignment_from_text("Hello world... Yes.")
    chunks = chunk_alignment(align, min_chunk_duration=0.0)
    for c in chunks:
        assert any(ch.isalnum() for ch in c.text), \
            f"punctuation-only chunk leaked: {c.text!r}"


def test_chunk_alignment_empty():
    align = Alignment(characters=[], start_times=[], end_times=[])
    assert chunk_alignment(align) == []


def test_chunk_alignment_no_overlap_between_chunks():
    align = _alignment_from_text("Hi. Yo.")
    chunks = chunk_alignment(align, min_chunk_duration=5.0)
    for a, b in zip(chunks, chunks[1:]):
        assert a.end <= b.start + 1e-6


def test_render_caption_pngs_writes_files(tmp_path: Path):
    align = _alignment_from_text("one two three four five six")
    chunks = chunk_alignment(align, max_words=3, max_chars=100,
                              min_chunk_duration=0.0)
    cfg = EDLCaptions(style="clean_white", position="lower_third")
    renders = render_caption_pngs(
        chunks=chunks, config=cfg,
        out_w=1080, out_h=1920, cache_dir=tmp_path,
    )
    assert len(renders) == len(chunks)
    for r in renders:
        assert r.png_path.exists()
        assert 0 <= r.x <= 1080
        assert 0 <= r.y <= 1920


def test_render_caption_pngs_applies_voiceover_offset(tmp_path: Path):
    align = _alignment_from_text("Hi there.")
    chunks = chunk_alignment(align, min_chunk_duration=0.0)
    cfg = EDLCaptions()
    renders = render_caption_pngs(
        chunks=chunks, config=cfg,
        out_w=1080, out_h=1920, cache_dir=tmp_path,
        voiceover_offset=3.0,
    )
    assert renders[0].start >= 3.0


def test_render_caption_pngs_respects_position(tmp_path: Path):
    align = _alignment_from_text("Top text.")
    chunks = chunk_alignment(align, min_chunk_duration=0.0)
    top_cfg = EDLCaptions(position="top")
    bottom_cfg = EDLCaptions(position="bottom")
    [top] = render_caption_pngs(
        chunks=chunks, config=top_cfg,
        out_w=1080, out_h=1920, cache_dir=tmp_path,
    )
    [bottom] = render_caption_pngs(
        chunks=chunks, config=bottom_cfg,
        out_w=1080, out_h=1920, cache_dir=tmp_path,
    )
    assert top.y < bottom.y


def test_render_caption_pngs_font_size_override(tmp_path: Path):
    align = _alignment_from_text("Hi.")
    chunks = chunk_alignment(align, min_chunk_duration=0.0)
    small = EDLCaptions(font_size=32)
    big = EDLCaptions(font_size=96)
    [sr] = render_caption_pngs(chunks=chunks, config=small,
                                 out_w=1080, out_h=1920, cache_dir=tmp_path)
    [br] = render_caption_pngs(chunks=chunks, config=big,
                                 out_w=1080, out_h=1920, cache_dir=tmp_path)
    assert sr.png_path != br.png_path


def test_build_caption_overlay_chain_empty():
    chains = build_caption_overlay_chain(
        renders=[], input_indices=[],
        base_label="[vost_out]", final_label="[vout]",
    )
    assert chains == ["[vost_out]null[vout]"]


def test_build_caption_overlay_chain_chains_multiple(tmp_path: Path):
    align = _alignment_from_text("one. two. three.")
    chunks = chunk_alignment(align, min_chunk_duration=0.0)
    renders = render_caption_pngs(
        chunks=chunks, config=EDLCaptions(),
        out_w=1080, out_h=1920, cache_dir=tmp_path,
    )
    chains = build_caption_overlay_chain(
        renders=renders, input_indices=[10, 11, 12],
        base_label="[vost_out]", final_label="[vout]",
    )
    assert len(chains) == len(renders)
    assert chains[0].startswith("[vost_out][10:v]overlay=")
    assert chains[-1].endswith("[vout]")
    assert "[vcap0]" in chains[1]


def test_all_styles_and_positions_have_presets():
    for s in ("clean_white", "boxed_dark", "karaoke_yellow"):
        assert s in CAPTION_STYLE_PRESETS
    for p in ("top", "middle", "bottom", "lower_third"):
        assert p in CAPTION_POSITION_FRACTION


def _make_edl(captions: EDLCaptions | None = None,
              voiceover: EDLVoiceover | None = None) -> EDL:
    return EDL(
        title="t",
        output=EDLOutput(duration=2.0),
        voiceover=voiceover,
        video=[EDLVideoCue(start=0.0, end=2.0, clip_id=1)],
        audio=[EDLAudioCue(start=0.0, end=2.0, kind="voiceover")]
              if voiceover else [],
        captions=captions,
    )


def test_validate_captions_requires_voiceover():
    edl = _make_edl(captions=EDLCaptions(), voiceover=None)
    issues = validate_edl(edl)
    paths = [i.path for i in issues if i.severity == "error"]
    assert "captions" in paths


def test_validate_captions_with_voiceover_ok():
    vo = EDLVoiceover(voice_id="v", script="hi")
    edl = _make_edl(captions=EDLCaptions(), voiceover=vo)
    issues = validate_edl(edl)
    cap_errors = [i for i in issues if i.severity == "error"
                  and i.path.startswith("captions")]
    assert cap_errors == []


def test_validate_captions_rejects_zero_max_words():
    vo = EDLVoiceover(voice_id="v", script="hi")
    edl = _make_edl(captions=EDLCaptions(max_words=0), voiceover=vo)
    issues = validate_edl(edl)
    assert any(i.path == "captions.max_words" and i.severity == "error"
               for i in issues)
