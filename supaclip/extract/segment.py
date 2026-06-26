from __future__ import annotations

import csv
from pathlib import Path

from .chunking import chunk_segment

Range = tuple[float, float]


def parse_timestamp(s: str) -> float:
    """Parse SS, MM:SS, or HH:MM:SS into seconds (float)."""
    s = s.strip()
    if not s:
        raise ValueError("empty timestamp")
    parts = s.split(":")
    if len(parts) == 1:
        return float(parts[0])
    if len(parts) == 2:
        m, sec = parts
        return int(m) * 60 + float(sec)
    if len(parts) == 3:
        h, m, sec = parts
        return int(h) * 3600 + int(m) * 60 + float(sec)
    raise ValueError(f"unrecognized timestamp: {s!r}")


def format_timestamp(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t - h * 3600 - m * 60
    if h:
        return f"{h:d}:{m:02d}:{s:06.3f}"
    return f"{m:d}:{s:06.3f}"


def clamp_ranges(
    ranges: list[Range],
    min_clip: float,
    max_clip: float,
    duration: float,
) -> list[Range]:
    """Drop ranges shorter than min_clip; clip overlong ranges to max_clip; clamp to [0, duration]."""
    out: list[Range] = []
    for start, end in ranges:
        s = max(0.0, min(start, duration))
        e = max(0.0, min(end, duration))
        if e <= s:
            continue
        if e - s > max_clip:
            e = s + max_clip
        if e - s < min_clip:
            continue
        out.append((s, e))
    return out


def manual_segments(timestamps_file: str | Path) -> list[Range]:
    p = Path(timestamps_file)
    if not p.exists():
        raise FileNotFoundError(f"--timestamps file not found: {p}")
    ranges: list[Range] = []
    with p.open("r", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        for row in reader:
            if not row or all(not c.strip() for c in row):
                continue
            if row[0].lstrip().startswith("#"):
                continue
            if len(row) < 2:
                raise ValueError(f"timestamps row needs start,end: {row!r}")
            start = parse_timestamp(row[0])
            end = parse_timestamp(row[1])
            if end <= start:
                raise ValueError(f"end <= start in row {row!r}")
            ranges.append((start, end))
    return ranges


def file_segments(duration: float) -> list[Range]:
    """Single segment spanning the entire input. Use for pre-cut clips."""
    if duration <= 0:
        return []
    return [(0.0, duration)]


def interval_segments(
    duration: float,
    interval: float,
    overlap: float = 5.0,
) -> list[Range]:
    if interval <= 0:
        return []
    ranges: list[Range] = []
    step = max(1.0, interval - overlap)
    t = 0.0
    while t < duration:
        ranges.append((t, min(duration, t + interval)))
        t += step
    return ranges


def scene_segments(video_path: str, min_clip: float, max_clip: float) -> list[Range]:
    """Detect shot boundaries via PySceneDetect, then bucket frames into ranges."""
    from scenedetect import SceneManager, open_video
    from scenedetect.detectors import ContentDetector

    video = open_video(video_path)
    sm = SceneManager()
    sm.add_detector(ContentDetector())
    sm.detect_scenes(video)
    scenes = sm.get_scene_list()
    ranges: list[Range] = []
    for start, end in scenes:
        ranges.append((start.get_seconds(), end.get_seconds()))
    return ranges


def auto_segments_from_peaks(
    duration: float,
    samples: list[tuple[float, float]],
    min_clip: float,
    max_clip: float,
) -> list[Range]:
    """Audio-trough segmentation: split the whole duration at quiet points.

    Matches the chunking logic used by the debug command — boundaries land at
    local minima in the loudness curve so events are unlikely to be cut in their
    middle, with 5s overlap between adjacent windows. Falls back to evenly-spaced
    windows when no audio samples are available.
    """
    if duration <= 0:
        return []
    if not samples:
        return interval_segments(duration, max_clip)
    return chunk_segment(0.0, duration, samples)
