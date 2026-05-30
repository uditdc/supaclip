from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from supaclip.core.cache import Cache, fingerprint_file
from supaclip.core.manifest import (
    AudioInfo,
    Clip,
    ExtractInfo,
    Manifest,
    SourceInfo,
    load_manifest,
    now_iso,
    save_manifest,
)
from supaclip.extract.analyze import blend_score
from supaclip.extract.audio import audio_factor_for_range, detect_peaks, peak_loudness_db
from supaclip.extract.dedupe import iou, merge_overlapping
from supaclip.extract.profiles import GTA_PROFILE, load_profile
from supaclip.extract.segment import (
    clamp_ranges,
    format_timestamp,
    interval_segments,
    manual_segments,
    parse_timestamp,
)


# ---------------------- dedupe / iou ----------------------

def test_iou_basic():
    assert iou((0, 10), (5, 15)) == pytest.approx(5 / 15)
    assert iou((0, 10), (10, 20)) == 0.0
    assert iou((0, 10), (0, 10)) == 1.0
    assert iou((0, 10), (20, 30)) == 0.0


def test_merge_overlapping_iterative():
    ranges = [(0.0, 10.0), (1.0, 11.0), (50.0, 60.0)]
    merged = merge_overlapping(ranges, threshold=0.5)
    assert merged == [(0.0, 11.0), (50.0, 60.0)]


def test_merge_overlapping_disabled_at_high_threshold():
    ranges = [(0.0, 10.0), (5.0, 15.0)]
    merged = merge_overlapping(ranges, threshold=0.99)
    assert merged == [(0.0, 10.0), (5.0, 15.0)]


# ---------------------- timestamp parse / format ----------------------

@pytest.mark.parametrize("s,expected", [
    ("30", 30.0),
    ("1:30", 90.0),
    ("01:00:05", 3605.0),
    ("0:00:01.500", 1.5),
])
def test_parse_timestamp(s, expected):
    assert parse_timestamp(s) == pytest.approx(expected)


def test_format_timestamp_roundtrip():
    assert format_timestamp(90.0) == "1:30.000"
    assert format_timestamp(3605.5).startswith("1:00:05")


# ---------------------- manifest model ----------------------

def _sample_manifest() -> Manifest:
    return Manifest(
        source=SourceInfo(file="/tmp/x.mp4", duration=10.0, resolution="1920x1080", fps=60.0),
        extract=ExtractInfo(segmenter="auto", analyzer="gemma4", game_profile="gta", created_at=now_iso()),
        taxonomy=list(GTA_PROFILE.taxonomy),
        clips=[Clip(
            id="clip_01", file="clips/clip_01.mp4", source_in=0.0, source_out=5.0,
            duration=5.0, resolution="1920x1080", fps=60.0,
            description="d", categories=["stunt"], score=42,
            game_signals={"wanted_level": 0}, audio=AudioInfo(),
            keyframes=[], segment_source="auto",
        )],
    )


def test_manifest_roundtrip(tmp_path: Path):
    m = _sample_manifest()
    path = tmp_path / "manifest.json"
    save_manifest(m, path)
    loaded = load_manifest(path)
    assert loaded.model_dump() == m.model_dump()


def test_manifest_rejects_extras():
    payload = _sample_manifest().model_dump(mode="json")
    payload["bogus"] = True
    with pytest.raises(Exception):
        Manifest.model_validate(payload)


# ---------------------- profiles ----------------------

def test_load_builtin_profile():
    p = load_profile("gta")
    assert p.name == "gta"
    assert "police_chase" in p.taxonomy
    assert "wanted_level" in p.signal_keys()


def test_load_profile_from_file(tmp_path: Path):
    custom = {
        "name": "demo",
        "taxonomy": ["a", "b"],
        "signals": [{"key": "k", "type": "str", "description": "d"}],
        "prompt_hints": "x",
    }
    p = tmp_path / "demo.json"
    p.write_text(json.dumps(custom))
    loaded = load_profile(str(p))
    assert loaded.name == "demo"
    assert loaded.taxonomy == ["a", "b"]


def test_load_profile_unknown():
    with pytest.raises(ValueError):
        load_profile("not_a_profile_anywhere")


# ---------------------- audio peaks ----------------------

def test_detect_peaks_picks_local_maxima():
    samples = [(t * 0.5, db) for t, db in enumerate(
        [-40, -30, -20, -25, -35, -30, -10, -5, -10, -20, -40]
    )]
    peaks = detect_peaks(samples, percentile=0.6, min_gap_seconds=0.5)
    times = sorted(p.time for p in peaks)
    assert any(abs(t - 1.0) < 0.6 for t in times)
    assert any(abs(t - 3.5) < 0.6 for t in times)


def test_detect_peaks_empty():
    assert detect_peaks([]) == []


def test_audio_factor_for_range():
    samples = [(t * 1.0, db) for t, db in enumerate([-60, -50, -40, -10, -50, -60])]
    factor = audio_factor_for_range(samples, 2.0, 4.0)
    assert 90 <= factor <= 100
    assert peak_loudness_db(samples, 2.0, 4.0) == pytest.approx(-10.0)


# ---------------------- scoring ----------------------

def test_blend_score_default_weights():
    assert blend_score(100, 0) == 70
    assert blend_score(0, 100) == 30
    assert blend_score(80, 50) == round(0.7 * 80 + 0.3 * 50)
    assert blend_score(-5, 200) == 30  # clamped


# ---------------------- cache ----------------------

def test_cache_get_set(tmp_path: Path):
    c = Cache(tmp_path)
    assert c.get("ns", ("a", 1)) is None
    c.set("ns", ("a", 1), {"x": 2})
    assert c.get("ns", ("a", 1)) == {"x": 2}


def test_cache_disabled_is_noop(tmp_path: Path):
    c = Cache(tmp_path, enabled=False)
    c.set("ns", ("k",), "v")
    assert c.get("ns", ("k",)) is None


def test_fingerprint_changes_with_mtime(tmp_path: Path):
    f = tmp_path / "x.bin"
    f.write_bytes(b"abc")
    a = fingerprint_file(f)
    f.write_bytes(b"abcd")
    b = fingerprint_file(f)
    assert a != b


# ---------------------- segment helpers ----------------------

def test_clamp_ranges_drops_too_short_and_clips_too_long():
    out = clamp_ranges([(0.0, 5.0), (10.0, 200.0), (300.0, 320.0)],
                       min_clip=15.0, max_clip=60.0, duration=400.0)
    assert (0.0, 5.0) not in out  # too short
    assert (10.0, 70.0) in out    # clipped from 190s to 60s
    assert (300.0, 320.0) in out


def test_interval_segments_overlap():
    ranges = interval_segments(duration=150.0, interval=60.0, overlap=5.0)
    assert ranges[0] == (0.0, 60.0)
    assert ranges[1][0] == 55.0


def test_manual_segments(tmp_path: Path):
    p = tmp_path / "cuts.csv"
    p.write_text("0,5\n1:00,1:10\n# a comment\n\n")
    out = manual_segments(p)
    assert out == [(0.0, 5.0), (60.0, 70.0)]


# ---------------------- ffmpeg smoke ----------------------

def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg not installed")
def test_ffmpeg_smoke_probe_cut_keyframes(tmp_path: Path):
    from supaclip.core.ffmpeg import cut_clip, extract_keyframes, probe

    src = tmp_path / "src.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=1000:duration=4",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(src),
    ], check=True, capture_output=True)

    info = probe(src)
    assert info.width == 320 and info.height == 240
    assert info.has_audio
    assert info.duration == pytest.approx(4.0, abs=0.5)

    out = tmp_path / "out.mp4"
    cut_clip(src, 1.0, 3.0, out)
    assert out.exists() and out.stat().st_size > 0

    kfs = extract_keyframes(src, 0.5, 3.5, 2, str(tmp_path / "kf{i:02d}.jpg"))
    assert len(kfs) == 2
    for k in kfs:
        assert Path(k).exists()


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg not installed")
def test_integration_pipeline_with_mocked_analyzer(tmp_path: Path):
    from supaclip.core.log import Logger
    from supaclip.extract.analyze import SegmentAnalysis, SegmentEvent
    from supaclip.extract.pipeline import ExtractConfig, run

    src = tmp_path / "src.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=40",
        "-f", "lavfi", "-i", "sine=frequency=1000:duration=40",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(src),
    ], check=True, capture_output=True)

    out_dir = tmp_path / "clips"
    cfg = ExtractConfig(
        videos=[str(src)],
        output_dir=str(out_dir),
        segmenter="interval",
        timestamps_file=None,
        interval=20.0,
        game_profile="gta",
        analyzer="frames",
        llm="gemma4",
        base_url="http://localhost:11434/v1",
        api_key=None,
        keyframes=1,
        dedup_iou=0.6,
        no_dedup=True,
        min_clip=5.0,
        max_clip=30.0,
        max_duration=600.0,
        cache_dir=str(tmp_path / "cache"),
        no_cache=True,
        keep_temp=False,
        verbose=False,
    )

    fake = SegmentAnalysis(events=[
        SegmentEvent(
            start=0.0,
            end=20.0,
            description="mocked clip",
            categories=["cruising"],
            base_interest=50,
            game_signals={"wanted_level": 0, "vehicles": ["car"]},
            audio_cues=["engine"],
        ),
    ])
    with patch("supaclip.extract.backends.frames.FramesBackend.analyze_segment", return_value=fake):
        manifests = run(cfg, Logger(verbose=False))

    assert len(manifests) == 1
    m = manifests[0]
    assert len(m.clips) >= 1
    for c in m.clips:
        assert c.description
        assert 0 <= c.score <= 100
        assert set(c.categories).issubset(set(m.taxonomy))
        assert "wanted_level" in c.game_signals
    assert (out_dir / "manifest.json").exists()


# ---------------------- CLI --help is fast (no heavy imports) ----------------------

def test_help_is_fast():
    """--help must not trigger heavy imports."""
    res = subprocess.run(
        ["python", "-m", "supaclip.extract.cli", "--help"],
        capture_output=True, text=True, timeout=10,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert res.returncode == 0
    assert "extract" in res.stdout
