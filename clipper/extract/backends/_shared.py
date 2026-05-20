from __future__ import annotations

import json
import re
from typing import Any

from ..analyze import SegmentAnalysis, SegmentEvent
from ..profiles import GameProfile


MIN_EVENT_DURATION = 2.0


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _parse_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    stripped = text.strip()
    stripped = _JSON_FENCE_RE.sub("", stripped).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None


def _coerce(parsed: dict[str, Any], profile: GameProfile, duration: float) -> SegmentAnalysis:
    raw_events = parsed.get("events")
    if not isinstance(raw_events, list) or not raw_events:
        single = _coerce_event(parsed, profile, duration, fallback_window=(0.0, duration))
        if single is None:
            return SegmentAnalysis(events=[SegmentEvent(
                start=0.0, end=duration,
                description="(analyzer returned no events)",
            )])
        return SegmentAnalysis(events=[single])

    events: list[SegmentEvent] = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        ev = _coerce_event(raw, profile, duration, fallback_window=None)
        if ev is not None:
            events.append(ev)

    events.sort(key=lambda e: e.start)
    events = _prune_overlaps(events)

    if not events:
        first_raw = next((r for r in raw_events if isinstance(r, dict)), None)
        if first_raw is not None:
            forced = dict(first_raw)
            forced["start"] = 0.0
            forced["end"] = duration
            salvaged = _coerce_event(
                forced, profile, duration, fallback_window=(0.0, duration),
            )
            if salvaged is not None:
                return SegmentAnalysis(events=[salvaged])
        events = [SegmentEvent(
            start=0.0, end=duration,
            description="(analyzer returned no usable events)",
        )]
    return SegmentAnalysis(events=events)


def _coerce_event(
    raw: dict[str, Any],
    profile: GameProfile,
    duration: float,
    fallback_window: tuple[float, float] | None,
) -> SegmentEvent | None:
    try:
        start = float(raw.get("start", 0.0))
        end = float(raw.get("end", duration if fallback_window else 0.0))
    except (TypeError, ValueError):
        if fallback_window is None:
            return None
        start, end = fallback_window

    start = max(0.0, min(start, duration))
    end = max(0.0, min(end, duration))
    if end <= start:
        if fallback_window is None:
            return None
        start, end = fallback_window
    if end - start < MIN_EVENT_DURATION and fallback_window is None:
        return None

    description = str(raw.get("description") or "").strip() or "(no description)"

    raw_cats = raw.get("categories") or []
    if not isinstance(raw_cats, list):
        raw_cats = []
    allowed = set(profile.taxonomy)
    categories = [c for c in raw_cats if isinstance(c, str) and c in allowed]

    try:
        base_interest = int(raw.get("base_interest") or 0)
    except (TypeError, ValueError):
        base_interest = 0
    base_interest = max(0, min(100, base_interest))

    raw_signals = raw.get("game_signals") or {}
    if not isinstance(raw_signals, dict):
        raw_signals = {}
    keys = set(profile.signal_keys())
    game_signals = {k: v for k, v in raw_signals.items() if k in keys}

    raw_cues = raw.get("audio_cues") or []
    if not isinstance(raw_cues, list):
        raw_cues = []
    audio_cues = [str(c) for c in raw_cues if c]

    return SegmentEvent(
        start=round(start, 3),
        end=round(end, 3),
        description=description,
        categories=categories,
        base_interest=base_interest,
        game_signals=game_signals,
        audio_cues=audio_cues,
    )


def _prune_overlaps(events: list[SegmentEvent]) -> list[SegmentEvent]:
    kept: list[SegmentEvent] = []
    for ev in events:
        if not kept:
            kept.append(ev)
            continue
        prev = kept[-1]
        if ev.start >= prev.end:
            kept.append(ev)
            continue
        overlap = min(prev.end, ev.end) - max(prev.start, ev.start)
        shorter = min(prev.end - prev.start, ev.end - ev.start)
        if shorter > 0 and overlap / shorter > 0.5:
            if ev.base_interest > prev.base_interest:
                kept[-1] = ev
            continue
        ev.start = prev.end
        if ev.end - ev.start >= MIN_EVENT_DURATION:
            kept.append(ev)
    return kept


def _signals_block(profile: GameProfile, indent: str = "      ") -> str:
    return "\n".join(
        f"{indent}- {s.key} ({s.type}): {s.description}" for s in profile.signals
    )


def _taxonomy_str(profile: GameProfile) -> str:
    return ", ".join(profile.taxonomy) if profile.taxonomy else "(none)"
