# 0004 — Movie output modes: "movie recap" vs "movie clips"

- **Status:** accepted (recap built; clips planned)
- **Date:** 2026-06-29
- **Relates to:** [0001](0001-movie-recap-layering.md) (layering),
  [0003](0003-source-summary-rollup.md) (story spine).

## Context

Two distinct end-products are wanted from a film, and they must not be
conflated. Naming them up front keeps the layering clean.

## Decision

Define two **output modes** (application-layer recipes), both built on the same
shared `movie` *extract profile* + catalog. They differ only at the output
(stitch) stage — not in how the film is analyzed.

| Mode | What it produces | Narration | Ordering | State |
|---|---|---|---|---|
| **movie recap** | Whole film → a chronological **series** of ~60s shorts that **narrate the entire story** | yes (TTS voiceover is the spine; captions mirror it) | story order (`timeline`) | **built** — `.claude/skills/movie-recap/`, uses dialogue + synopsis/beat-sheet |
| **movie clips** | A set of standalone **30–60s segments of the interesting bits** | no | by interest, not story order | **planned** |

### Terminology

"movie recap" / "movie clips" are **output modes**, NOT extract profiles. The
extract `profiles` (`extract/profiles.py`) describe *analysis vocabulary* (the
`movie` profile's taxonomy/signals); both modes consume its output. Avoid
calling them "profiles" in code to prevent confusion with `GameProfile`.

## movie clips — implementation sketch (when built)

Almost no new engine; it's a thin selection + export recipe over existing
primitives:

- **Select** the top segments by score, filtered to a 30–60s window
  (`catalog_search(min_duration=30, max_duration=60, order_by="score" or a
  narrative score, ...)`). Each surviving clip is one output.
- **Export** each as its own short: reframe to 9:16, optional OST hook/label,
  optional music — **no voiceover, no captions**, keep or duck clip audio.
  This is a degenerate EDL (single video cue, `audio.kind="clip_audio"`).
- **Depends on Phase 3 narrative scoring** to make "interesting" mean
  story-meaningful rather than merely loud — the same ranking the recap's
  beat-aware selection needs. Build scoring once; both modes use it.

Likely shipped as a sibling skill (`movie-clips`) and/or a
`supaclip clips <source>` CLI wrapper.

## Open questions (defer to build time)

- One file per clip vs an optional stitched "highlights reel" compilation.
- Whether clips keep original dialogue audio (likely yes) and burn the film's
  own subtitles as captions (the dialogue is already in the catalog).

## Consequences

- Clear separation: recap = storytelling (narration-led); clips = highlights
  (segment-led). Shared analysis, divergent output.
- Reinforces the roadmap: **Phase 3 narrative scoring** is the shared
  dependency that unlocks good selection for both modes.
