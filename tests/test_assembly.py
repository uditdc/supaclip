from __future__ import annotations

import pytest

from clipper.core.edl import (
    EDL,
    EDLAnnotation,
    EDLAudioCue,
    EDLMusic,
    EDLOSTCue,
    EDLOutput,
    EDLVideoCue,
    EDLVoiceover,
)
from clipper.stitch.assembly import CueInput, RenderInputs, build_command
from clipper.stitch.music import build_music_plan


def _two_cue_edl(duration: float = 4.0, with_vo: bool = True) -> tuple[EDL, list[CueInput]]:
    edl = EDL(
        title="t",
        output=EDLOutput(width=1080, height=1920, fps=60, duration=duration),
        voiceover=EDLVoiceover(voice_id="v", script="hi") if with_vo else None,
        video=[
            EDLVideoCue(start=0.0, end=duration / 2, clip_id=1),
            EDLVideoCue(start=duration / 2, end=duration, clip_id=2),
        ],
        audio=[EDLAudioCue(start=0.0, end=duration, kind="voiceover")] if with_vo else [],
        ost=[EDLOSTCue(start=0.5, end=1.5, text="HOOK", style="bold_yellow")],
    )
    cues = [
        CueInput(file_path="/tmp/a.mp4", cue=edl.video[0],
                 cue_start=0.0, cue_end=duration / 2,
                 source_in=0.0, src_w=1920, src_h=1080, reframe="crop_center"),
        CueInput(file_path="/tmp/b.mp4", cue=edl.video[1],
                 cue_start=duration / 2, cue_end=duration,
                 source_in=2.0, src_w=1920, src_h=1080, reframe="crop_center"),
    ]
    return edl, cues


def test_build_command_two_cues_with_voiceover(tmp_path):
    edl, cues = _two_cue_edl()
    vo_wav = tmp_path / "vo.wav"
    vo_wav.write_bytes(b"x")
    args = build_command(
        RenderInputs(edl=edl, cues=cues, voiceover_wav=str(vo_wav)),
        tmp_path / "out.mp4",
    )

    cmd = " ".join(args)
    assert args.count("-i") == 3
    assert "/tmp/a.mp4" in args
    assert "/tmp/b.mp4" in args
    assert str(vo_wav) in args
    assert "-filter_complex" in args
    fc = args[args.index("-filter_complex") + 1]
    assert "concat=n=2:v=1:a=0[vj1]" in fc
    assert "drawtext=" in fc
    assert "[avo]" in fc
    assert "-map" in args
    assert "[vout]" in args and "[aout]" in args


def test_build_command_single_cue_uses_null_not_concat(tmp_path):
    edl, cues = _two_cue_edl()
    edl.video = [EDLVideoCue(start=0.0, end=4.0, clip_id=1)]
    cues = cues[:1]
    cues[0].cue = edl.video[0]
    cues[0].cue_end = 4.0
    args = build_command(
        RenderInputs(edl=edl, cues=cues),
        tmp_path / "out.mp4",
    )
    fc = args[args.index("-filter_complex") + 1]
    assert "concat=n=" not in fc
    assert "null[vjoined]" in fc


def test_build_command_without_voiceover_emits_silence(tmp_path):
    edl, cues = _two_cue_edl(with_vo=False)
    args = build_command(RenderInputs(edl=edl, cues=cues), tmp_path / "out.mp4")
    fc = args[args.index("-filter_complex") + 1]
    assert "anullsrc" in fc
    assert "[avo]" not in fc


def test_build_command_with_clip_audio_cue(tmp_path):
    edl, cues = _two_cue_edl(with_vo=False)
    edl.audio.append(EDLAudioCue(start=0.0, end=2.0, kind="clip_audio", level_db=-12.0))
    args = build_command(RenderInputs(edl=edl, cues=cues), tmp_path / "out.mp4")
    fc = args[args.index("-filter_complex") + 1]
    assert "[abg0]" in fc
    assert "volume=-12.0dB" in fc


def test_build_command_includes_output_settings(tmp_path):
    edl, cues = _two_cue_edl()
    args = build_command(RenderInputs(edl=edl, cues=cues,
                                       voiceover_wav=str(tmp_path / "v.wav")),
                          tmp_path / "out.mp4")
    assert "libx264" in args
    assert "yuv420p" in args
    assert "aac" in args
    assert "+faststart" in args
    assert "-r" in args and "60" in args


def test_build_command_no_cues_raises(tmp_path):
    edl, _ = _two_cue_edl()
    edl.video = []
    with pytest.raises(ValueError):
        build_command(RenderInputs(edl=edl, cues=[]), tmp_path / "x.mp4")


def test_build_command_crossfade_uses_xfade(tmp_path):
    edl, cues = _two_cue_edl(duration=4.0, with_vo=False)
    edl.video[1] = EDLVideoCue(start=2.0, end=4.0, clip_id=2,
                               transition_in="crossfade", transition_duration=0.5)
    cues[1].cue = edl.video[1]
    args = build_command(RenderInputs(edl=edl, cues=cues), tmp_path / "out.mp4")
    fc = args[args.index("-filter_complex") + 1]
    assert "xfade=transition=fade:duration=0.500" in fc
    assert "concat=n=2" not in fc


def test_build_command_freeze_first_inserts_loop(tmp_path):
    edl, cues = _two_cue_edl(duration=4.0, with_vo=False)
    edl.video[0] = EDLVideoCue(start=0.0, end=2.0, clip_id=1, effect="freeze_first")
    cues[0].cue = edl.video[0]
    args = build_command(RenderInputs(edl=edl, cues=cues), tmp_path / "out.mp4")
    fc = args[args.index("-filter_complex") + 1]
    assert "loop=loop=-1:size=1" in fc


def test_build_command_slow_mo_inserts_setpts(tmp_path):
    edl, cues = _two_cue_edl(duration=4.0, with_vo=False)
    edl.video[0] = EDLVideoCue(start=0.0, end=2.0, clip_id=1,
                               effect="slow_mo", effect_params={"speed": 0.5})
    cues[0].cue = edl.video[0]
    args = build_command(RenderInputs(edl=edl, cues=cues), tmp_path / "out.mp4")
    fc = args[args.index("-filter_complex") + 1]
    assert "setpts=PTS/0.5" in fc
    cue0_t_idx = args.index("-t") + 1
    assert float(args[cue0_t_idx]) == 1.0  # 2.0 cue dur * 0.5 speed


def test_build_command_ken_burns_inserts_zoompan(tmp_path):
    edl, cues = _two_cue_edl(duration=4.0, with_vo=False)
    edl.video[0] = EDLVideoCue(start=0.0, end=2.0, clip_id=1, effect="ken_burns_in")
    cues[0].cue = edl.video[0]
    args = build_command(RenderInputs(edl=edl, cues=cues), tmp_path / "out.mp4")
    fc = args[args.index("-filter_complex") + 1]
    assert "zoompan=z=" in fc


def test_build_command_annotation_inserts_drawbox(tmp_path):
    edl, cues = _two_cue_edl(with_vo=False)
    edl.annotations.append(EDLAnnotation(
        start=0.5, end=2.0, shape="circle", x=540, y=700, radius=180
    ))
    args = build_command(RenderInputs(edl=edl, cues=cues), tmp_path / "out.mp4")
    fc = args[args.index("-filter_complex") + 1]
    assert "drawbox=" in fc
    assert "[vann]" in fc


def test_build_command_music_added_as_input(tmp_path):
    edl, cues = _two_cue_edl(with_vo=True)
    edl.music = EDLMusic(file="/tmp/m.mp3", level_db=-20.0, duck=True)
    music_plan = build_music_plan(edl.music, music_input_index=3,
                                   duration=edl.output.duration,
                                   voiceover_sidechain_label="[avo_sc]")
    args = build_command(RenderInputs(edl=edl, cues=cues,
                                       voiceover_wav=str(tmp_path / "v.wav"),
                                       music_path="/tmp/m.mp3",
                                       music_plan=music_plan),
                          tmp_path / "out.mp4")
    assert args.count("-i") == 4
    assert "/tmp/m.mp3" in args
    fc = args[args.index("-filter_complex") + 1]
    assert "sidechaincompress" in fc
    assert "asplit=2[avo][avo_sc]" in fc
