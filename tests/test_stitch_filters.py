from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from supaclip.core.edl import EDLOSTCue
from supaclip.stitch.overlay import (
    BUNDLED_FONT_NAME,
    POSITION_Y_FRACTION,
    STYLE_PRESETS,
    _bundled_font_path,
    _resolve_font,
    build_ost_overlay_chain,
    render_caption_png,
    render_ost_pngs,
)
from supaclip.stitch.reframe import build_reframe_filter


def test_reframe_crop_center():
    f = build_reframe_filter("crop_center")
    assert "crop=min(in_w\\,in_h*1080/1920):min(in_h\\,in_w*1920/1080)" in f
    assert "(in_w-out_w)/2:(in_h-out_h)/2" in f
    assert "scale=1080:1920" in f
    assert "fps=60" in f
    assert "format=yuv420p" in f


def test_reframe_crop_left_right_have_offset_difference():
    left = build_reframe_filter("crop_left")
    right = build_reframe_filter("crop_right")
    assert ":0:(in_h-out_h)/2" in left
    assert ":in_w-out_w:(in_h-out_h)/2" in right


def test_reframe_letterbox_uses_pad():
    f = build_reframe_filter("letterbox")
    assert "force_original_aspect_ratio=decrease" in f
    assert "pad=1080:1920" in f
    assert "crop=" not in f


def test_reframe_custom_dimensions():
    f = build_reframe_filter("crop_center", dst_w=720, dst_h=1280, fps=30)
    assert "scale=720:1280" in f
    assert "crop=min(in_w\\,in_h*720/1280):min(in_h\\,in_w*1280/720)" in f
    assert "fps=30" in f


def test_reframe_zero_offset_unchanged():
    assert build_reframe_filter("crop_center", offset=0) == build_reframe_filter("crop_center")


def test_reframe_offset_pans_and_clamps():
    f = build_reframe_filter("crop_center", offset=120)
    assert "clip((in_w-out_w)/2+(120)\\,0\\,in_w-out_w)" in f
    neg = build_reframe_filter("crop_left", offset=-40)
    assert "clip(0+(-40)\\,0\\,in_w-out_w)" in neg


def test_reframe_offset_ignored_for_letterbox():
    assert build_reframe_filter("letterbox", offset=200) == build_reframe_filter("letterbox")


def test_all_styles_have_presets():
    for style in ("dark", "light", "yellow_punch", "red_alert", "pink_reveal",
                  "yellow_punch_shadow", "gradient_dark", "accent_bar"):
        assert style in STYLE_PRESETS


def test_new_presets_declare_their_effects():
    assert STYLE_PRESETS["yellow_punch_shadow"].shadow is not None
    assert STYLE_PRESETS["gradient_dark"].gradient_to is not None
    accent = STYLE_PRESETS["accent_bar"]
    assert accent.accent is not None and accent.accent_width > 0


def test_position_fractions_cover_all_positions():
    assert set(POSITION_Y_FRACTION) == {"top", "middle", "bottom"}


def test_bundled_font_is_the_default_when_none_passed():
    assert _bundled_font_path() is not None
    resolved = _resolve_font(None)
    assert resolved.endswith(BUNDLED_FONT_NAME)


def test_explicit_fontfile_overrides_bundled(tmp_path):
    # explicit but missing still raises (authoritative), never silently falls back
    with pytest.raises(FileNotFoundError):
        _resolve_font(str(tmp_path / "nope.ttf"))


def test_new_ost_presets_render(tmp_path: Path):
    for style in ("yellow_punch_shadow", "gradient_dark", "accent_bar"):
        cue = EDLOSTCue(start=0, end=2, text="Breaking News", style=style)
        [r] = render_ost_pngs([cue], out_w=1080, out_h=1920, cache_dir=tmp_path)
        assert r.png_path.exists()


def test_ost_no_animation_emits_single_render(tmp_path: Path):
    cue = EDLOSTCue(start=0.0, end=2.0, text="hi")
    renders = render_ost_pngs([cue], out_w=1080, out_h=1920, cache_dir=tmp_path)
    assert len(renders) == 1
    assert (renders[0].start, renders[0].end) == (0.0, 2.0)


def test_ost_pop_in_scales_and_settles(tmp_path: Path):
    cue = EDLOSTCue(start=1.0, end=4.0, text="POP", style="yellow_punch",
                    animate_in="pop", animate_duration=0.2)
    renders = render_ost_pngs([cue], out_w=1080, out_h=1920, cache_dir=tmp_path, fps=30)
    # several entrance windows then a settled span covering the remainder
    assert len(renders) > 1
    assert renders[0].start == pytest.approx(1.0)
    assert renders[-1].end == pytest.approx(4.0)
    for a, b in zip(renders, renders[1:]):
        assert a.end == pytest.approx(b.start)
    # the entrance overlay is drawn larger than the settled card (grows from centre)
    settled = Image.open(renders[-1].png_path)
    first = Image.open(renders[0].png_path)
    assert first.width >= settled.width


def test_ost_slide_up_moves_position_and_reuses_base(tmp_path: Path):
    cue = EDLOSTCue(start=0.0, end=2.0, text="slide", animate_in="slide_up",
                    animate_duration=0.3)
    renders = render_ost_pngs([cue], out_w=1080, out_h=1920, cache_dir=tmp_path, fps=30)
    ys = [r.y for r in renders]
    assert ys[0] > ys[-1]  # starts lower, settles up
    # slide reuses the settled PNG (no per-position re-raster)
    assert renders[0].png_path == renders[-1].png_path


def test_ost_resolution_aware_sizing(tmp_path: Path):
    cue = EDLOSTCue(start=0, end=2, text="Size", style="dark")
    [hd] = render_ost_pngs([cue], out_w=1080, out_h=1920, cache_dir=tmp_path)
    [uhd] = render_ost_pngs([cue], out_w=2160, out_h=3840, cache_dir=tmp_path)
    h1, h2 = Image.open(hd.png_path).height, Image.open(uhd.png_path).height
    assert h2 / h1 == pytest.approx(2.0, abs=0.1)


def test_render_caption_png_produces_file(tmp_path: Path):
    dest = tmp_path / "cap.png"
    w, h = render_caption_png(
        text="hello world",
        style_name="dark",
        out_w=1080,
        fontfile=None,
        dest=dest,
    )
    assert dest.exists()
    assert w > 0 and h > 0
    # rounded box fits within reasonable bounds
    assert w <= int(1080 * 0.86) + 8


def test_render_ost_pngs_returns_one_per_cue(tmp_path: Path):
    cues = [
        EDLOSTCue(start=0, end=1, text="A", style="dark", position="top"),
        EDLOSTCue(start=1, end=2, text="B", style="light", position="bottom"),
    ]
    renders = render_ost_pngs(cues, out_w=1080, out_h=1920, cache_dir=tmp_path)
    assert len(renders) == 2
    assert renders[0].y < renders[1].y  # top is above bottom
    assert all(r.png_path.exists() for r in renders)


def test_render_ost_pngs_centers_horizontally(tmp_path: Path):
    cues = [EDLOSTCue(start=0, end=1, text="Hi", style="dark", position="middle")]
    [r] = render_ost_pngs(cues, out_w=1080, out_h=1920, cache_dir=tmp_path)
    # x should be roughly centered (not at 0 or pinned to one edge)
    assert 50 < r.x < 1080 - 50


def test_render_ost_pngs_uses_content_hash_cache(tmp_path: Path):
    cue = EDLOSTCue(start=0, end=1, text="same", style="dark")
    [r1] = render_ost_pngs([cue], out_w=1080, out_h=1920, cache_dir=tmp_path)
    [r2] = render_ost_pngs([cue], out_w=1080, out_h=1920, cache_dir=tmp_path)
    assert r1.png_path == r2.png_path


def test_build_overlay_chain_handles_empty():
    chains = build_ost_overlay_chain(
        renders=[], input_indices=[], base_label="[vann]", final_label="[vout]"
    )
    assert chains == ["[vann]null[vout]"]


def test_build_overlay_chain_chains_multiple(tmp_path: Path):
    cues = [
        EDLOSTCue(start=0.0, end=1.0, text="A", style="dark"),
        EDLOSTCue(start=1.0, end=2.0, text="B", style="yellow_punch"),
    ]
    renders = render_ost_pngs(cues, out_w=1080, out_h=1920, cache_dir=tmp_path)
    chains = build_ost_overlay_chain(
        renders=renders, input_indices=[5, 6],
        base_label="[vann]", final_label="[vout]",
    )
    assert len(chains) == 2
    assert chains[0].startswith("[vann][5:v]overlay=")
    assert "enable='between(t,0.000,1.000)'" in chains[0]
    assert chains[1].startswith("[vost0][6:v]overlay=")
    assert chains[-1].endswith("[vout]")


def test_render_caption_png_fontfile_missing(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        render_caption_png(
            text="x",
            style_name="dark",
            out_w=1080,
            fontfile="/tmp/does-not-exist.ttf",
            dest=tmp_path / "x.png",
        )
