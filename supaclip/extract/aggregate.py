from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .backends._shared import _coerce, _parse_json
from .profiles import GameProfile

PROMPT_VERSION = "agg-v3"
MIN_CLIP_SECONDS = 10.0


@dataclass
class AggregateConfig:
    model: str
    base_url: str
    api_key: str | None
    provider: str = "openai"  # "openai" (OpenAI-compat) or "google" (AI Studio)


def aggregate_events(
    events: list[dict[str, Any]],
    *,
    source_duration: float,
    profile: GameProfile,
    cfg: AggregateConfig,
) -> list[dict[str, Any]]:
    """Final post-processing pass over events from EVERY analysis call.

    Input events are in SOURCE-COORDINATE timestamps (0.0 = start of the source
    video, source_duration = end). The aggregator sees descriptions, categories,
    signals, and timestamps — no images. It merges:
      - duplicate events that adjacent chunks/segments emitted independently,
      - fragments split across chunk/segment boundaries,
      - back-to-back events that describe the same continuous situation.

    Returns events with source-coordinate start/end. On any failure the input
    is returned unchanged so the pipeline keeps producing output.
    """
    if not events:
        return events
    if len(events) < 2:
        return events

    sorted_events = sorted(
        events,
        key=lambda e: (float(e.get("start", 0.0)), float(e.get("end", 0.0))),
    )
    prompt = _build_prompt(sorted_events, source_duration, profile)

    try:
        raw = _call(prompt, cfg)
    except Exception:  # noqa: BLE001
        return sorted_events

    parsed = _parse_json(raw)
    if parsed is None:
        return sorted_events

    refined = _coerce(parsed, profile, source_duration)
    if not refined.events:
        return sorted_events

    out = [
        {
            "start": ev.start,
            "end": ev.end,
            "description": ev.description,
            "categories": ev.categories,
            "base_interest": ev.base_interest,
            "game_signals": ev.game_signals,
            "audio_cues": [],
        }
        for ev in refined.events
    ]
    return _enforce_min_duration(out, source_duration)


def _enforce_min_duration(
    events: list[dict[str, Any]],
    source_duration: float,
) -> list[dict[str, Any]]:
    """Defensive post-filter: drop or extend events shorter than MIN_CLIP_SECONDS.

    If the model emits a short sliver despite the prompt's guidance, extend it
    by pulling time from the larger neighbor when one exists; otherwise drop it.
    """
    if not events:
        return events
    events = sorted(events, key=lambda e: (float(e.get("start", 0.0)), float(e.get("end", 0.0))))
    kept: list[dict[str, Any]] = []
    for ev in events:
        start = float(ev.get("start", 0.0))
        end = float(ev.get("end", 0.0))
        dur = end - start
        if dur >= MIN_CLIP_SECONDS:
            kept.append(ev)
            continue
        need = MIN_CLIP_SECONDS - dur
        prev_end = kept[-1]["end"] if kept else 0.0
        slack_before = start - float(prev_end)
        slack_after = source_duration - end
        take_before = min(need / 2.0, slack_before)
        take_after = min(need - take_before, slack_after)
        take_before = min(need - take_after, slack_before)
        if (dur + take_before + take_after) < MIN_CLIP_SECONDS:
            continue
        ev = {**ev, "start": round(start - take_before, 3), "end": round(end + take_after, 3)}
        kept.append(ev)
    return kept


def _call(prompt: str, cfg: AggregateConfig) -> str:
    if cfg.provider == "google":
        return _call_google(prompt, cfg)
    return _call_openai(prompt, cfg)


def _call_openai(prompt: str, cfg: AggregateConfig) -> str:
    from openai import OpenAI

    client = OpenAI(base_url=cfg.base_url, api_key=cfg.api_key or "ollama")
    resp = client.chat.completions.create(
        model=cfg.model,
        messages=[
            {"role": "system", "content": "You are a careful video editor. Reply with JSON only."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )
    return resp.choices[0].message.content or ""


def _call_google(prompt: str, cfg: AggregateConfig) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=cfg.api_key)
    resp = client.models.generate_content(
        model=_normalize_google_model(cfg.model),
        contents=[
            "You are a careful video editor. Reply with JSON only.\n\n" + prompt,
        ],
        config=types.GenerateContentConfig(
            temperature=0.2,
            response_mime_type="application/json",
        ),
    )
    return getattr(resp, "text", "") or ""


def _normalize_google_model(model: str) -> str:
    m = (model or "").strip()
    if m.startswith("google/"):
        m = m[len("google/"):]
    if m.endswith(":free"):
        m = m[: -len(":free")]
    return m or "gemini-2.0-flash"


def _build_prompt(
    events: list[dict[str, Any]],
    source_duration: float,
    profile: GameProfile,
) -> str:
    tax = ", ".join(profile.taxonomy) if profile.taxonomy else "(none)"

    bullets = []
    for ev in events:
        start = float(ev.get("start", 0.0))
        end = float(ev.get("end", 0.0))
        desc = (ev.get("description") or "").strip()
        cats = ev.get("categories") or []
        signals = ev.get("game_signals") or {}
        interest = ev.get("base_interest", 0)
        sig_str = ", ".join(f"{k}={v}" for k, v in signals.items()) if signals else ""
        bullets.append(
            f"- [{start:.1f}-{end:.1f}s] "
            f"interest={interest} "
            f"cats=[{', '.join(cats)}] "
            f"{('signals={'+sig_str+'} ') if sig_str else ''}"
            f"\"{desc}\""
        )
    body = "\n".join(bullets)

    return (
        f"A {source_duration:.1f}-second gameplay video ({profile.name}) was analyzed by\n"
        f"a vision model in several overlapping segments and chunks. Each bullet\n"
        f"below is one event the vision pass returned, with timestamps in SOURCE\n"
        f"coordinates (0.0 = source start, {source_duration:.1f} = source end).\n\n"
        f"Raw events from every analysis call (sorted by start time, may include\n"
        f"duplicates, fragments split at chunk/segment boundaries, and near-identical\n"
        f"events from overlap regions):\n"
        f"{body}\n\n"
        f"TASK: produce a clean, de-duplicated event list for the entire video.\n"
        f"Aggressively apply ALL of the rules below.\n\n"
        f"0. MINIMUM EVENT DURATION = 10 seconds. EVERY event in your output MUST\n"
        f"   span at least 10 seconds. If an input event is shorter than 10s:\n"
        f"   - MERGE it with the most semantically similar adjacent event (the\n"
        f"     short event is almost certainly a fragment or transition).\n"
        f"   - If no adjacent event is similar enough, EXTEND its window by\n"
        f"     absorbing the slack before/after, as long as you stay within the\n"
        f"     video duration and don't overlap a neighboring event.\n"
        f"   - If neither merge nor extend works (truly isolated 3s sliver),\n"
        f"     DROP it entirely. Do NOT emit any event shorter than 10s.\n\n"
        f"1. MERGE events that describe the SAME continuous situation:\n"
        f"   - Adjacent or overlapping windows whose descriptions are paraphrases\n"
        f"     of each other (e.g. \"driving a sedan downtown\" and \"the player\n"
        f"     continues driving the grey sedan through downtown\") MUST become\n"
        f"     ONE event. Take the earliest start and the latest end.\n"
        f"   - Same category + same vehicle + same activity + adjacent time = MERGE.\n"
        f"   - If three back-to-back events all describe cruising in the same car\n"
        f"     through the same area, they are ONE event, not three.\n\n"
        f"2. DROP duplicates from chunk/segment overlap regions:\n"
        f"   - Two events covering nearly the same window with similar descriptions:\n"
        f"     keep the better-described one (longer description, more specific\n"
        f"     location, higher base_interest).\n\n"
        f"3. REFINE descriptions for clarity. Keep them grounded in the input\n"
        f"   descriptions — DO NOT invent details, locations, NPC names, or events\n"
        f"   that aren't in the input bullets. Drop weasel words. 1-3 sentences.\n\n"
        f"4. KEEP genuinely distinct events separate, even when adjacent. A vehicle\n"
        f"   change, on-foot/in-vehicle transition, wanted-level change, or location\n"
        f"   change is a real boundary — don't merge across one.\n\n"
        f"5. PRESERVE structured fields:\n"
        f"   - When merging, take the UNION of vehicles/events lists.\n"
        f"   - Take the MAXIMUM of wanted_level across merged inputs.\n"
        f"   - Take the most specific (longest, most descriptive) `location` string.\n"
        f"   - Take the MAXIMUM base_interest across merged inputs.\n"
        f"   - Categories: union, then keep only tags from the allowed list below.\n\n"
        f"6. TIMESTAMPS:\n"
        f"   - Output times are in SOURCE coordinates (same as inputs).\n"
        f"   - Sort events by start time. Windows MUST NOT overlap each other.\n"
        f"   - When merging, the merged window = (min(starts), max(ends)).\n\n"
        f"Allowed category tags: {tax}\n\n"
        f"Output JSON only, no prose, no code fences:\n"
        f"{{\n"
        f'  "events": [\n'
        f"    {{\n"
        f'      "start": 0.0,\n'
        f'      "end": 0.0,\n'
        f'      "description": "...",\n'
        f'      "categories": ["..."],\n'
        f'      "base_interest": 0,\n'
        f'      "game_signals": {{...}}\n'
        f"    }}\n"
        f"  ]\n"
        f"}}"
    )
