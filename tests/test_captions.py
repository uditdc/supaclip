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
    _hex_to_rgba,
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


def test_chunk_alignment_attaches_word_timing():
    align = _alignment_from_text("one two three", char_dur=0.1)
    [chunk] = chunk_alignment(align, max_words=10, max_chars=100,
                              min_chunk_duration=0.0)
    assert [w.text for w in chunk.words] == ["one", "two", "three"]
    assert chunk.words[0].start == pytest.approx(chunk.start)
    for a, b in zip(chunk.words, chunk.words[1:]):
        assert a.start < b.start


def test_hex_to_rgba():
    assert _hex_to_rgba("#FFD600") == (255, 214, 0, 255)
    assert _hex_to_rgba("#00000080") == (0, 0, 0, 128)


def test_karaoke_fill_emits_one_render_per_word(tmp_path: Path):
    align = _alignment_from_text("one two three", char_dur=0.1)
    chunks = chunk_alignment(align, max_words=10, max_chars=100,
                             min_chunk_duration=0.0)
    cfg = EDLCaptions(highlight="karaoke_fill")
    renders = render_caption_pngs(
        chunks=chunks, config=cfg,
        out_w=1080, out_h=1920, cache_dir=tmp_path,
    )
    assert len(renders) == 3
    for r in renders:
        assert r.png_path.exists()


def test_karaoke_fill_windows_are_contiguous_and_share_position(tmp_path: Path):
    align = _alignment_from_text("one two three", char_dur=0.1)
    [chunk] = chunk_alignment(align, max_words=10, max_chars=100,
                              min_chunk_duration=0.0)
    cfg = EDLCaptions(highlight="karaoke_fill")
    renders = render_caption_pngs(
        chunks=[chunk], config=cfg,
        out_w=1080, out_h=1920, cache_dir=tmp_path,
    )
    assert renders[0].start == pytest.approx(chunk.start)
    assert renders[-1].end == pytest.approx(chunk.end)
    for a, b in zip(renders, renders[1:]):
        assert a.end == pytest.approx(b.start)
    assert len({(r.x, r.y) for r in renders}) == 1


def test_karaoke_variants_have_distinct_pngs(tmp_path: Path):
    align = _alignment_from_text("one two three", char_dur=0.1)
    chunks = chunk_alignment(align, max_words=10, max_chars=100,
                             min_chunk_duration=0.0)
    renders = render_caption_pngs(
        chunks=chunks, config=EDLCaptions(highlight="karaoke_fill"),
        out_w=1080, out_h=1920, cache_dir=tmp_path,
    )
    assert len({r.png_path for r in renders}) == 3


def test_validate_rejects_bad_highlight_color():
    vo = EDLVoiceover(voice_id="v", script="hi")
    edl = _make_edl(
        captions=EDLCaptions(highlight="karaoke_fill", highlight_color="yellow"),
        voiceover=vo,
    )
    issues = validate_edl(edl)
    assert any(i.path == "captions.highlight_color" and i.severity == "error"
               for i in issues)


def test_all_styles_and_positions_have_presets():
    for s in ("clean_white", "boxed_dark", "karaoke_yellow"):
        assert s in CAPTION_STYLE_PRESETS
    for p in ("top", "middle", "bottom", "lower_third"):
        assert p in CAPTION_POSITION_FRACTION


def test_default_render_is_byte_stable_across_runs(tmp_path: Path):
    # "defaults unchanged" within the new baseline: shadow-off / animation-off
    # output is deterministic run-to-run (pinned bundled font, no randomness).
    chunks = chunk_alignment(_alignment_from_text("Stable baseline."),
                             min_chunk_duration=0.0)
    a = tmp_path / "a"
    b = tmp_path / "b"
    [ra] = render_caption_pngs(chunks=chunks, config=EDLCaptions(), out_w=1080,
                               out_h=1920, cache_dir=a)
    [rb] = render_caption_pngs(chunks=chunks, config=EDLCaptions(), out_w=1080,
                               out_h=1920, cache_dir=b)
    assert ra.png_path.name == rb.png_path.name
    assert ra.png_path.read_bytes() == rb.png_path.read_bytes()


def test_defaults_emit_one_render_per_chunk(tmp_path: Path):
    align = _alignment_from_text("one two three four. five six seven eight.")
    chunks = chunk_alignment(align, min_chunk_duration=0.0)
    renders = render_caption_pngs(chunks=chunks, config=EDLCaptions(),
                                  out_w=1080, out_h=1920, cache_dir=tmp_path)
    assert len(renders) == len(chunks)


def test_resolution_aware_caption_sizing(tmp_path: Path):
    chunks = chunk_alignment(_alignment_from_text("Size."), min_chunk_duration=0.0)
    from PIL import Image
    [hd] = render_caption_pngs(chunks=chunks, config=EDLCaptions(style="clean_white"),
                               out_w=1080, out_h=1920, cache_dir=tmp_path)
    [uhd] = render_caption_pngs(chunks=chunks, config=EDLCaptions(style="clean_white"),
                                out_w=2160, out_h=3840, cache_dir=tmp_path)
    h1, h2 = Image.open(hd.png_path).height, Image.open(uhd.png_path).height
    assert h2 / h1 == pytest.approx(2.0, abs=0.12)


def test_karaoke_pop_adds_overlays_per_word(tmp_path: Path):
    align = _alignment_from_text("one two three", char_dur=0.15)
    chunks = chunk_alignment(align, max_words=10, max_chars=100, min_chunk_duration=0.0)
    plain = render_caption_pngs(
        chunks=chunks, config=EDLCaptions(highlight="karaoke_fill"),
        out_w=1080, out_h=1920, cache_dir=tmp_path)
    popped = render_caption_pngs(
        chunks=chunks,
        config=EDLCaptions(highlight="karaoke_fill", animate="pop",
                           animate_duration=0.12, animate_overshoot=0.15),
        out_w=1080, out_h=1920, cache_dir=tmp_path, fps=30)
    assert len(popped) > len(plain)          # base words + pop overlays
    assert len({r.x for r in popped}) == 1   # overlays share the phrase anchor
    assert all(r.png_path.exists() for r in popped)


def test_karaoke_pop_overlay_windows_are_within_word(tmp_path: Path):
    align = _alignment_from_text("one two three", char_dur=0.3)
    [chunk] = chunk_alignment(align, max_words=10, max_chars=100, min_chunk_duration=0.0)
    cfg = EDLCaptions(highlight="karaoke_fill", animate="pop",
                      animate_duration=0.15, animate_overshoot=0.2)
    renders = render_caption_pngs(chunks=[chunk], config=cfg,
                                  out_w=1080, out_h=1920, cache_dir=tmp_path, fps=30)
    # every render stays inside the chunk window and starts are non-decreasing
    for r in renders:
        assert chunk.start - 1e-6 <= r.start <= chunk.end + 1e-6
    starts = [r.start for r in renders]
    assert starts == sorted(starts)


def test_active_word_pill_changes_render(tmp_path: Path):
    align = _alignment_from_text("one two three", char_dur=0.15)
    chunks = chunk_alignment(align, max_words=10, max_chars=100, min_chunk_duration=0.0)
    no_pill = render_caption_pngs(
        chunks=chunks, config=EDLCaptions(highlight="karaoke_fill"),
        out_w=1080, out_h=1920, cache_dir=tmp_path)
    pill = render_caption_pngs(
        chunks=chunks,
        config=EDLCaptions(highlight="karaoke_fill", active_word_bg="#1E90FF",
                           active_word_bg_radius=14),
        out_w=1080, out_h=1920, cache_dir=tmp_path)
    # a pill preset produces distinct PNGs from the no-pill baseline
    assert {r.png_path.name for r in pill}.isdisjoint({r.png_path.name for r in no_pill})


def test_default_word_spacing_widens_karaoke(tmp_path: Path, monkeypatch):
    from PIL import Image

    import supaclip.stitch.captions as cap
    align = _alignment_from_text("one two three", char_dur=0.2)
    [chunk] = chunk_alignment(align, max_words=10, max_chars=100, min_chunk_duration=0.0)
    cfg = EDLCaptions(highlight="karaoke_fill")

    monkeypatch.setattr(cap, "WORD_SPACING_FRAC", 0.0)
    tight = render_caption_pngs(chunks=[chunk], config=cfg, out_w=1080, out_h=1920,
                                cache_dir=tmp_path / "tight")
    monkeypatch.setattr(cap, "WORD_SPACING_FRAC", 0.18)
    spaced = render_caption_pngs(chunks=[chunk], config=cfg, out_w=1080, out_h=1920,
                                 cache_dir=tmp_path / "spaced")
    assert Image.open(spaced[0].png_path).width > Image.open(tight[0].png_path).width


def test_active_word_highlight_zooms_current_word(tmp_path: Path):
    align = _alignment_from_text("alpha bravo charlie", char_dur=0.2)
    chunks = chunk_alignment(align, max_words=10, max_chars=100, min_chunk_duration=0.0)
    fill = render_caption_pngs(
        chunks=chunks, config=EDLCaptions(style="clean_white", highlight="karaoke_fill"),
        out_w=1080, out_h=1920, cache_dir=tmp_path)
    active = render_caption_pngs(
        chunks=chunks,
        config=EDLCaptions(style="clean_white", highlight="active_word",
                           highlight_color="#39FF14"),
        out_w=1080, out_h=1920, cache_dir=tmp_path, fps=30)
    # active_word adds a persistent zoom overlay per word (default scale > 1)
    assert len(active) == 2 * len(fill)
    assert all(r.png_path.exists() for r in active)
    assert len({r.x for r in active}) == 1
    # its base renders differ from karaoke_fill (only the current word is lit)
    assert {r.png_path.name for r in active}.isdisjoint({r.png_path.name for r in fill})


def test_active_word_persistent_overlay_covers_word_window(tmp_path: Path):
    align = _alignment_from_text("alpha bravo charlie", char_dur=0.3)
    [chunk] = chunk_alignment(align, max_words=10, max_chars=100, min_chunk_duration=0.0)
    renders = render_caption_pngs(
        chunks=[chunk],
        config=EDLCaptions(style="clean_white", highlight="active_word"),
        out_w=1080, out_h=1920, cache_dir=tmp_path, fps=30)
    # every render stays within the chunk; the zoom holds for the whole word
    assert renders[0].start == pytest.approx(chunk.start)
    assert renders[-1].end == pytest.approx(chunk.end)
    for r in renders:
        assert chunk.start - 1e-6 <= r.start < r.end <= chunk.end + 1e-6


def test_active_word_scale_1_is_color_only(tmp_path: Path):
    align = _alignment_from_text("alpha bravo charlie", char_dur=0.2)
    chunks = chunk_alignment(align, max_words=10, max_chars=100, min_chunk_duration=0.0)
    renders = render_caption_pngs(
        chunks=chunks,
        config=EDLCaptions(style="clean_white", highlight="active_word",
                           active_word_scale=1.0),
        out_w=1080, out_h=1920, cache_dir=tmp_path, fps=30)
    # scale exactly 1.0 => recolor only, no zoom overlays (one render per word)
    assert len(renders) == 3


def test_karaoke_fill_zoom_is_opt_in(tmp_path: Path):
    align = _alignment_from_text("alpha bravo charlie", char_dur=0.2)
    chunks = chunk_alignment(align, max_words=10, max_chars=100, min_chunk_duration=0.0)
    plain = render_caption_pngs(
        chunks=chunks, config=EDLCaptions(highlight="karaoke_fill"),
        out_w=1080, out_h=1920, cache_dir=tmp_path)
    zoomed = render_caption_pngs(
        chunks=chunks,
        config=EDLCaptions(highlight="karaoke_fill", active_word_scale=1.18),
        out_w=1080, out_h=1920, cache_dir=tmp_path, fps=30)
    assert len(plain) == 3          # default fill: no zoom overlays
    assert len(zoomed) == 6         # explicit scale zooms the active word too


def test_plain_fade_splits_into_alpha_windows(tmp_path: Path):
    chunks = chunk_alignment(_alignment_from_text("Fade me in and out."),
                             min_chunk_duration=1.0)
    plain = render_caption_pngs(chunks=chunks, config=EDLCaptions(),
                                out_w=1080, out_h=1920, cache_dir=tmp_path)
    faded = render_caption_pngs(chunks=chunks, config=EDLCaptions(fade_ms=120),
                                out_w=1080, out_h=1920, cache_dir=tmp_path, fps=30)
    assert len(faded) > len(plain)
    # windows stay contiguous and cover the same span
    assert faded[0].start == pytest.approx(plain[0].start)
    assert faded[-1].end == pytest.approx(plain[-1].end)
    for a, b in zip(faded, faded[1:]):
        assert a.end == pytest.approx(b.start)


def test_karaoke_pop_plus_fade_out_targets_chunk_end(tmp_path: Path):
    # regression: with pop overlays present, the trailing fade must apply to the
    # render that actually reaches chunk.end, not the last list entry (a pop
    # overlay sitting near the last word's start).
    align = _alignment_from_text("one two three", char_dur=0.4)
    [chunk] = chunk_alignment(align, max_words=10, max_chars=100, min_chunk_duration=0.0)
    cfg = EDLCaptions(highlight="karaoke_fill", animate="pop",
                      animate_duration=0.12, fade_ms=150)
    renders = render_caption_pngs(chunks=[chunk], config=cfg,
                                  out_w=1080, out_h=1920, cache_dir=tmp_path, fps=30)
    last_end = max(r.end for r in renders)
    assert last_end == pytest.approx(chunk.end)
    # the windows reaching the very end form a short alpha ramp (more than one)
    tail = [r for r in renders if r.end > chunk.end - 0.15 + 1e-6]
    assert len(tail) >= 2


def test_plain_pop_scales_in(tmp_path: Path):
    from PIL import Image
    chunks = chunk_alignment(_alignment_from_text("Pop this in now."),
                             min_chunk_duration=1.0)
    renders = render_caption_pngs(
        chunks=chunks,
        config=EDLCaptions(style="boxed_dark", animate="pop", animate_duration=0.15),
        out_w=1080, out_h=1920, cache_dir=tmp_path, fps=30)
    assert len(renders) > 1
    # the entrance frame is larger than the settled frame (grows from centre)
    assert Image.open(renders[0].png_path).width >= Image.open(renders[-1].png_path).width
    assert renders[-1].end == pytest.approx(chunks[0].end)


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
