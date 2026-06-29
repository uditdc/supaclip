"""Whole-source rollup: synopsis, theme, characters, and a beat-sheet.

A text-only pass over the final event list (in story order, with dialogue) that
produces the "story spine" a recap is built from. User-provided context (an
up-front synopsis and cast via VideoContext) primes the pass; the model fills in
and structures the rest. Best-effort: any failure yields None and the pipeline
keeps producing a manifest.
"""

from __future__ import annotations

from typing import Any

from ..core.manifest import CharacterRole, SourceSummary, StoryBeat
from .backends._shared import _parse_json
from .llm import LLMConfig, call_json
from .profiles import GameProfile, VideoContext

PROMPT_VERSION = "sum-v1"
_SYSTEM = "You are a film analyst writing a faithful plot summary. Reply with JSON only."


def summarize_source(
    events: list[dict[str, Any]],
    *,
    source_duration: float,
    profile: GameProfile,
    cfg: LLMConfig,
    context: VideoContext | None = None,
    target_beats: int = 12,
) -> SourceSummary | None:
    if not events:
        return None

    ordered = sorted(events, key=lambda e: float(e.get("start", 0.0)))
    prompt = _build_prompt(ordered, source_duration, profile, context, target_beats)

    try:
        raw = call_json(prompt, cfg, system=_SYSTEM)
    except Exception:  # noqa: BLE001
        return None

    parsed = _parse_json(raw)
    if not isinstance(parsed, dict):
        return None

    summary = _coerce(parsed, source_duration, cfg.model)
    if not summary.synopsis and not summary.beats:
        return None
    return summary


def _coerce(parsed: dict[str, Any], source_duration: float, model: str) -> SourceSummary:
    characters: list[CharacterRole] = []
    for c in parsed.get("characters") or []:
        if isinstance(c, dict) and (c.get("name") or "").strip():
            characters.append(
                CharacterRole(name=str(c["name"]).strip(), role=str(c.get("role") or "").strip())
            )

    beats: list[StoryBeat] = []
    for b in parsed.get("beats") or []:
        if not isinstance(b, dict):
            continue
        try:
            start = max(0.0, min(float(b.get("start", 0.0)), source_duration))
            end = max(0.0, min(float(b.get("end", source_duration)), source_duration))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        beats.append(
            StoryBeat(
                title=str(b.get("title") or "").strip() or "Untitled beat",
                start=round(start, 3),
                end=round(end, 3),
                summary=str(b.get("summary") or "").strip(),
            )
        )
    beats.sort(key=lambda x: x.start)

    themes = [str(t).strip() for t in (parsed.get("themes") or []) if str(t).strip()]

    return SourceSummary(
        synopsis=str(parsed.get("synopsis") or "").strip(),
        themes=themes,
        tone=str(parsed.get("tone") or "").strip(),
        characters=characters,
        beats=beats,
        generated_by=model,
    )


def _build_prompt(
    events: list[dict[str, Any]],
    source_duration: float,
    profile: GameProfile,
    context: VideoContext | None,
    target_beats: int,
) -> str:
    lines: list[str] = []
    for ev in events:
        start = float(ev.get("start", 0.0))
        end = float(ev.get("end", 0.0))
        desc = (ev.get("description") or "").strip()
        dialogue = (ev.get("dialogue") or "").strip()
        line = f"- [{start:.1f}-{end:.1f}s] {desc}"
        if dialogue:
            line += f' | dialogue: "{dialogue}"'
        lines.append(line)
    body = "\n".join(lines)

    primer = ""
    if context is not None and not context.is_empty():
        parts = []
        if context.intro.strip():
            parts.append(f"Known synopsis / context (treat as authoritative):\n{context.intro.strip()}")
        if context.characters:
            cast = ", ".join(c.name for c in context.characters)
            parts.append(f"Known cast: {cast}")
        primer = "\n\n".join(parts) + "\n\n"

    return (
        f"{primer}"
        f"Below are the scenes of a {source_duration:.0f}-second {profile.subject}, in\n"
        f"chronological order, each with its on-screen description and (when\n"
        f"available) the dialogue spoken during it. Timestamps are in source\n"
        f"coordinates.\n\n"
        f"{body}\n\n"
        f"TASK: produce a faithful whole-film summary as the spine for a recap.\n"
        f"Ground everything in the scenes and dialogue above — do NOT invent plot,\n"
        f"names, or events not supported by them. Prefer character names from the\n"
        f"dialogue/known cast over generic labels.\n\n"
        f"Return JSON only:\n"
        f"{{\n"
        f'  "synopsis": "150-250 word spoiler-complete plot summary, present tense",\n'
        f'  "themes": ["2-4 short theme phrases"],\n'
        f'  "tone": "one phrase, e.g. tense thriller / warm comedy",\n'
        f'  "characters": [{{"name": "...", "role": "protagonist/antagonist/..."}}],\n'
        f'  "beats": [\n'
        f'    {{"title": "short beat name", "start": 0.0, "end": 0.0,\n'
        f'      "summary": "1-2 sentences on what happens in this stretch"}}\n'
        f"  ]\n"
        f"}}\n\n"
        f"Produce about {target_beats} contiguous, non-overlapping beats that cover\n"
        f"the whole runtime start to end, breaking on act/location/tonal shifts."
    )
