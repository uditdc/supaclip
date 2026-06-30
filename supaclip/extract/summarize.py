"""Whole-source rollup: synopsis, theme, characters, and a beat-sheet.

A text pass over the final scenes (in story order, with dialogue) that produces
the "story spine" a recap is built from. User-provided context (an up-front
synopsis and cast via VideoContext) primes it; the model fills in and structures
the rest.

Short films are summarized in one call. Long films would overflow the model
context, so they are summarized hierarchically: scenes are split into windows,
each window is summarized, then a reduce pass writes the global synopsis from
the window summaries. Best-effort throughout: any failure yields None (or the
salvageable partial) and the pipeline keeps producing a manifest.
"""

from __future__ import annotations

from typing import Any

from ..core.manifest import CharacterRole, SourceSummary, StoryBeat
from .backends._shared import _parse_json
from .llm import LLMConfig, call_json
from .profiles import GameProfile, VideoContext

PROMPT_VERSION = "sum-v2"
WINDOW_SCENES = 30
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
    if len(ordered) <= WINDOW_SCENES:
        return _single_pass(ordered, source_duration, profile, cfg, context, target_beats)
    return _hierarchical(ordered, source_duration, profile, cfg, context)


def _single_pass(
    events: list[dict[str, Any]],
    source_duration: float,
    profile: GameProfile,
    cfg: LLMConfig,
    context: VideoContext | None,
    target_beats: int,
) -> SourceSummary | None:
    prompt = _full_prompt(events, source_duration, profile, context, target_beats)
    parsed = _try_json(prompt, cfg)
    if parsed is None:
        return None
    summary = SourceSummary(
        synopsis=str(parsed.get("synopsis") or "").strip(),
        themes=_str_list(parsed.get("themes")),
        tone=str(parsed.get("tone") or "").strip(),
        characters=_coerce_characters(parsed.get("characters")),
        beats=_coerce_beats(parsed.get("beats"), source_duration),
        generated_by=cfg.model,
    )
    if not summary.synopsis and not summary.beats:
        return None
    return summary


def _hierarchical(
    events: list[dict[str, Any]],
    source_duration: float,
    profile: GameProfile,
    cfg: LLMConfig,
    context: VideoContext | None,
) -> SourceSummary | None:
    windows = [events[i:i + WINDOW_SCENES] for i in range(0, len(events), WINDOW_SCENES)]
    partial_synopses: list[str] = []
    characters: list[CharacterRole] = []
    beats: list[StoryBeat] = []
    seen_names: set[str] = set()

    for window in windows:
        parsed = _try_json(_window_prompt(window, source_duration, profile, context), cfg)
        if parsed is None:
            continue
        syn = str(parsed.get("synopsis") or "").strip()
        if syn:
            partial_synopses.append(syn)
        for ch in _coerce_characters(parsed.get("characters")):
            if ch.name.lower() not in seen_names:
                seen_names.add(ch.name.lower())
                characters.append(ch)
        beats.extend(_coerce_beats(parsed.get("beats"), source_duration))

    if not partial_synopses and not beats:
        return None

    beats.sort(key=lambda b: b.start)
    reduced = _try_json(_reduce_prompt(partial_synopses, profile, context), cfg) if partial_synopses else None
    if reduced and str(reduced.get("synopsis") or "").strip():
        synopsis = str(reduced["synopsis"]).strip()
        themes = _str_list(reduced.get("themes"))
        tone = str(reduced.get("tone") or "").strip()
    else:
        synopsis = " ".join(partial_synopses)[:2000]
        themes, tone = [], ""

    return SourceSummary(
        synopsis=synopsis,
        themes=themes,
        tone=tone,
        characters=characters,
        beats=beats,
        generated_by=cfg.model,
    )


def _try_json(prompt: str, cfg: LLMConfig) -> dict[str, Any] | None:
    try:
        raw = call_json(prompt, cfg, system=_SYSTEM)
    except Exception:  # noqa: BLE001
        return None
    parsed = _parse_json(raw)
    return parsed if isinstance(parsed, dict) else None


def _str_list(value: Any) -> list[str]:
    return [str(t).strip() for t in (value or []) if str(t).strip()]


def _coerce_characters(value: Any) -> list[CharacterRole]:
    out: list[CharacterRole] = []
    for c in value or []:
        if isinstance(c, dict) and (c.get("name") or "").strip():
            out.append(CharacterRole(name=str(c["name"]).strip(), role=str(c.get("role") or "").strip()))
    return out


def _coerce_beats(value: Any, source_duration: float) -> list[StoryBeat]:
    beats: list[StoryBeat] = []
    for b in value or []:
        if not isinstance(b, dict):
            continue
        try:
            start = max(0.0, min(float(b.get("start", 0.0)), source_duration))
            end = max(0.0, min(float(b.get("end", source_duration)), source_duration))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        beats.append(StoryBeat(
            title=str(b.get("title") or "").strip() or "Untitled beat",
            start=round(start, 3),
            end=round(end, 3),
            summary=str(b.get("summary") or "").strip(),
        ))
    beats.sort(key=lambda x: x.start)
    return beats


def _primer(context: VideoContext | None) -> str:
    if context is None or context.is_empty():
        return ""
    parts = []
    if context.intro.strip():
        parts.append(f"Known synopsis / context (treat as authoritative):\n{context.intro.strip()}")
    if context.characters:
        parts.append("Known cast: " + ", ".join(c.name for c in context.characters))
    return "\n\n".join(parts) + "\n\n"


def _scene_lines(events: list[dict[str, Any]]) -> str:
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
    return "\n".join(lines)


def _full_prompt(
    events: list[dict[str, Any]],
    source_duration: float,
    profile: GameProfile,
    context: VideoContext | None,
    target_beats: int,
) -> str:
    return (
        f"{_primer(context)}"
        f"Below are the scenes of a {source_duration:.0f}-second {profile.subject}, in\n"
        f"chronological order, each with its on-screen description and (when available)\n"
        f"the dialogue spoken during it. Timestamps are in source coordinates.\n\n"
        f"{_scene_lines(events)}\n\n"
        f"TASK: produce a faithful whole-film summary as the spine for a recap.\n"
        f"Ground everything in the scenes and dialogue above — do NOT invent plot,\n"
        f"names, or events not supported by them. Prefer character names from the\n"
        f"dialogue/known cast over generic labels.\n\n"
        f"Return JSON only:\n"
        f'{{"synopsis": "150-250 word spoiler-complete plot summary, present tense",\n'
        f' "themes": ["2-4 short theme phrases"],\n'
        f' "tone": "one phrase, e.g. tense thriller / warm comedy",\n'
        f' "characters": [{{"name": "...", "role": "protagonist/antagonist/..."}}],\n'
        f' "beats": [{{"title": "short beat name", "start": 0.0, "end": 0.0,\n'
        f'   "summary": "1-2 sentences on what happens"}}]}}\n\n'
        f"Produce about {target_beats} contiguous, non-overlapping beats covering the\n"
        f"whole runtime start to end, breaking on act/location/tonal shifts."
    )


def _window_prompt(
    events: list[dict[str, Any]],
    source_duration: float,
    profile: GameProfile,
    context: VideoContext | None,
) -> str:
    start = float(events[0].get("start", 0.0))
    end = float(events[-1].get("end", source_duration))
    return (
        f"{_primer(context)}"
        f"Below is a CONTIGUOUS PORTION ({start:.0f}-{end:.0f}s) of a longer\n"
        f"{profile.subject}, in chronological order, each scene with its description\n"
        f"and (when available) spoken dialogue.\n\n"
        f"{_scene_lines(events)}\n\n"
        f"TASK: summarize JUST this portion, grounded only in the scenes/dialogue\n"
        f"above (do not invent). Return JSON only:\n"
        f'{{"synopsis": "3-5 sentences on what happens in this portion",\n'
        f' "characters": [{{"name": "...", "role": "..."}}],\n'
        f' "beats": [{{"title": "...", "start": 0.0, "end": 0.0, "summary": "1-2 sentences"}}]}}\n\n'
        f"Beats must be contiguous and stay within {start:.0f}-{end:.0f}s."
    )


def _reduce_prompt(
    partial_synopses: list[str],
    profile: GameProfile,
    context: VideoContext | None,
) -> str:
    joined = "\n\n".join(f"PART {i + 1}: {s}" for i, s in enumerate(partial_synopses))
    return (
        f"{_primer(context)}"
        f"Below are sequential plot summaries of consecutive portions of a single\n"
        f"{profile.subject}, in order.\n\n"
        f"{joined}\n\n"
        f"TASK: combine them into one coherent whole-film summary. Ground it only in\n"
        f"the parts above. Return JSON only:\n"
        f'{{"synopsis": "150-250 word spoiler-complete plot summary, present tense",\n'
        f' "themes": ["2-4 short theme phrases"],\n'
        f' "tone": "one phrase"}}'
    )
