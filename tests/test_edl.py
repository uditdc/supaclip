from __future__ import annotations

from dataclasses import dataclass

import pytest

from supaclip.core.edl import (
    EDL,
    EDL_SCHEMA_VERSION,
    EDLAnnotation,
    EDLAudioCue,
    EDLMusic,
    EDLOSTCue,
    EDLOutput,
    EDLVideoCue,
    EDLVoiceover,
    load_edl,
    save_edl,
    validate_edl,
)


def _basic_edl(duration: float = 10.0, with_voiceover: bool = True) -> EDL:
    return EDL(
        title="t",
        output=EDLOutput(duration=duration),
        voiceover=EDLVoiceover(voice_id="v1", script="hi") if with_voiceover else None,
        video=[
            EDLVideoCue(start=0.0, end=5.0, clip_id=1),
            EDLVideoCue(start=5.0, end=duration, clip_id=2),
        ],
        audio=[EDLAudioCue(start=0.0, end=duration, kind="voiceover")] if with_voiceover else [],
        ost=[EDLOSTCue(start=0.0, end=2.0, text="HOOK", style="yellow_punch")],
    )


def test_roundtrip(tmp_path):
    edl = _basic_edl()
    path = tmp_path / "edl.json"
    save_edl(edl, path)
    assert load_edl(path) == edl


def test_extra_fields_rejected():
    with pytest.raises(Exception):
        EDL.model_validate({
            "schema_version": 1,
            "title": "t",
            "output": {"duration": 5.0},
            "video": [],
            "audio": [],
            "ost": [],
            "bogus": True,
        })


def test_validate_clean_edl_has_no_errors():
    issues = validate_edl(_basic_edl())
    assert [i for i in issues if i.severity == "error"] == []


def test_validate_detects_gap():
    edl = _basic_edl()
    edl.video[1] = EDLVideoCue(start=6.0, end=10.0, clip_id=2)
    issues = validate_edl(edl)
    assert any("gap" in i.message for i in issues if i.severity == "error")


def test_validate_detects_overlap():
    edl = _basic_edl()
    edl.video[1] = EDLVideoCue(start=4.0, end=10.0, clip_id=2)
    issues = validate_edl(edl)
    assert any("overlap" in i.message for i in issues if i.severity == "error")


def test_validate_detects_short_track():
    edl = _basic_edl()
    edl.video.pop()
    issues = validate_edl(edl)
    assert any("ends at" in i.message for i in issues if i.severity == "error")


def test_validate_voiceover_referenced_but_missing():
    edl = _basic_edl(with_voiceover=False)
    edl.audio.append(EDLAudioCue(start=0.0, end=10.0, kind="voiceover"))
    issues = validate_edl(edl)
    assert any("voiceover" in i.path for i in issues if i.severity == "error")


def test_validate_voiceover_unused_warns():
    edl = _basic_edl()
    edl.audio = []
    issues = validate_edl(edl)
    assert any(i.severity == "warning" and "voiceover" in i.path for i in issues)


def test_validate_end_exceeds_duration():
    edl = _basic_edl()
    edl.ost[0] = EDLOSTCue(start=0.0, end=99.0, text="x")
    issues = validate_edl(edl)
    assert any("exceeds output.duration" in i.message for i in issues)


def test_validate_zero_length_cue():
    edl = _basic_edl()
    edl.ost.append(EDLOSTCue(start=3.0, end=3.0, text="x"))
    issues = validate_edl(edl)
    assert any("end <= start" in i.message for i in issues)


def test_schema_version_default():
    edl = _basic_edl()
    assert edl.schema_version == EDL_SCHEMA_VERSION


def test_validate_unsupported_schema_version():
    edl = _basic_edl()
    edl.schema_version = 99
    issues = validate_edl(edl)
    assert any(i.path == "schema_version" for i in issues)


@dataclass
class _FakeClip:
    duration: float
    source_in: float = 0.0


def test_validate_resolver_missing_clip():
    edl = _basic_edl()
    issues = validate_edl(edl, resolver=lambda cid: None)
    missing = [i for i in issues if "not found" in i.message]
    assert len(missing) == 2


def test_validate_resolver_cue_exceeds_clip():
    edl = _basic_edl()
    catalog = {1: _FakeClip(duration=5.0), 2: _FakeClip(duration=3.0)}
    issues = validate_edl(edl, resolver=catalog.get)
    assert any("exceeds available clip footage" in i.message for i in issues)


def test_validate_resolver_fits():
    edl = _basic_edl()
    catalog = {1: _FakeClip(duration=20.0), 2: _FakeClip(duration=20.0)}
    issues = validate_edl(edl, resolver=catalog.get)
    assert [i for i in issues if i.severity == "error"] == []


def test_default_cue_has_no_effect_and_cut_transition():
    cue = EDLVideoCue(start=0, end=1, clip_id=1)
    assert cue.effect == "none"
    assert cue.transition_in == "cut"
    assert cue.transition_duration == 0.0
    assert cue.reframe_offset == 0


def test_validate_crossfade_too_long_errors():
    edl = _basic_edl()
    edl.video[1] = EDLVideoCue(start=5.0, end=10.0, clip_id=2,
                               transition_in="crossfade", transition_duration=5.0)
    issues = validate_edl(edl)
    assert any("exceeds half" in i.message for i in issues)


def test_validate_crossfade_zero_duration_errors():
    edl = _basic_edl()
    edl.video[1] = EDLVideoCue(start=5.0, end=10.0, clip_id=2,
                               transition_in="crossfade", transition_duration=0.0)
    issues = validate_edl(edl)
    assert any("must be > 0" in i.message and "transition_duration" in i.path
               for i in issues)


def test_validate_crossfade_within_bounds_ok():
    edl = _basic_edl()
    edl.video[1] = EDLVideoCue(start=5.0, end=10.0, clip_id=2,
                               transition_in="crossfade", transition_duration=0.5)
    issues = validate_edl(edl)
    assert [i for i in issues if i.severity == "error"] == []


def test_validate_slow_mo_speed_range():
    edl = _basic_edl()
    edl.video[0] = EDLVideoCue(start=0.0, end=5.0, clip_id=1,
                               effect="slow_mo", effect_params={"speed": 2.0})
    issues = validate_edl(edl)
    assert any("slow_mo speed" in i.message for i in issues)


def test_validate_ken_burns_zoom_positive():
    edl = _basic_edl()
    edl.video[0] = EDLVideoCue(start=0.0, end=5.0, clip_id=1,
                               effect="ken_burns_in",
                               effect_params={"zoom_from": -1.0, "zoom_to": 1.2})
    issues = validate_edl(edl)
    assert any("zoom_from/zoom_to" in i.message for i in issues)


def test_validate_annotation_in_bounds():
    edl = _basic_edl()
    edl.annotations.append(EDLAnnotation(
        start=0.0, end=2.0, shape="circle", x=540, y=700, radius=180
    ))
    issues = validate_edl(edl)
    assert [i for i in issues if i.severity == "error"] == []


def test_validate_annotation_out_of_bounds_errors():
    edl = _basic_edl()
    edl.annotations.append(EDLAnnotation(
        start=0.0, end=2.0, shape="circle", x=5000, y=700, radius=180
    ))
    issues = validate_edl(edl)
    assert any("outside" in i.message for i in issues)


def test_validate_annotation_shape_requires_dimensions():
    edl = _basic_edl()
    edl.annotations.append(EDLAnnotation(
        start=0.0, end=2.0, shape="circle", x=540, y=700, radius=0
    ))
    issues = validate_edl(edl)
    assert any("radius > 0" in i.message for i in issues)


def test_validate_music_duck_without_voiceover_warns():
    edl = _basic_edl(with_voiceover=False)
    edl.music = EDLMusic(file="/tmp/m.mp3", duck=True)
    issues = validate_edl(edl)
    assert any(i.severity == "warning" and "music.duck" in i.path for i in issues)


def test_v11_roundtrip_with_all_new_fields(tmp_path):
    edl = _basic_edl()
    edl.video[0] = EDLVideoCue(start=0.0, end=5.0, clip_id=1,
                               effect="ken_burns_in",
                               effect_params={"zoom_from": 1.0, "zoom_to": 1.15})
    edl.video[1] = EDLVideoCue(start=5.0, end=10.0, clip_id=2,
                               transition_in="crossfade", transition_duration=0.5,
                               reframe_offset=120)
    edl.annotations = [EDLAnnotation(start=1.0, end=3.0, shape="circle",
                                      x=540, y=700, radius=180)]
    edl.music = EDLMusic(file="/tmp/m.mp3", level_db=-20.0, duck=True)
    p = tmp_path / "edl.json"
    save_edl(edl, p)
    loaded = load_edl(p)
    assert loaded == edl
