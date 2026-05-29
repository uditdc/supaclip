from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from supaclip.core.ffmpeg import VideoInfo
from supaclip.core.log import Logger
from supaclip.extract import pipeline as pipeline_mod
from supaclip.extract.analyze import SegmentAnalysis, SegmentEvent
from supaclip.extract.pipeline import ExtractConfig, _run_one
from supaclip.extract.profiles import GTA_PROFILE


class _ConcurrencyProbeBackend:
    """Records peak concurrency and tags each event with its chunk start.

    Each call sleeps briefly so overlapping calls are forced to coexist, then
    returns one event whose description encodes the chunk start — letting the
    test assert the reassembled order is by source position, not completion."""

    name = "stub"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0
        self.peak = 0

    def analyze_segment(self, video_path, start, end, profile, context=None):
        with self._lock:
            self._active += 1
            self.peak = max(self.peak, self._active)
        try:
            time.sleep(0.05)
            return SegmentAnalysis(events=[
                SegmentEvent(start=0.0, end=20.0, description=f"chunk@{start:.1f}",
                             base_interest=50),
            ])
        finally:
            with self._lock:
                self._active -= 1


@pytest.fixture
def patched_ffmpeg(monkeypatch):
    monkeypatch.setattr(pipeline_mod, "ensure_ffmpeg", lambda: None)
    monkeypatch.setattr(pipeline_mod, "probe", lambda path: VideoInfo(
        path=str(path), width=1920, height=1080,
        duration=300.0, fps=60.0, has_audio=False,
    ))
    monkeypatch.setattr(pipeline_mod, "extract_loudness_curve", lambda *a, **kw: [])
    monkeypatch.setattr(pipeline_mod, "extract_keyframes", lambda *a, **kw: [])
    # keep the aggregator from reordering: leave events as the pipeline assembled them
    monkeypatch.setattr(pipeline_mod, "aggregate_events", lambda events, **kw: events)


def _make_cfg(video_path: str, out_dir: Path) -> ExtractConfig:
    return ExtractConfig(
        videos=[video_path],
        output_dir=str(out_dir),
        segmenter="interval",
        timestamps_file=None,
        interval=60.0,
        game_profile="gta",
        analyzer="stub",
        llm="stub-model",
        base_url="http://stub",
        api_key=None,
        keyframes=0,
        dedup_iou=0.6,
        no_dedup=True,
        min_clip=15.0,
        max_clip=60.0,
        max_duration=5400.0,
        cache_dir=str(out_dir / "cache"),
        no_cache=True,
        keep_temp=False,
        verbose=False,
        no_chunk=True,
        analyze_concurrency=4,
    )


class _NullCache:
    def get(self, *a, **kw): return None
    def set(self, *a, **kw): return None


def test_analysis_runs_concurrently_and_preserves_source_order(patched_ffmpeg, tmp_path):
    video = tmp_path / "input.mp4"
    video.write_bytes(b"")
    backend = _ConcurrencyProbeBackend()
    cfg = _make_cfg(str(video), tmp_path / "out")

    manifest = _run_one(str(video), cfg, GTA_PROFILE, backend, _NullCache(),
                        Logger(verbose=False))

    assert backend.peak > 1, "analysis chunks should run in parallel"

    starts = [c.source_in for c in manifest.clips]
    assert starts == sorted(starts), "clips must stay ordered by source position"
