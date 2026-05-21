from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

from ..analyze import SegmentAnalysis, SegmentEvent
from ..profiles import GameProfile
from ._shared import (
    _coerce,
    _parse_json,
    _prune_overlaps,
    _signals_block,
    _taxonomy_str,
)


CHUNK_SECONDS = 480.0
TARGET_WIDTH = 720
TARGET_VIDEO_BITRATE = "800k"
TARGET_FPS = 24
INLINE_MAX_BYTES = 15 * 1024 * 1024
FILES_API_POLL_SECONDS = 1.5
FILES_API_POLL_TIMEOUT = 180.0


class GemmaVideoBackend:
    name = "gemma-video"

    def __init__(self, model: str, base_url: str, api_key: str | None) -> None:
        self.model = _normalize_model(model)
        env_key = _discover_api_key()
        forwarded = api_key if _looks_like_ai_studio_key(api_key) else None
        self.api_key = env_key or forwarded
        if not self.api_key:
            raise ValueError(
                "gemma-video requires a Google AI Studio key. Set GEMINI_API_KEY "
                "(or GOOGLE_API_KEY), or pass --api-key with an AIza... key."
            )

    def _client(self):
        from google import genai
        return genai.Client(api_key=self.api_key)

    def analyze_segment(
        self,
        video_path: str,
        start: float,
        end: float,
        profile: GameProfile,
    ) -> SegmentAnalysis:
        total_duration = max(0.0, end - start)
        if total_duration <= 0:
            return SegmentAnalysis(events=[])

        client = self._client()
        chunks = _split_chunks(start, end, CHUNK_SECONDS)
        merged: list[SegmentEvent] = []

        for chunk_start, chunk_end in chunks:
            chunk_duration = chunk_end - chunk_start
            mp4_bytes = _encode_chunk(
                video_path, chunk_start, chunk_end,
                TARGET_WIDTH, TARGET_VIDEO_BITRATE, TARGET_FPS,
            )
            raw = self._call(client, mp4_bytes, profile, chunk_duration)
            parsed = _parse_json(raw)
            if parsed is None:
                raw = self._call(client, mp4_bytes, profile, chunk_duration)
                parsed = _parse_json(raw)
            if parsed is None:
                merged.append(SegmentEvent(
                    start=chunk_start - start,
                    end=chunk_end - start,
                    description="(analyzer failed to return valid JSON)",
                ))
                continue

            events = _coerce(parsed, profile, chunk_duration).events
            offset = chunk_start - start
            for ev in events:
                ev.start = round(ev.start + offset, 3)
                ev.end = round(ev.end + offset, 3)
                merged.append(ev)

        merged.sort(key=lambda e: e.start)
        merged = _prune_overlaps(merged)

        if not merged:
            merged = [SegmentEvent(
                start=0.0, end=total_duration,
                description="(analyzer returned no events)",
            )]
        return SegmentAnalysis(events=merged)

    def _call(self, client, mp4_bytes: bytes, profile: GameProfile, duration: float) -> str:
        from google.genai import types

        prompt = _build_prompt(profile, duration)
        video_part = _video_part(client, mp4_bytes)
        try:
            resp = client.models.generate_content(
                model=self.model,
                contents=[video_part, prompt],
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    response_mime_type="application/json",
                ),
            )
        finally:
            _release_video_part(client, video_part)
        return getattr(resp, "text", "") or ""


def _split_chunks(start: float, end: float, max_len: float) -> list[tuple[float, float]]:
    if end <= start or max_len <= 0:
        return []
    chunks: list[tuple[float, float]] = []
    t = start
    while t < end:
        chunk_end = min(end, t + max_len)
        chunks.append((t, chunk_end))
        t = chunk_end
    return chunks


def _encode_chunk(
    video_path: str,
    t0: float,
    t1: float,
    width: int,
    vbitrate: str,
    fps: int,
) -> bytes:
    duration = max(0.0, t1 - t0)
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        out_path = tmp.name
    try:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
            "-ss", f"{t0:.3f}", "-i", str(video_path),
            "-t", f"{duration:.3f}",
            "-vf", f"scale={width}:-2",
            "-r", str(fps),
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-b:v", vbitrate,
            "-an",
            "-movflags", "+faststart",
            out_path,
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        return Path(out_path).read_bytes()
    finally:
        try:
            Path(out_path).unlink()
        except OSError:
            pass


def _video_part(client, mp4_bytes: bytes):
    from google.genai import types

    if len(mp4_bytes) <= INLINE_MAX_BYTES:
        return types.Part.from_bytes(data=mp4_bytes, mime_type="video/mp4")

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp.write(mp4_bytes)
        upload_path = tmp.name
    try:
        uploaded = client.files.upload(
            file=upload_path,
            config={"mime_type": "video/mp4"},
        )
    finally:
        try:
            Path(upload_path).unlink()
        except OSError:
            pass

    deadline = time.monotonic() + FILES_API_POLL_TIMEOUT
    while _file_state(uploaded) == "PROCESSING":
        if time.monotonic() > deadline:
            raise RuntimeError(f"Gemini Files API processing timed out for {uploaded.name!r}")
        time.sleep(FILES_API_POLL_SECONDS)
        uploaded = client.files.get(name=uploaded.name)

    if _file_state(uploaded) != "ACTIVE":
        raise RuntimeError(
            f"Gemini Files API returned state={_file_state(uploaded)} for {uploaded.name!r}"
        )
    return uploaded


def _file_state(uploaded) -> str:
    state = getattr(uploaded, "state", None)
    if state is None:
        return "UNKNOWN"
    return getattr(state, "name", str(state))


def _release_video_part(client, part) -> None:
    name = getattr(part, "name", None)
    if not name:
        return
    try:
        client.files.delete(name=name)
    except Exception:
        return


def _discover_api_key() -> str | None:
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_AI_STUDIO_API_KEY"):
        v = os.environ.get(var)
        if v:
            return v
    return None


def _looks_like_ai_studio_key(key: str | None) -> bool:
    return bool(key) and not key.startswith("sk-") and not key.startswith("Bearer ")


def _normalize_model(model: str) -> str:
    """Strip OpenRouter-style prefixes/suffixes so the same model id works for both backends."""
    m = model.strip()
    if m.startswith("google/"):
        m = m[len("google/"):]
    if m.endswith(":free"):
        m = m[: -len(":free")]
    return m or "gemma-4-31b-it"


def _build_prompt(profile: GameProfile, duration: float) -> str:
    sig_lines = _signals_block(profile, indent="      ")
    tax = _taxonomy_str(profile)
    return (
        f"You are watching a {duration:.1f}-second clip of {profile.name} gameplay.\n"
        f"{profile.prompt_hints}\n\n"
        f"Allowed category tags (subset only): {tax}\n\n"
        f"TASK: identify every distinct situation. Each event is a contiguous\n"
        f"[start, end] time window in seconds from the BEGINNING of this clip\n"
        f"(0.0 to {duration:.1f}). Windows MUST NOT overlap. A new situation\n"
        f"begins when ANY of these change: on-foot vs in-vehicle state, vehicle\n"
        f"being driven, wanted level, activity (walking, driving, shooting,\n"
        f"crashing), on-screen mission/event text, or location/environment.\n\n"
        f"Aim for:\n"
        f"- ≤ 20s clip with one continuous action: 1 event is fine.\n"
        f"- > 30s with state changes: at least 3 events.\n"
        f"- > 60s with state changes: at least 5 events.\n"
        f"- > 120s: typically 6–10 events.\n"
        f"Skip dull stretches (idling, menus, loading) by leaving gaps between\n"
        f"events; do NOT pad to cover the whole clip.\n\n"
        f"REQUIRED JSON SHAPE (return JSON only, no prose, no code fences):\n"
        f"{{\n"
        f'  "events": [\n'
        f"    {{\n"
        f'      "start": 0.0,\n'
        f'      "end": 0.0,\n'
        f'      "description": "1-3 sentences grounded in visible footage; no inventions",\n'
        f'      "categories": ["tag", ...],\n'
        f'      "base_interest": 0,\n'
        f'      "game_signals": {{\n{sig_lines}\n      }},\n'
        f'      "audio_cues": ["e.g. sirens", "gunfire", "engine"]\n'
        f"    }}\n"
        f"  ]\n"
        f"}}"
    )
