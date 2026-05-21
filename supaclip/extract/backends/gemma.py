from __future__ import annotations

import base64
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..analyze import SegmentAnalysis, SegmentEvent
from ..profiles import GameProfile
from ._shared import _coerce, _parse_json, _signals_block, _taxonomy_str


FRAMES_FLOOR = 6
FRAMES_CEILING = 24
SECONDS_PER_FRAME = 5.0


class GemmaBackend:
    name = "gemma"

    def __init__(self, model: str, base_url: str, api_key: str | None) -> None:
        self.model = model
        self.base_url = base_url
        self.api_key = api_key or "ollama"

    def _client(self):
        from openai import OpenAI
        return OpenAI(base_url=self.base_url, api_key=self.api_key)

    def analyze_segment(
        self,
        video_path: str,
        start: float,
        end: float,
        profile: GameProfile,
    ) -> SegmentAnalysis:
        duration = max(0.0, end - start)
        frame_count = _frame_count_for(duration)
        frames = _sample_frames(video_path, start, end, frame_count)
        prompt = _build_prompt(profile, duration, frame_count)
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for jpg_bytes in frames:
            b64 = base64.b64encode(jpg_bytes).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            })

        client = self._client()

        def _call() -> str:
            resp = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a careful video analyst. Reply with JSON only."},
                    {"role": "user", "content": content},
                ],
                temperature=0.2,
            )
            return resp.choices[0].message.content or ""

        raw = _call()
        parsed = _parse_json(raw)
        if parsed is None:
            raw = _call()
            parsed = _parse_json(raw)
        if parsed is None:
            return SegmentAnalysis(events=[
                SegmentEvent(
                    start=0.0,
                    end=duration,
                    description="(analyzer failed to return valid JSON)",
                )
            ])
        return _coerce(parsed, profile, duration)


def _frame_count_for(duration: float) -> int:
    if duration <= 0:
        return 0
    target = int(duration // SECONDS_PER_FRAME)
    return max(FRAMES_FLOOR, min(FRAMES_CEILING, target))


def _sample_frames(video_path: str, start: float, end: float, count: int) -> list[bytes]:
    if end <= start or count <= 0:
        return []
    out: list[bytes] = []
    span = end - start
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(count):
            offset = start + span * (i + 0.5) / count
            path = Path(tmp) / f"f{i}.jpg"
            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
                "-ss", f"{offset:.3f}", "-i", str(video_path),
                "-frames:v", "1", "-q:v", "4", "-vf", "scale='min(768,iw)':-2",
                str(path),
            ]
            try:
                subprocess.run(cmd, check=True, capture_output=True)
                if path.exists():
                    out.append(path.read_bytes())
            except subprocess.CalledProcessError:
                continue
    return out


def _build_prompt(profile: GameProfile, duration: float, frame_count: int) -> str:
    sig_lines = _signals_block(profile, indent="      ")
    tax = _taxonomy_str(profile)
    return (
        f"Analyze a {duration:.1f}-second video segment sampled as {frame_count} frames "
        f"in temporal order (frame 0 = segment start, last frame ≈ segment end).\n"
        f"{profile.prompt_hints}\n\n"
        f"Allowed category tags (subset only): {tax}\n\n"
        f"TASK: split the segment into a sequence of distinct situations. A new\n"
        f"situation begins whenever ANY of these change: the on-foot/in-vehicle\n"
        f"state, the vehicle being driven, the wanted level, the activity\n"
        f"(walking, driving, shooting, crashing), the on-screen event/mission\n"
        f"text, or the location/environment. Each situation is a contiguous\n"
        f"time window [start, end] in seconds RELATIVE TO THE SEGMENT START\n"
        f"(0.0 = first frame, {duration:.1f} = last frame). Windows MUST NOT overlap.\n\n"
        f"How many events to return:\n"
        f"- Short clip (< 20s) with one continuous action: one event is fine.\n"
        f"- Anything longer with state changes (especially {duration:.1f}s): YOU MUST\n"
        f"  split into MULTIPLE events — typically one event per 20-60 seconds\n"
        f"  of footage, more if the action changes faster. Do NOT bundle\n"
        f"  separate activities into one long event just because they happen\n"
        f"  back-to-back.\n"
        f"- Skip dull stretches (idling, menus, loading) by leaving gaps\n"
        f"  between events; do not pad to cover the whole segment.\n\n"
        f"EXAMPLE for a 90-second clip where the player walks to a parked car,\n"
        f"steals it, and gets chased by police — return THREE events, NOT one:\n"
        f"{{\n"
        f'  "events": [\n'
        f'    {{"start": 0.0, "end": 18.0, "description": "Player walks down a city sidewalk past pedestrians toward a parked sedan.", "categories": ["cruising"], "base_interest": 20, "game_signals": {{"wanted_level": 0, "vehicles": [], "location": "Vinewood Boulevard"}}, "audio_cues": ["footsteps", "city ambience"]}},\n'
        f'    {{"start": 18.0, "end": 38.0, "description": "Player pulls the driver out of a sedan and accelerates away; two stars appear in the HUD.", "categories": ["npc_chaos"], "base_interest": 55, "game_signals": {{"wanted_level": 2, "vehicles": ["sedan"], "events": [], "location": "Vinewood Boulevard"}}, "audio_cues": ["car horn", "engine"]}},\n'
        f'    {{"start": 38.0, "end": 90.0, "description": "High-speed chase on the freeway with two police cruisers in pursuit, sirens wailing.", "categories": ["police_chase"], "base_interest": 85, "game_signals": {{"wanted_level": 3, "vehicles": ["sedan", "police_cruiser"], "events": [], "location": "Los Santos freeway"}}, "audio_cues": ["sirens", "engine"]}}\n'
        f"  ]\n"
        f"}}\n\n"
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
