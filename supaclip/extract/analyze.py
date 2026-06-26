from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .profiles import GameProfile, VideoContext

PROMPT_VERSION = "v7"


@dataclass
class SegmentEvent:
    start: float
    end: float
    description: str
    categories: list[str] = field(default_factory=list)
    base_interest: int = 0
    game_signals: dict[str, Any] = field(default_factory=dict)
    audio_cues: list[str] = field(default_factory=list)


@dataclass
class SegmentAnalysis:
    events: list[SegmentEvent] = field(default_factory=list)


class AnalyzerBackend(Protocol):
    name: str

    def analyze_segment(
        self,
        video_path: str,
        start: float,
        end: float,
        profile: GameProfile,
        context: VideoContext | None = None,
    ) -> SegmentAnalysis: ...


def blend_score(base_interest: int, audio_factor: float) -> int:
    base_interest = max(0, min(100, int(base_interest)))
    audio_factor = max(0.0, min(100.0, float(audio_factor)))
    return int(round(0.7 * base_interest + 0.3 * audio_factor))


def build_backend(
    name: str,
    model: str,
    base_url: str,
    api_key: str | None,
) -> AnalyzerBackend:
    if name == "frames":
        from .backends.frames import FramesBackend
        return FramesBackend(model=model, base_url=base_url, api_key=api_key)
    if name == "video":
        from .backends.video import VideoBackend
        return VideoBackend(model=model, base_url=base_url, api_key=api_key)
    raise ValueError(f"unknown analyzer backend: {name!r}")
