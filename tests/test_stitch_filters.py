from __future__ import annotations

from pathlib import Path

import pytest

from supaclip.core.edl import EDLOSTCue
from supaclip.stitch.overlay import (
    POSITION_Y_FRACTION,
    STYLE_PRESETS,
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


def test_all_styles_have_presets():
    for style in ("dark", "light", "yellow_punch", "red_alert", "pink_reveal"):
        assert style in STYLE_PRESETS


def test_position_fractions_cover_all_positions():
    assert set(POSITION_Y_FRACTION) == {"top", "middle", "bottom"}


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
