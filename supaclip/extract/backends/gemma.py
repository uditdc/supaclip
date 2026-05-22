from __future__ import annotations

import base64
import mimetypes
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..analyze import SegmentAnalysis, SegmentEvent
from ..profiles import GameProfile, VideoContext
from ._shared import _coerce, _context_block, _parse_json, _signals_block, _taxonomy_str


FRAMES_FLOOR = 6
SECONDS_PER_FRAME = 1.0
TILE_PX = 512


@dataclass
class FrameRecord:
    idx: int
    t_rel: float
    t_abs: float
    main_path: Path
    main_jpeg: bytes | None = None


@dataclass
class PreparedRequest:
    backend: str
    model: str
    base_url: str
    profile_name: str
    segment: tuple[float, float]
    duration: float
    prompt: str
    frames: list[FrameRecord]
    config: dict[str, Any] = field(default_factory=dict)
    ffmpeg_command: list[str] = field(default_factory=list)
    source_path: str = ""
    workdir: Path | None = None
    context: VideoContext | None = None

    def cleanup(self) -> None:
        if self.workdir and self.workdir.exists():
            shutil.rmtree(self.workdir, ignore_errors=True)


class GemmaBackend:
    name = "gemma"

    def __init__(self, model: str, base_url: str, api_key: str | None) -> None:
        self.model = model
        self.base_url = base_url
        self.api_key = api_key or "ollama"

    def _client(self):
        from openai import OpenAI
        return OpenAI(base_url=self.base_url, api_key=self.api_key)

    def prepare(
        self,
        video_path: str,
        start: float,
        end: float,
        profile: GameProfile,
        workdir: Path | None = None,
        context: VideoContext | None = None,
    ) -> PreparedRequest:
        duration = max(0.0, end - start)
        owns_workdir = workdir is None
        workdir = workdir or Path(tempfile.mkdtemp(prefix="supaclip-gemma-"))
        workdir.mkdir(parents=True, exist_ok=True)

        count = _frame_count_for(duration)
        ffmpeg_cmd, frames = _extract_frames(
            video_path, start, duration, workdir, count,
        )
        prompt = _build_prompt(profile, duration, len(frames), context)

        return PreparedRequest(
            backend=self.name,
            model=self.model,
            base_url=self.base_url,
            profile_name=profile.name,
            segment=(start, end),
            duration=duration,
            prompt=prompt,
            frames=frames,
            config={
                "frames_floor": FRAMES_FLOOR,
                "seconds_per_frame": SECONDS_PER_FRAME,
                "tile_px": TILE_PX,
            },
            ffmpeg_command=ffmpeg_cmd,
            source_path=str(video_path),
            workdir=workdir if owns_workdir else None,
            context=context,
        )

    def send(self, prepared: PreparedRequest) -> str:
        if not prepared.frames:
            return ""

        content: list[dict[str, Any]] = [{"type": "text", "text": prepared.prompt}]
        for part in _character_content_parts(prepared.context):
            content.append(part)
        for f in prepared.frames:
            content.append({"type": "text", "text": f"t={f.t_rel:.1f}s"})
            data = f.main_jpeg if f.main_jpeg is not None else f.main_path.read_bytes()
            b64 = base64.b64encode(data).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            })

        client = self._client()
        resp = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "You are a careful video analyst. Reply with JSON only."},
                {"role": "user", "content": content},
            ],
            temperature=0.2,
        )
        return resp.choices[0].message.content or ""

    def analyze_segment(
        self,
        video_path: str,
        start: float,
        end: float,
        profile: GameProfile,
        context: VideoContext | None = None,
    ) -> SegmentAnalysis:
        prepared = self.prepare(video_path, start, end, profile, context=context)
        try:
            raw = self.send(prepared)
            parsed = _parse_json(raw)
            if parsed is None:
                raw = self.send(prepared)
                parsed = _parse_json(raw)
            if parsed is None:
                return SegmentAnalysis(events=[
                    SegmentEvent(
                        start=0.0,
                        end=prepared.duration,
                        description="(analyzer failed to return valid JSON)",
                    )
                ])
            return _coerce(parsed, profile, prepared.duration)
        finally:
            prepared.cleanup()


def _character_content_parts(context: VideoContext | None) -> list[dict[str, Any]]:
    """OpenAI chat content parts: alternating text label + image_url for each character."""
    if context is None or not context.characters:
        return []
    parts: list[dict[str, Any]] = []
    for i, ch in enumerate(context.characters, 1):
        for j, img in enumerate(ch.images, 1):
            path = Path(img)
            try:
                data = path.read_bytes()
            except OSError:
                continue
            mime, _ = mimetypes.guess_type(path.name)
            if not mime or not mime.startswith("image/"):
                mime = "image/jpeg"
            b64 = base64.b64encode(data).decode("ascii")
            label = (
                f"Reference image {i}.{j} of {ch.name}"
                if len(ch.images) > 1 else f"Reference image {i}: {ch.name}"
            )
            parts.append({"type": "text", "text": label})
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })
    return parts


def _frame_count_for(duration: float) -> int:
    if duration <= 0:
        return 0
    target = int(duration // SECONDS_PER_FRAME)
    return max(FRAMES_FLOOR, target)


def _extract_frames(
    video_path: str,
    start: float,
    duration: float,
    workdir: Path,
    count: int,
) -> tuple[list[str], list[FrameRecord]]:
    """One ffmpeg call: produce `count` evenly-spaced 512² padded frames with timecode."""
    if duration <= 0 or count <= 0:
        return [], []

    fps = count / duration
    pattern = workdir / "f_%04d.jpg"
    filter_chain = (
        f"fps={fps},"
        f"scale={TILE_PX}:{TILE_PX}:force_original_aspect_ratio=decrease,"
        f"pad={TILE_PX}:{TILE_PX}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"drawtext=text='%{{pts\\:hms}}':x=12:y=12:fontsize=28:fontcolor=white:"
        f"box=1:boxcolor=black@0.55:boxborderw=6"
    )

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", str(video_path),
        "-t", f"{duration:.3f}",
        "-vf", filter_chain,
        "-q:v", "5",
        str(pattern),
    ]
    subprocess.run(cmd, check=True, capture_output=True)

    produced = sorted(workdir.glob("f_*.jpg"))[:count]
    span_per_frame = duration / count if count else duration
    frames = [
        FrameRecord(
            idx=i,
            t_rel=(i + 0.5) * span_per_frame,
            t_abs=start + (i + 0.5) * span_per_frame,
            main_path=path,
        )
        for i, path in enumerate(produced)
    ]
    return cmd, frames


def _build_prompt(
    profile: GameProfile,
    duration: float,
    frame_count: int,
    context: VideoContext | None = None,
) -> str:
    if profile.prompt_template_frames:
        return profile.prompt_template_frames.format(
            duration=duration,
            frame_count=frame_count,
            subject=profile.subject,
            hints=profile.prompt_hints,
            taxonomy=_taxonomy_str(profile),
            signals=_signals_block(profile, indent="      "),
            boundary_rules=profile.effective_boundary_rules(),
            example=profile.example_json,
            context=_context_block(context),
        )
    sig_lines = _signals_block(profile, indent="      ")
    tax = _taxonomy_str(profile)
    ctx = _context_block(context)
    example = (profile.example_json.strip() + "\n\n") if profile.example_json.strip() else ""
    return (
        f"{ctx}"
        f"Analyze a {duration:.1f}-second {profile.subject} segment sampled as "
        f"{frame_count} frames in temporal order. Each image is preceded by a text "
        f"token of the form `t=12.5s` giving its segment-relative timestamp; the "
        f"same value is also burned into the top-left corner of the image as a "
        f"backup.\n"
        f"{profile.prompt_hints}\n\n"
        f"Allowed category tags (subset only): {tax}\n\n"
        f"TASK: split the segment into a sequence of distinct situations.\n"
        f"{profile.effective_boundary_rules()}\n"
        f"Each situation is a contiguous time window [start, end] in seconds\n"
        f"RELATIVE TO THE SEGMENT START (0.0 = first frame, {duration:.1f} = last\n"
        f"frame). Windows MUST NOT overlap.\n\n"
        f"BOUNDARY PRECISION (this is the most important part — get the times right):\n"
        f"- Use the `t=X.Xs` text token preceding each frame to set start/end. The\n"
        f"  burned-in timecode is a backup; the text token is authoritative.\n"
        f"- An event's `start` MUST equal the timestamp of the FIRST frame in which\n"
        f"  the new situation is visible. NOT one frame earlier, NOT one frame\n"
        f"  later. If the change happens between two frames, use the LATER frame's\n"
        f"  timestamp.\n"
        f"- An event's `end` MUST equal the timestamp of the LAST frame in which\n"
        f"  the situation is still visible. If the next sampled frame already shows\n"
        f"  the new situation, set `end` to THIS frame's timestamp, not the next\n"
        f"  one's.\n"
        f"- The first event's `start` is 0.0. The last event's `end` is {duration:.1f}\n"
        f"  unless you are intentionally leaving a trailing dull gap.\n"
        f"- Consecutive events should be tight: `events[i].end == events[i+1].start`\n"
        f"  when the transition is sharp (no gap). Only leave a gap if you are\n"
        f"  deliberately skipping dull footage.\n\n"
        f"MINIMUM EVENT DURATION = 10 seconds. Every event you emit MUST span\n"
        f"at least 10 seconds. Shorter beats are NOT separate events — either:\n"
        f"- EXTEND the adjacent event to absorb the short beat, or\n"
        f"- DROP the short beat if it's a transition or dull moment.\n"
        f"Better to under-segment (fewer, longer events) than to emit a 3-second\n"
        f"\"player turns left\" sliver.\n\n"
        f"How many events to return:\n"
        f"- Short clip (< 20s) with one continuous action: one event is fine.\n"
        f"- 20–40s with state changes: 1–2 events.\n"
        f"- 40–90s with state changes: 2–4 events, each ≥ 10s.\n"
        f"- > 90s: 3–6 events, each ≥ 10s. Do NOT bundle separate activities\n"
        f"  into one long event just because they happen back-to-back.\n"
        f"- Skip dull stretches (idling, menus, loading) by leaving gaps\n"
        f"  between events; do not pad to cover the whole segment.\n\n"
        f"{example}"
        f"REQUIRED JSON SHAPE (return JSON only, no prose, no code fences):\n"
        f"{{\n"
        f'  "events": [\n'
        f"    {{\n"
        f'      "start": 0.0,\n'
        f'      "end": 0.0,\n'
        f'      "description": "1-3 sentences grounded in visible footage; no inventions",\n'
        f'      "categories": ["tag", ...],\n'
        f'      "base_interest": 0,\n'
        f'      "game_signals": {{\n{sig_lines}\n      }}\n'
        f"    }}\n"
        f"  ]\n"
        f"}}"
    )
