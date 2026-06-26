from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Voice:
    voice_id: str
    name: str
    description: str | None = None


@dataclass(frozen=True)
class Alignment:
    """Per-character timing for a synthesized utterance.

    `characters[i]` is spoken between `start_times[i]` and `end_times[i]`
    (seconds from audio start). Spaces and punctuation are included.
    """
    characters: list[str]
    start_times: list[float]
    end_times: list[float]

    def to_dict(self) -> dict[str, list]:
        return {
            "characters": list(self.characters),
            "start_times": list(self.start_times),
            "end_times": list(self.end_times),
        }

    @classmethod
    def from_dict(cls, data: dict) -> Alignment:
        return cls(
            characters=list(data["characters"]),
            start_times=[float(t) for t in data["start_times"]],
            end_times=[float(t) for t in data["end_times"]],
        )


class TTSBackend(Protocol):
    name: str

    def synthesize(
        self,
        text: str,
        voice_id: str,
        settings: dict[str, float],
        out_path: str | Path,
    ) -> Path:
        """Synthesize `text` to a wav file at `out_path`. Returns the written path."""
        ...

    def synthesize_with_alignment(
        self,
        text: str,
        voice_id: str,
        settings: dict[str, float],
        out_path: str | Path,
    ) -> tuple[Path, Alignment]:
        """Synthesize and also return per-character timing data."""
        ...

    def list_voices(self) -> list[Voice]:
        ...


def normalize_settings(settings: dict[str, float]) -> dict[str, float]:
    """ElevenLabs voice_settings are 0..1 floats. Accept 0..100 user-friendly
    values (e.g. 'Stability 40') and normalize."""
    out: dict[str, float] = {}
    for k, v in settings.items():
        f = float(v)
        if f > 1.0:
            f = f / 100.0
        out[k] = max(0.0, min(1.0, f))
    return out
