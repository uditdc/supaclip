from __future__ import annotations

from pathlib import Path

from supaclip.extract.subtitles import (
    dialogue_for_range,
    find_sidecar,
    load_for_video,
    parse_subtitles,
)

SRT = """1
00:00:01,000 --> 00:00:04,000
Hello there.

2
00:00:05,500 --> 00:00:08,000
<i>General</i> Kenobi.
You are a bold one.
"""

VTT = """WEBVTT

NOTE this is a comment block

00:00.000 --> 00:02.000 align:start
First line.

00:00:03.000 --> 00:00:06.000
Second line {an8}here.
"""


def test_parse_srt_basic():
    cues = parse_subtitles(SRT)
    assert len(cues) == 2
    assert cues[0].start == 1.0 and cues[0].end == 4.0
    assert cues[0].text == "Hello there."
    # tags stripped, multi-line joined
    assert cues[1].text == "General Kenobi. You are a bold one."


def test_parse_vtt_handles_header_notes_and_short_timestamps():
    cues = parse_subtitles(VTT)
    assert len(cues) == 2
    assert cues[0].start == 0.0 and cues[0].end == 2.0
    assert cues[0].text == "First line."
    assert cues[1].text == "Second line here."


def test_dialogue_for_range_overlap():
    cues = parse_subtitles(SRT)
    # window touching only the second cue
    assert dialogue_for_range(cues, 5.0, 9.0) == "General Kenobi. You are a bold one."
    # window spanning both
    joined = dialogue_for_range(cues, 0.0, 10.0)
    assert joined.startswith("Hello there.") and "General Kenobi" in joined
    # gap with no dialogue
    assert dialogue_for_range(cues, 4.2, 5.2) == ""


def test_find_sidecar_prefers_exact_stem(tmp_path: Path):
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"x")
    assert find_sidecar(video) is None
    (tmp_path / "movie.en.vtt").write_text(VTT, encoding="utf-8")
    (tmp_path / "movie.srt").write_text(SRT, encoding="utf-8")
    # exact <stem>.srt wins over a language-suffixed variant
    assert find_sidecar(video) == tmp_path / "movie.srt"


def test_load_for_video_reads_sidecar(tmp_path: Path):
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"x")
    (tmp_path / "movie.srt").write_text(SRT, encoding="utf-8")
    cues, source = load_for_video(video)
    assert len(cues) == 2
    assert source and source.endswith("movie.srt")


def test_load_for_video_explicit_path(tmp_path: Path):
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"x")
    subs = tmp_path / "elsewhere.vtt"
    subs.write_text(VTT, encoding="utf-8")
    cues, source = load_for_video(video, explicit_path=subs)
    assert len(cues) == 2 and source == str(subs)
