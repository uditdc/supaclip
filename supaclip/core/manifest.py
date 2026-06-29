from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 3


class SourceInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file: str
    duration: float
    resolution: str
    fps: float


class ExtractInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segmenter: str
    analyzer: str
    game_profile: str
    created_at: str


class AudioInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    peak_loudness_db: float | None = None
    cues: list[str] = Field(default_factory=list)


class Clip(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    file: str
    source_in: float
    source_out: float
    duration: float
    resolution: str
    fps: float
    description: str
    dialogue: str = ""
    categories: list[str] = Field(default_factory=list)
    score: int
    game_signals: dict[str, Any] = Field(default_factory=dict)
    audio: AudioInfo = Field(default_factory=AudioInfo)
    keyframes: list[str] = Field(default_factory=list)
    segment_source: str


class CharacterRole(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    role: str = ""


class StoryBeat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    start: float
    end: float
    summary: str = ""


class SourceSummary(BaseModel):
    """Whole-film rollup: the story spine a recap is built from."""
    model_config = ConfigDict(extra="forbid")
    synopsis: str = ""
    themes: list[str] = Field(default_factory=list)
    tone: str = ""
    characters: list[CharacterRole] = Field(default_factory=list)
    beats: list[StoryBeat] = Field(default_factory=list)
    generated_by: str = ""


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = SCHEMA_VERSION
    source: SourceInfo
    extract: ExtractInfo
    taxonomy: list[str] = Field(default_factory=list)
    clips: list[Clip] = Field(default_factory=list)
    summary: SourceSummary | None = None


def save_manifest(manifest: Manifest, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = manifest.model_dump(mode="json")
    with p.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


def load_manifest(path: str | Path) -> Manifest:
    with Path(path).open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return Manifest.model_validate(data)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
