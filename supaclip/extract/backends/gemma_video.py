from __future__ import annotations

import os
import subprocess
import sys
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
MAX_API_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (2.0, 5.0, 15.0)


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

            raw, api_err = self._call_with_retries(
                client, mp4_bytes, profile, chunk_duration, chunk_start, chunk_end,
            )
            if api_err is not None:
                _log_chunk_error(chunk_start, chunk_end, self.model, api_err)
                merged.append(SegmentEvent(
                    start=chunk_start - start,
                    end=chunk_end - start,
                    description=f"(analyzer API error: {api_err})",
                ))
                continue

            parsed = _parse_json(raw)
            if parsed is None:
                raw2, api_err2 = self._call_with_retries(
                    client, mp4_bytes, profile, chunk_duration, chunk_start, chunk_end,
                )
                if api_err2 is None:
                    parsed = _parse_json(raw2)
            if parsed is None:
                _log_chunk_error(
                    chunk_start, chunk_end, self.model,
                    "analyzer returned non-JSON output twice in a row",
                )
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

    def _call_with_retries(
        self,
        client,
        mp4_bytes: bytes,
        profile: GameProfile,
        duration: float,
        chunk_start: float,
        chunk_end: float,
    ) -> tuple[str, str | None]:
        """Call the model with retry on transient failures.

        Returns (raw_text, error_str). On success error_str is None. On a
        permanent failure (retries exhausted, or non-retryable error) returns
        ("", error_str). Logs every retry attempt to stderr so the user can see
        what's happening without --verbose.
        """
        last_err: str | None = None
        for attempt in range(1, MAX_API_ATTEMPTS + 1):
            try:
                return self._call(client, mp4_bytes, profile, duration), None
            except Exception as e:  # noqa: BLE001
                desc = _describe_error(e)
                retryable = _is_retryable(e)
                last_err = desc
                if not retryable or attempt == MAX_API_ATTEMPTS:
                    return "", desc
                backoff = RETRY_BACKOFF_SECONDS[
                    min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)
                ]
                sys.stderr.write(
                    f"  ! gemma-video {chunk_start:.1f}-{chunk_end:.1f}s attempt {attempt}"
                    f"/{MAX_API_ATTEMPTS} failed ({desc}); retrying in {backoff:.0f}s\n"
                )
                time.sleep(backoff)
        return "", last_err or "unknown error"


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


def _describe_error(exc: Exception) -> str:
    """Compact one-line summary of a google-genai exception for logs."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    status = getattr(exc, "status", None)
    message = getattr(exc, "message", None) or str(exc)
    if code or status:
        parts = [type(exc).__name__]
        if code:
            parts.append(f"{code}")
        if status:
            parts.append(str(status))
        return f"{' '.join(parts)}: {message[:200]}"
    return f"{type(exc).__name__}: {message[:200]}"


def _is_retryable(exc: Exception) -> bool:
    """5xx and 429 are transient. 4xx (other than 429) are not."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    try:
        code_int = int(code) if code is not None else None
    except (TypeError, ValueError):
        code_int = None

    if code_int is not None:
        if code_int >= 500:
            return True
        if code_int == 429:
            return True
        return False

    name = type(exc).__name__
    return name in {"ServerError", "ServiceUnavailable", "DeadlineExceeded", "TimeoutError"}


def _log_chunk_error(start: float, end: float, model: str, err: str) -> None:
    sys.stderr.write(
        f"  ✗ gemma-video chunk {start:.1f}-{end:.1f}s ({model}) failed: {err}\n"
    )


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
        f"BOUNDARY PRECISION (this is the most important part — get the times right):\n"
        f"- An event's `start` MUST equal the timestamp of the first moment the\n"
        f"  new situation is visible. NOT a second earlier, NOT a second later.\n"
        f"- An event's `end` MUST equal the timestamp of the last moment the\n"
        f"  situation is still visible. If the next moment already shows the new\n"
        f"  situation, set `end` to THIS moment's timestamp.\n"
        f"- The first event's `start` is 0.0 unless you are intentionally skipping\n"
        f"  a dull opening. The last event's `end` is {duration:.1f} unless you are\n"
        f"  intentionally leaving a trailing dull gap.\n"
        f"- Consecutive events should be tight: `events[i].end == events[i+1].start`\n"
        f"  when the transition is sharp. Only leave a gap if you are deliberately\n"
        f"  skipping dull footage.\n\n"
        f"MINIMUM EVENT DURATION = 10 seconds. Every event you emit MUST span\n"
        f"at least 10 seconds. Shorter beats are NOT separate events — either:\n"
        f"- EXTEND the adjacent event to absorb the short beat, or\n"
        f"- DROP the short beat if it's a transition or dull moment.\n"
        f"It is better to under-segment (fewer, longer events) than to emit a\n"
        f"3-second \"player turns left\" sliver.\n\n"
        f"Aim for:\n"
        f"- ≤ 20s clip with one continuous action: 1 event is fine.\n"
        f"- 20–40s with state changes: 1–2 events.\n"
        f"- 40–90s with state changes: 2–4 events.\n"
        f"- > 90s: 3–6 events, each ≥ 10s.\n"
        f"Skip dull stretches (idling, menus, loading) by leaving gaps between\n"
        f"events; do NOT pad to cover the whole clip.\n\n"
        f"DO NOT INVENT AUDIO. The video stream is muted — you have NO sound\n"
        f"input. Do not guess sirens, engines, gunfire, music, or dialogue. The\n"
        f"schema below does not include an audio field.\n\n"
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
