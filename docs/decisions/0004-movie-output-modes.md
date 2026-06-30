# 0004 — Movie output modes: "movie recap" vs "movie clips"

- **Status:** accepted (recap built; clips built — skill + core primitives)
- **Date:** 2026-06-29 (updated 2026-06-30)
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

## movie clips — as built

One ~30–60s segment per **beat** of the source summary, each a standalone
vertical short with the film's **original audio** and its **own subtitles**
burned in, styled like ours. No voiceover. The engine pieces added:

- **Source-timed captions.** `EDLCaptions.cues` (list of `EDLCaptionCue
  {start,end,text}`) lets captions be driven by explicit pre-timed lines
  instead of voiceover alignment; `validate_edl` allows captions without a
  voiceover when cues are present; `render.py` styles them via the existing
  caption renderer (`captions.chunks_from_cues`). The film's `.srt` cues for a
  clip's window are offset to clip-local time and passed in.
- **Original audio, peak-normalized.** Film dialogue is mastered quiet, so each
  clip gets a **per-clip constant gain** set as the cue's `level_db`: the
  generator measures the segment's true peak (`ffmpeg volumedetect`) and lands
  it just under 0 dB. A single fixed gain per clip — no limiter, no adaptive
  normalizer — keeps loudness consistent across clips with **no intra-clip
  pumping/flicker**. (Adaptive `dynaudnorm` and a `+12 dB`+`alimiter` boost were
  both tried and rejected: the limiter pumped audibly right before each line of
  dialogue.) No engine audio change — just `EDLAudioCue.level_db`.
  Trade-off: peak-normalization is bounded by the clip's loudest moment, so
  dialogue in clips with loud effects/music stays relatively quiet; lifting it
  further would require dynamic-range compression (mild adaptiveness).
- **Selection:** per beat, the highest-score catalog clip ≥30s, windowed to
  ≤60s. Phase-3 narrative scoring will improve "interesting" later; score is a
  serviceable proxy now.

Each clip is a single-video-cue EDL: `clip_audio` (peak-normalized) +
`captions.cues` from the source subtitles.

**Upstreamed (2026-06-30) following the [0001](0001-movie-recap-layering.md)
layering** — scout scripts (`clips/demo/build_clips.py`, `build_commentary.py`)
were promoted by altitude, not wholesale:

- **Core primitives** (general, reusable): `core/ffmpeg.py: segment_decodes_clean`
  (corrupt-region pre-flight for real-world rips) and `measure_peak_db`
  (constant-gain helper); `extract/subtitles.py: cues_for_range` (clip-local
  timed source cues).
- **MCP exposure** so the skill can reach them: `probe_clip` (decodes_clean +
  peak_db) and `get_clip_subtitles` (timed clip-local source cues).
- **App layer**: the `movie-clips` skill encodes the recipe (one clip per beat,
  clean-clip selection via `probe_clip`, peak-gain audio, source-subtitle or
  commentary captions, watermark) with a `--commentary` mode. The commentary
  *narration* is LLM-authored — which is precisely why this is a skill, not a
  CLI. Selection policy, caption style, duck levels, and watermark are skill
  defaults, not core.

A headless `supaclip clips` CLI could still wrap the deterministic plain mode
later; commentary stays skill-only (needs the LLM).

## Open questions (deferred)

- One file per clip vs an optional stitched "highlights reel" compilation.
- Per-beat segment choice once Phase-3 narrative scoring lands.

## Consequences

- Clear separation: recap = storytelling (narration-led); clips = highlights
  (segment-led). Shared analysis, divergent output.
- Reinforces the roadmap: **Phase 3 narrative scoring** is the shared
  dependency that unlocks good selection for both modes.
