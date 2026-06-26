from __future__ import annotations

import pytest

from supaclip.core.edl import EDLAnnotation, EDLMusic, EDLVideoCue
from supaclip.stitch.annotation import build_annotation, build_annotation_chain
from supaclip.stitch.effects import plan_effect
from supaclip.stitch.music import build_music_plan, resolve_music_file
from supaclip.stitch.transitions import build_join_chain, needs_xfade_chain

# ---- effects ----

def test_plan_none_returns_cue_duration_and_empty_snippet():
    cue = EDLVideoCue(start=0.0, end=4.0, clip_id=1, effect="none")
    plan = plan_effect(cue, 1080, 1920, 60)
    assert plan.source_consumed == 4.0
    assert plan.filter_snippet == ""


def test_plan_freeze_first_consumes_tiny_source():
    cue = EDLVideoCue(start=0.0, end=2.0, clip_id=1, effect="freeze_first")
    plan = plan_effect(cue, 1080, 1920, 60)
    assert plan.source_consumed < 0.1
    assert "loop=loop=-1:size=1" in plan.filter_snippet
    assert "trim=duration=2.000" in plan.filter_snippet


def test_plan_slow_mo_consumes_speed_fraction():
    cue = EDLVideoCue(start=0.0, end=4.0, clip_id=1,
                     effect="slow_mo", effect_params={"speed": 0.5})
    plan = plan_effect(cue, 1080, 1920, 60)
    assert plan.source_consumed == pytest.approx(2.0)
    assert plan.filter_snippet == "setpts=PTS/0.5"


def test_plan_slow_mo_clamps_speed():
    cue = EDLVideoCue(start=0.0, end=4.0, clip_id=1,
                     effect="slow_mo", effect_params={"speed": 5.0})
    plan = plan_effect(cue, 1080, 1920, 60)
    assert "setpts=PTS/1.0" in plan.filter_snippet


def test_plan_ken_burns_in_zoom_expression():
    cue = EDLVideoCue(start=0.0, end=2.0, clip_id=1, effect="ken_burns_in")
    plan = plan_effect(cue, 1080, 1920, 60)
    assert "zoompan=z=" in plan.filter_snippet
    assert "1.0000" in plan.filter_snippet
    assert "1.1500" in plan.filter_snippet


# ---- transitions ----

def test_needs_xfade_chain_negative():
    cues = [EDLVideoCue(start=0, end=1, clip_id=1)]
    assert not needs_xfade_chain(cues)


def test_needs_xfade_chain_positive():
    cues = [
        EDLVideoCue(start=0, end=1, clip_id=1),
        EDLVideoCue(start=1, end=2, clip_id=2,
                   transition_in="crossfade", transition_duration=0.3),
    ]
    assert needs_xfade_chain(cues)


def test_join_chain_single_cue_uses_null():
    cues = [EDLVideoCue(start=0, end=1, clip_id=1)]
    j = build_join_chain(cues, ["[v0]"], [1.0])
    assert len(j.chains) == 1
    assert "null[vjoined]" in j.chains[0]
    assert j.final_label == "[vjoined]"


def test_join_chain_cut_uses_concat():
    cues = [
        EDLVideoCue(start=0, end=1, clip_id=1),
        EDLVideoCue(start=1, end=2, clip_id=2),
    ]
    j = build_join_chain(cues, ["[v0]", "[v1]"], [1.0, 1.0])
    assert "concat=n=2:v=1:a=0[vj1]" in j.chains[0]
    assert j.final_label == "[vj1]"


def test_join_chain_crossfade_offsets_correctly():
    cues = [
        EDLVideoCue(start=0, end=2.0, clip_id=1),
        EDLVideoCue(start=2.0, end=4.0, clip_id=2,
                   transition_in="crossfade", transition_duration=0.5),
    ]
    j = build_join_chain(cues, ["[v0]", "[v1]"], [2.0, 2.0])
    chain = j.chains[0]
    assert "xfade=transition=fade:duration=0.500:offset=1.500" in chain


def test_join_chain_mixed_cut_and_xfade():
    cues = [
        EDLVideoCue(start=0, end=2.0, clip_id=1),
        EDLVideoCue(start=2.0, end=4.0, clip_id=2),
        EDLVideoCue(start=4.0, end=6.0, clip_id=3,
                   transition_in="crossfade", transition_duration=0.5),
    ]
    j = build_join_chain(cues, ["[v0]", "[v1]", "[v2]"], [2.0, 2.0, 2.0])
    assert "concat=n=2:v=1:a=0[vj1]" in j.chains[0]
    assert "xfade" in j.chains[1]


# ---- annotations ----

def test_annotation_circle_not_handled_by_drawbox():
    ann = EDLAnnotation(start=1.0, end=3.0, shape="circle", x=540, y=700, radius=180)
    with pytest.raises(ValueError):
        build_annotation(ann)


def test_annotation_chain_skips_circles():
    anns = [
        EDLAnnotation(start=0, end=1, shape="circle", x=540, y=700, radius=180),
        EDLAnnotation(start=0, end=1, shape="box", x=100, y=100, width=50, height=50),
    ]
    chain = build_annotation_chain(anns)
    assert chain.count("drawbox=") == 1


def test_render_circle_png_produces_ring(tmp_path):
    from supaclip.stitch.annotation import render_annotation_pngs

    anns = [EDLAnnotation(start=0.5, end=1.8, shape="circle", x=540, y=700,
                          radius=180, stroke_width=8)]
    [r] = render_annotation_pngs(anns, tmp_path)
    assert r.png_path.exists()
    # png is 2*radius + 2*(stroke+2) = 380px; placed centered on (x, y)
    assert r.x == 540 - 190
    assert r.y == 700 - 190
    assert r.start == 0.5 and r.end == 1.8


def test_render_annotation_pngs_ignores_non_circles(tmp_path):
    from supaclip.stitch.annotation import render_annotation_pngs

    anns = [EDLAnnotation(start=0, end=1, shape="box", x=100, y=100,
                          width=50, height=50)]
    assert render_annotation_pngs(anns, tmp_path) == []


def test_annotation_box_centered_origin():
    ann = EDLAnnotation(start=0, end=1, shape="box", x=540, y=960,
                        width=200, height=100, color="#00ff00")
    out = build_annotation(ann)
    assert "x=440" in out and "y=910" in out
    assert "w=200:h=100" in out


def test_annotation_arrow_is_filled_horizontal_bar():
    ann = EDLAnnotation(start=0, end=1, shape="arrow", x=300, y=500,
                        width=400, stroke_width=12)
    out = build_annotation(ann)
    assert "x=300" in out
    assert "w=400:h=12" in out
    assert "t=fill" in out


def test_annotation_chain_joins_with_comma():
    anns = [
        EDLAnnotation(start=0, end=1, shape="box", x=100, y=100, width=50, height=50),
        EDLAnnotation(start=0, end=1, shape="box", x=200, y=200, width=50, height=50),
    ]
    chain = build_annotation_chain(anns)
    assert chain.count("drawbox=") == 2
    assert ",drawbox=" in chain


def test_annotation_chain_empty():
    assert build_annotation_chain([]) == ""


# ---- music ----

def test_resolve_music_file_path(tmp_path):
    f = tmp_path / "song.mp3"
    f.write_bytes(b"x")
    assert resolve_music_file(str(f), None) == str(f.resolve())


def test_resolve_music_file_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        resolve_music_file(str(tmp_path / "nope.mp3"), None)


def test_resolve_music_file_catalog_without_conn():
    with pytest.raises(FileNotFoundError):
        resolve_music_file("catalog:42", None)


def test_build_music_plan_no_duck():
    music = EDLMusic(file="/tmp/m.mp3", level_db=-20.0, duck=False)
    plan = build_music_plan(music, music_input_index=2, duration=10.0,
                             voiceover_sidechain_label=None)
    assert plan.final_label == "[amus_pre_2]"
    assert any("volume=-20.0dB" in c for c in plan.chains)
    assert not any("sidechaincompress" in c for c in plan.chains)


def test_build_music_plan_with_duck():
    music = EDLMusic(file="/tmp/m.mp3", level_db=-22.0, duck=True)
    plan = build_music_plan(music, music_input_index=2, duration=10.0,
                             voiceover_sidechain_label="[avo_sc]")
    assert plan.final_label == "[amus_2]"
    assert any("sidechaincompress" in c for c in plan.chains)
    assert any("[avo_sc]" in c for c in plan.chains)
