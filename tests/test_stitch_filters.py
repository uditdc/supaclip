from __future__ import annotations

from clipper.core.edl import EDLOSTCue
from clipper.stitch.overlay import (
    STYLE_PRESETS,
    _escape_drawtext,
    build_drawtext,
    build_ost_chain,
)
from clipper.stitch.reframe import build_reframe_filter


def test_reframe_crop_center():
    f = build_reframe_filter("crop_center")
    assert "scale=1080:1920:force_original_aspect_ratio=increase" in f
    assert "crop=1080:1920:(in_w-out_w)/2:(in_h-out_h)/2" in f
    assert "fps=60" in f
    assert "format=yuv420p" in f


def test_reframe_crop_left_right_have_offset_difference():
    left = build_reframe_filter("crop_left")
    right = build_reframe_filter("crop_right")
    assert "crop=1080:1920:0:" in left
    assert "crop=1080:1920:in_w-out_w:" in right


def test_reframe_letterbox_uses_pad():
    f = build_reframe_filter("letterbox")
    assert "force_original_aspect_ratio=decrease" in f
    assert "pad=1080:1920" in f
    assert "crop=" not in f


def test_reframe_custom_dimensions():
    f = build_reframe_filter("crop_center", dst_w=720, dst_h=1280, fps=30)
    assert "scale=720:1280" in f
    assert "crop=720:1280" in f
    assert "fps=30" in f


def test_drawtext_escapes_special_chars():
    cue = EDLOSTCue(start=0, end=1, text="A: B% C\\D", style="white_pop")
    out = build_drawtext(cue)
    assert r"\:" in out
    assert r"\%" in out
    assert r"\\" in out


def test_drawtext_emits_enable_window():
    cue = EDLOSTCue(start=1.5, end=4.25, text="x", style="white_pop")
    out = build_drawtext(cue)
    assert "enable='between(t,1.500,4.250)'" in out


def test_drawtext_includes_style_attrs():
    cue = EDLOSTCue(start=0, end=1, text="hi", style="bold_yellow")
    out = build_drawtext(cue)
    preset = STYLE_PRESETS["bold_yellow"]
    assert f"fontsize={preset.fontsize}" in out
    assert f"fontcolor={preset.fontcolor}" in out
    assert f"borderw={preset.borderw}" in out


def test_drawtext_box_only_when_preset_enables_it():
    no_box = build_drawtext(EDLOSTCue(start=0, end=1, text="x", style="white_pop"))
    with_box = build_drawtext(EDLOSTCue(start=0, end=1, text="x", style="comment_trap"))
    assert "box=1" not in no_box
    assert "box=1" in with_box


def test_drawtext_fontfile_optional():
    cue = EDLOSTCue(start=0, end=1, text="x", style="white_pop")
    assert "fontfile=" not in build_drawtext(cue)
    assert "fontfile=/usr/share/fonts/x.ttf" in build_drawtext(
        cue, fontfile="/usr/share/fonts/x.ttf"
    )


def test_ost_chain_joins_with_comma():
    cues = [
        EDLOSTCue(start=0, end=1, text="A", style="white_pop"),
        EDLOSTCue(start=1, end=2, text="B", style="bold_yellow"),
    ]
    chain = build_ost_chain(cues)
    assert chain.count("drawtext=") == 2
    assert ",drawtext=" in chain


def test_ost_chain_empty():
    assert build_ost_chain([]) == ""


def test_all_styles_have_presets():
    for style in ("bold_yellow", "red_strike", "neon_pink", "white_pop", "comment_trap"):
        assert style in STYLE_PRESETS


def test_escape_drawtext_idempotent_on_safe_text():
    assert _escape_drawtext("HELLO WORLD") == "HELLO WORLD"
