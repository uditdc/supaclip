"""Subtitle ingestion: parse SRT/VTT and align dialogue to scene ranges.

Dialogue is the spoken plot. Capturing it per scene lets the catalog be
searched by what is *said*, not only by what a vision model *saw*, and gives
downstream summarizers (the movie-recap skill) the actual storyline to work
from. Sourced, in cost order, from a sidecar file or an embedded text stream —
no speech-to-text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..core.ffmpeg import extract_subtitle_text

_SIDECAR_EXTS = (".srt", ".vtt")
_TIMECODE = re.compile(
    r"(\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3}\s*-->\s*(\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3}"
)
_TAG = re.compile(r"<[^>]+>|\{[^}]*\}")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class SubtitleCue:
    start: float
    end: float
    text: str


def _to_seconds(stamp: str) -> float:
    parts = stamp.strip().replace(",", ".").split(":")
    parts = [float(p) for p in parts]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    h, m, s = parts[-3], parts[-2], parts[-1]
    return h * 3600 + m * 60 + s


def _clean(text: str) -> str:
    text = _TAG.sub(" ", text)
    text = text.replace("\n", " ")
    return _WS.sub(" ", text).strip()


def parse_subtitles(content: str) -> list[SubtitleCue]:
    """Parse SRT or WebVTT text into time-ordered cues. Tolerant of both formats."""
    cues: list[SubtitleCue] = []
    blocks = re.split(r"\n\s*\n", content.replace("\r\n", "\n").replace("\r", "\n"))
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        timing_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if timing_idx is None:
            continue
        timing = lines[timing_idx]
        if not _TIMECODE.search(timing):
            continue
        left, right = timing.split("-->", 1)
        start = _to_seconds(left)
        end = _to_seconds(right.split()[0] if right.split() else right)
        text = _clean(" ".join(lines[timing_idx + 1:]))
        if text and end > start:
            cues.append(SubtitleCue(start, end, text))
    cues.sort(key=lambda c: c.start)
    return cues


def find_sidecar(video_path: str | Path) -> Path | None:
    """Find a subtitle file next to the video: `<stem>.srt`, `<stem>.en.vtt`, etc."""
    video = Path(video_path)
    parent = video.parent
    stem = video.stem
    exact = [parent / f"{stem}{ext}" for ext in _SIDECAR_EXTS]
    for cand in exact:
        if cand.is_file():
            return cand
    for ext in _SIDECAR_EXTS:
        matches = sorted(parent.glob(f"{stem}.*{ext}"))
        if matches:
            return matches[0]
    return None


def load_for_video(
    video_path: str | Path, explicit_path: str | Path | None = None
) -> tuple[list[SubtitleCue], str | None]:
    """Resolve subtitles for a video, returning (cues, source_label).

    Order: explicit path → sidecar file → embedded text stream. Returns an
    empty list and None when nothing is found (callers degrade to vision-only).
    """
    if explicit_path is not None:
        p = Path(explicit_path)
        if not p.is_file():
            raise FileNotFoundError(f"subtitle file not found: {p}")
        return parse_subtitles(p.read_text(encoding="utf-8", errors="replace")), str(p)

    sidecar = find_sidecar(video_path)
    if sidecar is not None:
        cues = parse_subtitles(sidecar.read_text(encoding="utf-8", errors="replace"))
        if cues:
            return cues, str(sidecar)

    embedded = extract_subtitle_text(video_path)
    if embedded:
        cues = parse_subtitles(embedded)
        if cues:
            return cues, "embedded:0:s:0"

    return [], None


def dialogue_for_range(cues: list[SubtitleCue], start: float, end: float) -> str:
    """Concatenate the dialogue of every cue overlapping [start, end)."""
    spoken = [c.text for c in cues if c.start < end and c.end > start]
    return _WS.sub(" ", " ".join(spoken)).strip()
