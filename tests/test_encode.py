from __future__ import annotations

import pytest

from supaclip.core.edl import EDL, EDLAnnotation, EDLOutput, EDLVideoCue, EDLWatermark
from supaclip.stitch.encode import (
    build_video_encode_args,
    resolution_scale_factor,
    scale_edl,
    select_encoder,
)


def test_libx264_uses_preset_and_crf():
    args = build_video_encode_args("libx264", "slow", 18)
    assert args == ["-c:v", "libx264", "-preset", "slow", "-crf", "18"]


def test_nvenc_uses_cq_and_mapped_preset():
    args = build_video_encode_args("h264_nvenc", "medium", 20)
    assert args[:2] == ["-c:v", "h264_nvenc"]
    assert "-cq" in args and "20" in args
    assert "p4" in args


def test_videotoolbox_uses_quality():
    args = build_video_encode_args("hevc_videotoolbox", "medium", 0)
    assert args[:2] == ["-c:v", "hevc_videotoolbox"]
    assert "-q:v" in args
    # crf 0 (best) maps to the top of the quality scale
    assert args[args.index("-q:v") + 1] == "100"


def test_qsv_uses_global_quality():
    args = build_video_encode_args("h264_qsv", "fast", 23)
    assert "-global_quality" in args and "23" in args


def test_unknown_encoder_raises():
    with pytest.raises(ValueError):
        build_video_encode_args("nope", "medium", 20)


_PROBE_OK = lambda codec: True
_PROBE_FAIL = lambda codec: False


def test_select_encoder_auto_prefers_working_hardware():
    avail = {"libx264", "h264_nvenc", "hevc_nvenc"}
    assert select_encoder("auto", avail, probe=_PROBE_OK) == "h264_nvenc"


def test_select_encoder_auto_skips_unusable_hardware():
    avail = {"libx264", "h264_nvenc"}
    assert select_encoder("auto", avail, probe=_PROBE_FAIL) == "libx264"


def test_select_encoder_auto_falls_back_to_libx264():
    assert select_encoder("auto", {"libx264"}, probe=_PROBE_OK) == "libx264"


def test_select_encoder_explicit_unavailable_raises():
    with pytest.raises(ValueError):
        select_encoder("h264_nvenc", {"libx264"}, probe=_PROBE_OK)


def test_select_encoder_explicit_present_but_unusable_raises():
    with pytest.raises(ValueError):
        select_encoder("h264_nvenc", {"libx264", "h264_nvenc"}, probe=_PROBE_FAIL)


def test_select_encoder_explicit_available():
    assert select_encoder("libx264", {"libx264", "h264_nvenc"}, probe=_PROBE_OK) == "libx264"


def test_resolution_scale_factor_vertical():
    assert resolution_scale_factor(1080, 1920, "4k") == pytest.approx(2.0)
    assert resolution_scale_factor(1080, 1920, "720p") == pytest.approx(2 / 3)


def _edl() -> EDL:
    return EDL(
        title="t",
        output=EDLOutput(width=1080, height=1920, fps=60, duration=4.0,
                         watermark=EDLWatermark(text="brand", font_size=36)),
        video=[EDLVideoCue(start=0.0, end=4.0, clip_id=1, reframe_offset=100)],
        annotations=[EDLAnnotation(start=0, end=2, shape="circle", x=540, y=700,
                                   radius=180, stroke_width=8)],
    )


def test_scale_edl_doubles_pixel_fields():
    scaled = scale_edl(_edl(), 2.0)
    assert (scaled.output.width, scaled.output.height) == (2160, 3840)
    assert scaled.output.watermark.font_size == 72
    assert scaled.video[0].reframe_offset == 200
    ann = scaled.annotations[0]
    assert (ann.x, ann.y, ann.radius, ann.stroke_width) == (1080, 1400, 360, 16)


def test_scale_edl_keeps_even_dimensions():
    edl = _edl().model_copy(update={
        "output": EDLOutput(width=1081, height=1921, fps=60, duration=4.0)
    })
    scaled = scale_edl(edl, 1.5)
    assert scaled.output.width % 2 == 0
    assert scaled.output.height % 2 == 0


def test_scale_edl_identity_factor_returns_same():
    edl = _edl()
    assert scale_edl(edl, 1.0) is edl
