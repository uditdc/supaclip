from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

EDL_SCHEMA_VERSION = 1

ReframeMode = Literal["crop_center", "crop_left", "crop_right", "letterbox"]
AudioKind = Literal["voiceover", "clip_audio", "silence"]
OSTStyle = Literal["bold_yellow", "red_strike", "neon_pink", "white_pop", "comment_trap"]
TTSBackendName = Literal["elevenlabs"]
EffectKind = Literal["none", "freeze_first", "ken_burns_in", "ken_burns_out", "slow_mo"]
TransitionKind = Literal["cut", "crossfade"]
AnnotationShape = Literal["circle", "box", "arrow"]


class EDLOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    width: int = 1080
    height: int = 1920
    fps: int = 60
    duration: float


class EDLVoiceover(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: TTSBackendName = "elevenlabs"
    voice_id: str
    settings: dict[str, float] = Field(default_factory=dict)
    script: str


class EDLVideoCue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: float
    end: float
    clip_id: int
    source_in: float | None = None
    reframe: ReframeMode = "crop_center"
    reframe_offset: int = 0
    effect: EffectKind = "none"
    effect_params: dict[str, float] = Field(default_factory=dict)
    transition_in: TransitionKind = "cut"
    transition_duration: float = 0.0


class EDLAnnotation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: float
    end: float
    shape: AnnotationShape
    x: int
    y: int
    radius: int = 0
    width: int = 0
    height: int = 0
    color: str = "#ff3b30"
    stroke_width: int = 8


class EDLMusic(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file: str
    level_db: float = -22.0
    duck: bool = True


class EDLAudioCue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: float
    end: float
    kind: AudioKind
    level_db: float | None = None
    duck: bool = False


class EDLOSTCue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: float
    end: float
    text: str
    style: OSTStyle = "white_pop"


class EDL(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = EDL_SCHEMA_VERSION
    title: str
    output: EDLOutput
    voiceover: EDLVoiceover | None = None
    video: list[EDLVideoCue] = Field(default_factory=list)
    audio: list[EDLAudioCue] = Field(default_factory=list)
    ost: list[EDLOSTCue] = Field(default_factory=list)
    annotations: list[EDLAnnotation] = Field(default_factory=list)
    music: EDLMusic | None = None


def save_edl(edl: EDL, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        json.dump(edl.model_dump(mode="json"), fh, indent=2)


def load_edl(path: str | Path) -> EDL:
    with Path(path).open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return EDL.model_validate(data)


@dataclass(frozen=True)
class ValidationIssue:
    severity: Literal["error", "warning"]
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "path": self.path, "message": self.message}


class ClipResolver(Protocol):
    def __call__(self, clip_id: int) -> Any | None:
        """Return an object with .duration and .source_in floats, or None."""


_EPS = 1e-3


def validate_edl(edl: EDL, resolver: ClipResolver | None = None) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    if edl.schema_version != EDL_SCHEMA_VERSION:
        issues.append(ValidationIssue(
            "error", "schema_version",
            f"unsupported schema_version {edl.schema_version}, expected {EDL_SCHEMA_VERSION}",
        ))

    out = edl.output
    if out.duration <= 0:
        issues.append(ValidationIssue("error", "output.duration", "must be > 0"))
    if out.width <= 0 or out.height <= 0:
        issues.append(ValidationIssue("error", "output", "width/height must be > 0"))
    if out.fps <= 0:
        issues.append(ValidationIssue("error", "output.fps", "must be > 0"))

    for i, cue in enumerate(edl.video):
        _check_range(issues, f"video[{i}]", cue.start, cue.end, out.duration)
    for i, cue in enumerate(edl.audio):
        _check_range(issues, f"audio[{i}]", cue.start, cue.end, out.duration)
    for i, cue in enumerate(edl.ost):
        _check_range(issues, f"ost[{i}]", cue.start, cue.end, out.duration)

    sorted_video = sorted(enumerate(edl.video), key=lambda kv: kv[1].start)
    prev_end = 0.0
    prev_idx: int | None = None
    for orig_idx, cue in sorted_video:
        if cue.start - prev_end > _EPS:
            issues.append(ValidationIssue(
                "error", f"video[{orig_idx}]",
                f"gap in video track: {prev_end:.3f}s..{cue.start:.3f}s uncovered",
            ))
        elif prev_end - cue.start > _EPS:
            issues.append(ValidationIssue(
                "error", f"video[{orig_idx}]",
                f"overlaps previous video cue (prev ends at {prev_end:.3f}s, "
                f"this starts at {cue.start:.3f}s)",
            ))
        prev_end = max(prev_end, cue.end)
        prev_idx = orig_idx
    if edl.video and abs(prev_end - out.duration) > _EPS:
        issues.append(ValidationIssue(
            "error", f"video[{prev_idx}]",
            f"video track ends at {prev_end:.3f}s but output.duration is {out.duration:.3f}s",
        ))

    sorted_pairs = sorted(enumerate(edl.video), key=lambda kv: kv[1].start)
    for n, (orig_idx, cue) in enumerate(sorted_pairs):
        cue_dur = cue.end - cue.start
        if cue.transition_in == "crossfade":
            if cue.transition_duration <= 0:
                issues.append(ValidationIssue(
                    "error", f"video[{orig_idx}].transition_duration",
                    "must be > 0 when transition_in='crossfade'",
                ))
            elif n == 0:
                issues.append(ValidationIssue(
                    "warning", f"video[{orig_idx}].transition_in",
                    "first cue has crossfade; will fade in from black",
                ))
            else:
                prev_idx, prev_cue = sorted_pairs[n - 1]
                prev_dur = prev_cue.end - prev_cue.start
                max_xfade = min(cue_dur, prev_dur) / 2
                if cue.transition_duration - max_xfade > _EPS:
                    issues.append(ValidationIssue(
                        "error", f"video[{orig_idx}].transition_duration",
                        f"{cue.transition_duration:.3f}s exceeds half the shorter "
                        f"neighbor cue ({max_xfade:.3f}s)",
                    ))

        if cue.effect == "slow_mo":
            speed = cue.effect_params.get("speed", 0.5)
            if not (0.05 <= speed <= 1.0):
                issues.append(ValidationIssue(
                    "error", f"video[{orig_idx}].effect_params.speed",
                    f"slow_mo speed must be in [0.05, 1.0]; got {speed}",
                ))
        elif cue.effect in ("ken_burns_in", "ken_burns_out"):
            zf = cue.effect_params.get("zoom_from", 1.0)
            zt = cue.effect_params.get("zoom_to", 1.15)
            if zf <= 0 or zt <= 0:
                issues.append(ValidationIssue(
                    "error", f"video[{orig_idx}].effect_params",
                    "zoom_from/zoom_to must be > 0",
                ))

    for i, ann in enumerate(edl.annotations):
        _check_range(issues, f"annotations[{i}]", ann.start, ann.end, out.duration)
        if not (0 <= ann.x <= out.width):
            issues.append(ValidationIssue(
                "error", f"annotations[{i}].x",
                f"{ann.x} outside [0, {out.width}]",
            ))
        if not (0 <= ann.y <= out.height):
            issues.append(ValidationIssue(
                "error", f"annotations[{i}].y",
                f"{ann.y} outside [0, {out.height}]",
            ))
        if ann.shape == "circle" and ann.radius <= 0:
            issues.append(ValidationIssue(
                "error", f"annotations[{i}].radius",
                "circle annotation requires radius > 0",
            ))
        if ann.shape == "box" and (ann.width <= 0 or ann.height <= 0):
            issues.append(ValidationIssue(
                "error", f"annotations[{i}]",
                "box annotation requires width > 0 and height > 0",
            ))
        if ann.shape == "arrow" and ann.width <= 0:
            issues.append(ValidationIssue(
                "error", f"annotations[{i}].width",
                "arrow annotation requires width > 0 (length in pixels)",
            ))

    has_voiceover_cue = any(c.kind == "voiceover" for c in edl.audio)
    if has_voiceover_cue and edl.voiceover is None:
        issues.append(ValidationIssue(
            "error", "voiceover",
            "audio track references voiceover but edl.voiceover is unset",
        ))
    if edl.voiceover is not None and not has_voiceover_cue:
        issues.append(ValidationIssue(
            "warning", "voiceover",
            "voiceover defined but no audio cue uses it",
        ))

    if edl.music is not None and edl.music.duck and edl.voiceover is None:
        issues.append(ValidationIssue(
            "warning", "music.duck",
            "duck=true requires a voiceover to duck under; will be ignored",
        ))

    if resolver is not None:
        for i, cue in enumerate(edl.video):
            clip = resolver(cue.clip_id)
            if clip is None:
                issues.append(ValidationIssue(
                    "error", f"video[{i}].clip_id",
                    f"clip_id={cue.clip_id} not found in catalog",
                ))
                continue
            cue_dur = cue.end - cue.start
            source_in = cue.source_in if cue.source_in is not None else float(clip.source_in)
            available = float(clip.duration) - (source_in - float(clip.source_in))
            if cue_dur - available > _EPS:
                issues.append(ValidationIssue(
                    "error", f"video[{i}]",
                    f"cue duration {cue_dur:.3f}s exceeds available clip footage "
                    f"({available:.3f}s from source_in={source_in:.3f})",
                ))

    return issues


def _check_range(
    issues: list[ValidationIssue], path: str, start: float, end: float, total: float
) -> None:
    if start < -_EPS:
        issues.append(ValidationIssue("error", path, f"start {start:.3f}s < 0"))
    if end - total > _EPS:
        issues.append(ValidationIssue(
            "error", path, f"end {end:.3f}s exceeds output.duration {total:.3f}s",
        ))
    if end - start <= _EPS:
        issues.append(ValidationIssue("error", path, f"end <= start ({start:.3f}, {end:.3f})"))
