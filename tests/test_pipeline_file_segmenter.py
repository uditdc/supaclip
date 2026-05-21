from __future__ import annotations

from pathlib import Path

import pytest

from supaclip.core.ffmpeg import VideoInfo
from supaclip.core.log import Logger
from supaclip.extract import pipeline as pipeline_mod
from supaclip.extract.analyze import SegmentAnalysis, SegmentEvent
from supaclip.extract.pipeline import ExtractConfig, _run_one
from supaclip.extract.profiles import GTA_PROFILE


class StubBackend:
    name = "stub"

    def __init__(self, events: list[SegmentEvent]):
        self._events = events
        self.calls: list[tuple[float, float]] = []

    def analyze_segment(self, video_path, start, end, profile):
        self.calls.append((start, end))
        return SegmentAnalysis(events=list(self._events))


@pytest.fixture
def patched_ffmpeg(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline_mod, "ensure_ffmpeg", lambda: None)

    def fake_probe(path):
        return VideoInfo(
            path=str(path), width=1920, height=1080,
            duration=300.0, fps=60.0, has_audio=False,
        )

    monkeypatch.setattr(pipeline_mod, "probe", fake_probe)
    monkeypatch.setattr(pipeline_mod, "extract_loudness_curve", lambda *a, **kw: [])
    monkeypatch.setattr(pipeline_mod, "extract_keyframes", lambda *a, **kw: [])

    def fake_cut(video_path, start, end, out_path):
        Path(out_path).write_bytes(b"")

    monkeypatch.setattr(pipeline_mod, "cut_clip", fake_cut)
    return tmp_path


def _make_cfg(video_path: str, out_dir: Path, segmenter: str) -> ExtractConfig:
    return ExtractConfig(
        videos=[video_path],
        output_dir=str(out_dir),
        segmenter=segmenter,
        timestamps_file=None,
        interval=60.0,
        game_profile="gta",
        analyzer="stub",
        llm="stub-model",
        base_url="http://stub",
        api_key=None,
        keyframes=0,
        dedup_iou=0.6,
        no_dedup=False,
        min_clip=15.0,
        max_clip=60.0,
        max_duration=5400.0,
        cache_dir=str(out_dir / "cache"),
        no_cache=True,
        keep_temp=False,
        verbose=False,
    )


def test_file_segmenter_fans_events_into_clips_sharing_input_file(patched_ffmpeg, tmp_path):
    video = tmp_path / "input.mp4"
    video.write_bytes(b"")
    out_dir = tmp_path / "out"

    events = [
        SegmentEvent(start=0.0, end=12.0, description="chase",
                     categories=["police_chase"], base_interest=80),
        SegmentEvent(start=15.0, end=42.0, description="crash",
                     categories=["crash"], base_interest=70),
        SegmentEvent(start=60.0, end=120.0, description="cruising",
                     categories=["cruising"], base_interest=20),
    ]
    backend = StubBackend(events)
    cfg = _make_cfg(str(video), out_dir, segmenter="file")

    manifest = _run_one(str(video), cfg, GTA_PROFILE, backend, _Cache(), Logger(verbose=False))

    assert len(manifest.clips) == 3
    files = {c.file for c in manifest.clips}
    assert files == {str(video)}, "all events must share the original input file"

    descs = [c.description for c in manifest.clips]
    assert descs == ["chase", "crash", "cruising"]

    bounds = [(c.source_in, c.source_out) for c in manifest.clips]
    assert bounds == [(0.0, 12.0), (15.0, 42.0), (60.0, 120.0)]

    ids = [c.id for c in manifest.clips]
    assert ids == ["clip_01", "clip_02", "clip_03"]

    assert (out_dir / "manifest.json").exists()
    assert not list(out_dir.glob("seg_*.mp4")), "file segmenter must skip sub-mp4 cuts"


def test_non_file_segmenter_cuts_one_subfile_per_segment_and_events_share_it(patched_ffmpeg, tmp_path):
    video = tmp_path / "input.mp4"
    video.write_bytes(b"")
    out_dir = tmp_path / "out"

    events = [
        SegmentEvent(start=2.0, end=10.0, description="a", base_interest=60),
        SegmentEvent(start=20.0, end=40.0, description="b", base_interest=70),
    ]
    backend = StubBackend(events)
    cfg = _make_cfg(str(video), out_dir, segmenter="interval")

    manifest = _run_one(str(video), cfg, GTA_PROFILE, backend, _Cache(), Logger(verbose=False))

    assert len(manifest.clips) >= 2
    first_two = manifest.clips[:2]
    assert first_two[0].file == first_two[1].file, "events from same segment share the cut sub-mp4"
    assert "seg_01" in first_two[0].file
    assert first_two[0].source_in == 2.0 and first_two[0].source_out == 10.0
    assert first_two[1].source_in == 20.0 and first_two[1].source_out == 40.0


class _Cache:
    def get(self, *a, **kw): return None
    def set(self, *a, **kw): return None
