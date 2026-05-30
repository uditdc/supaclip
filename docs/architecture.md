# supaclip — Architecture

This document captures the current shape of the codebase, the dataflow between
modules, and the persistent artifacts each stage produces and consumes. It is
intended as a map for new contributors and for Claude when reasoning about the
project end-to-end.

## 1. Big picture

supaclip is a three-phase local pipeline for turning long gameplay (or
similar) videos into short-form vertical clips:

```
  ┌──────────┐   manifest.json    ┌──────────┐   SQLite        ┌──────────┐
  │ EXTRACT  ├───────────────────▶│ CATALOG  │◀──MCP tools─────│  CLAUDE  │
  │ (Phase 1)│                    │ (global) │                 │ (host)   │
  └─────┬────┘                    └────┬─────┘                 └────┬─────┘
        │ master clips (.mp4) + keyframes (.jpg)                    │
        │                                                           │ EDL JSON
        ▼                                                           ▼
   clips/<source>/                                              ┌──────────┐
                                                                │  STITCH  │
                                                                │ (Phase 2)│
                                                                └────┬─────┘
                                                                     │
                                                                     ▼
                                                                short.mp4
                                                                + sidecar
                                                                  edl.json
```

- **Extract** segments a source video, runs each segment through a vision LLM,
  and emits master clips plus a Claude-readable `manifest.json`.
- **Catalog** ingests one or more manifests into a single SQLite DB
  (`~/.local/share/supaclip/catalog.db`) with FTS5 over descriptions/cues and
  exposes itself as an MCP server.
- **Stitch** consumes an EDL (Edit Decision List) authored by Claude (browsing
  the catalog over MCP) and renders a 1080×1920 short via ffmpeg
  (`filter_complex`), with TTS voiceover, OST captions, annotations, effects,
  transitions, and an optional music bed.

The top-level `supaclip` CLI is an umbrella dispatcher
(`supaclip/cli.py`). Subcommands `extract`, `catalog`, `stitch`, `mcp`, and
`debug-prompt` are exposed as their own entry points as well.

## 2. Repository layout

```
supaclip/
├── __init__.py                version
├── cli.py                     umbrella dispatcher
├── core/                      shared, pipeline-agnostic primitives
│   ├── ffmpeg.py              probe / loudness / keyframes / run_ffmpeg / cut
│   ├── manifest.py            Pydantic models: Manifest, Clip, SourceInfo
│   ├── edl.py                 Pydantic models + validate_edl()
│   ├── cache.py               file-fingerprint keyed JSON cache
│   └── log.py                 CLI logger
│
├── extract/                   Phase 1 — analyse a single video
│   ├── cli.py                 ExtractConfig parsing
│   ├── pipeline.py            orchestrates probe → segment → analyse → aggregate → write manifest
│   ├── profiles.py            GameProfile + taxonomy/signals (e.g. gta6)
│   ├── segment.py             manual / interval / scene / auto / file segmenters
│   ├── chunking.py            audio-trough chunking within a segment
│   ├── audio.py               loudness peaks + per-range audio factor
│   ├── analyze.py             SegmentEvent / SegmentAnalysis types + backend factory
│   ├── aggregate.py           final dedup/merge pass over events (LLM call)
│   ├── dedupe.py              IoU-based temporal merge of candidate ranges
│   ├── backends/
│   │   ├── _shared.py         JSON parsing + event coercion helpers
│   │   ├── frames.py          OpenAI-compat sprite-grid backend (Ollama / vLLM / etc.)
│   │   └── video.py           Google AI Studio full-video backend
│   ├── debug.py / debug_cli.py    dump prompt payload for one segment
│   └── dry_chunk.py           preview chunking without analysing
│
├── catalog/                   Phase 1.5 — query across all manifests
│   ├── paths.py               XDG-style catalog resolution + SUPACLIP_CATALOG
│   ├── db.py                  sqlite3 connect + migrate
│   ├── schema.py              SQLite DDL + FTS5 virtual table
│   ├── ingest.py              add_manifest / add_directory / remove_manifest
│   ├── search.py              FTS5 + structured filters + signal expressions
│   ├── cli.py                 `supaclip catalog …` commands
│   └── mcp.py                 FastMCP server (catalog_*, get_clip_preview, validate_edl, render_edl)
│
└── stitch/                    Phase 2 / 2.5 — render an EDL
    ├── cli.py                 `stitch validate|render` commands
    ├── render.py              high-level orchestrator (load EDL → synth → render)
    ├── assembly.py            builds the `ffmpeg -filter_complex` command
    ├── reframe.py             crop_center/left/right + letterbox
    ├── effects.py             freeze_first / ken_burns_{in,out} / slow_mo plans
    ├── transitions.py         cut / crossfade join chain
    ├── annotation.py          circle / box / arrow overlay filters
    ├── overlay.py             OST caption PNG renderer + overlay chain
    ├── music.py               music bed planner (sidechain duck)
    ├── progress.py            ffmpeg -progress parser
    └── tts/                   pluggable TTS backends
        ├── base.py            TTSBackend protocol
        ├── elevenlabs.py      ElevenLabs implementation
        └── cache.py           (text + voice + settings) → WAV cache
```

Tests live in `tests/` and cover aggregation, chunking, catalog search, EDL
validation, assembly/filter graph construction, effects, transitions, TTS,
and end-to-end render integration.

## 3. Phase 1 — Extract

### 3.1 Entry point and config

`extract <video.mp4> [--segmenter …] [--analyzer …] [--llm …]` dispatches into
`supaclip.extract.cli.main`, which assembles an `ExtractConfig` (see
`pipeline.py:44`) and calls `run()`.

Per video, `_run_one()` walks these stages:

| Stage         | Module                          | Cached on disk?                           |
|---------------|---------------------------------|-------------------------------------------|
| Probe         | `core/ffmpeg.py: probe()`       | no (ffprobe is cheap)                     |
| Audio energy  | `core/ffmpeg.py: extract_loudness_curve` + `extract/audio.py` | yes (`audio`/fingerprint)   |
| Segmenter     | `extract/segment.py`            | yes (`segments`/fingerprint+params+`v2-trough`) |
| Dedup         | `extract/dedupe.py`             | n/a (deterministic)                       |
| Analyse       | `extract/analyze.py` + backends | yes per-chunk (`analysis`/fingerprint+chunk+prompt_version) |
| Aggregate     | `extract/aggregate.py`          | yes (`aggregate`/fingerprint+event-signature) |
| Write manifest| `core/manifest.py: save_manifest` | n/a                                     |

Everything cacheable goes through `core.cache.Cache`, keyed by a file
fingerprint (`fingerprint_file()`) so editing the source invalidates results.

### 3.2 Segmenters

`segment.py` provides five strategies, all returning `list[(start, end)]`:

- `manual` — read `start,end` pairs from a CSV (supports `SS`, `MM:SS`, `HH:MM:SS`).
- `interval` — fixed-length sliding window with 5s overlap.
- `scene` — PySceneDetect `ContentDetector`.
- `auto` (default) — split the entire duration at local minima ("troughs") of
  the audio loudness curve, so events aren't cut in their middle.
- `file` — treat the input as a single pre-cut clip.

After segmentation, `clamp_ranges()` drops ranges shorter than `--min-clip`,
clips overlong ones to `--max-clip`, and `dedupe.merge_overlapping()`
collapses temporally overlapping ranges (IoU ≥ `--dedup-iou`).

### 3.3 Analyser backends

`extract/analyze.py:build_backend(name, model, base_url, api_key)` picks one
of two implementations. Both produce `SegmentAnalysis(events=[SegmentEvent])`.

- **`video`** (default) — full-video analysis. Uploads (or inlines, if < 15 MB)
  the actual segment video to Google AI Studio Files API, transcoded to
  720p / 800 kbps / 24 fps in chunks of ≤ 480s; retries on transient errors.
  Google-specific; validates a Google AI Studio key at construction.
- **`frames`** — short-frame analysis. Samples a few frames (≤ 16) into a single
  near-square sprite grid and sends that one image to any OpenAI-compatible
  vision endpoint (local Ollama, vLLM, etc.). Model-agnostic; validates an
  endpoint + model id at construction. One call per segment-or-chunk.

Both share `backends/_shared.py` for JSON parsing, event coercion against the
profile taxonomy, overlap pruning, and prompt scaffolding.

A `GameProfile` (`extract/profiles.py`) defines a taxonomy (allowed category
strings) and `game_signals` schema (e.g. `wanted_level: int`,
`vehicles: list[str]`). Only categories and signal keys in the active profile
survive coercion — the LLM cannot invent new ones.

### 3.4 Chunking

`extract/chunking.py:chunk_segment(start, end, samples)` splits a long
candidate segment into ≤ ~CHUNK_SECONDS sub-windows at audio troughs, with
~5 s overlap. Each chunk is analysed independently and cached at the chunk
level; emitted events are shifted from chunk-local → source-local time before
being collected.

### 3.5 Aggregation

When ≥ 2 events come out of the analyse stage, `aggregate.py:aggregate_events`
runs a *text-only* LLM pass that sees all event descriptions, categories,
signals, and timestamps — but no images. It merges:

- duplicates emitted by overlapping chunks/segments,
- fragments split across boundaries,
- back-to-back events describing one continuous situation.

The aggregator's transport is selected by `pipeline._build_agg_config`:
OpenAI-compat for `frames`, Google AI Studio for `video`. On any
failure the input event list is returned unchanged so the pipeline always
produces output. Finally `_enforce_min_duration()` extends or drops events
shorter than `MIN_CLIP_SECONDS` (10s).

### 3.6 Manifest

For each surviving event, `_run_one` extracts `--keyframes N` JPEGs at evenly
spaced midpoints, computes `peak_loudness_db` and a blended `score` (70%
LLM-reported `base_interest` + 30% audio factor), and writes a `Clip` row.
The full `Manifest` (Pydantic, see `core/manifest.py`) carries:

```
source { file, duration, resolution, fps }
extract { segmenter, analyzer, game_profile, created_at }
taxonomy [...]
clips [
  { id, file, source_in, source_out, duration, resolution, fps,
    description, categories, score, game_signals,
    audio { peak_loudness_db, cues },
    keyframes [...], segment_source }
]
```

`Clip.file` is stored relative to the manifest dir when the source lives
under it, otherwise absolute. The manifest is the deliverable of Phase 1 —
no other stage edits it.

## 4. Phase 1.5 — Catalog

### 4.1 Storage

A single SQLite DB at `~/.local/share/supaclip/catalog.db` (overridable via
`--catalog` or `SUPACLIP_CATALOG`). Schema (`catalog/schema.py`):

- `sources` (one row per unique fingerprint),
- `extracts` (one row per ingested manifest; FK → sources),
- `clips` (clip rows; FK → extracts; stores game_signals/audio/keyframes as JSON),
- `clip_categories` (M:N category index),
- `clips_fts` (FTS5 virtual table over `description`, `audio_cues`, `tags`).

`ingest.add_manifest()` ingests a manifest; `add_directory()` walks a tree
looking for `manifest.json`. Both upsert by `(source_id, created_at,
segmenter, analyzer, game_profile)` so re-ingesting is idempotent.

### 4.2 Search

`catalog/search.py` supports:

- FTS5 free-text `query` over description / audio cues / tags,
- `categories` filter (OR by default, AND with `all_categories=True`),
- score / duration ranges,
- `segmenter`, `game_profile`, `source` filters,
- signal expressions: `"key=value"` (exact) or `"key~=value"` (substring/in-list),
- `order_by` and `limit`.

Returned `ClipRow` objects expose absolute `file` and `keyframes` paths so
downstream tools (and Claude) don't need to know about the catalog DB
location.

### 4.3 MCP server

`catalog/mcp.py` (optional, install with `[mcp]`) exposes the catalog via
FastMCP. Tools:

- `catalog_search`, `catalog_get_clip`, `catalog_get_source`,
  `catalog_list_sources`, `catalog_stats` — wrappers around `search.py`.
- `get_clip_preview` — compact dict tailored for EDL composition (only the
  fields Claude needs to pick a clip and set `source_in`).
- `validate_edl` — runs `core.edl.validate_edl` with a catalog-backed
  resolver. Returns `{ok, issues}`.
- `render_edl` — invokes `stitch.render.render()`. Spends ElevenLabs credits
  on first call for a given `(text, voice, settings)` tuple; subsequent calls
  hit the TTS cache.

The Claude Code skill at `.claude/skills/stitch-director.md` chains
`catalog_search → get_clip_preview → validate_edl → render_edl` end-to-end
without user confirmation between steps.

## 5. Phase 2 / 2.5 — Stitch

### 5.1 EDL schema

The EDL is a single JSON document (Pydantic models in `core/edl.py`,
`schema_version=1`, `extra="forbid"`):

```
output         { width=1080, height=1920, fps=60, duration }
voiceover?     { backend="elevenlabs", voice_id, settings, script }
video[]        { start, end, clip_id, source_in?, reframe, reframe_offset,
                 effect, effect_params, transition_in, transition_duration }
audio[]        { start, end, kind: "voiceover" | "clip_audio" | "silence",
                 level_db?, duck? }
ost[]          { start, end, text, style, position }
annotations[]  { start, end, shape: "circle" | "box" | "arrow", x, y, …, color, stroke_width }
music?         { file, level_db=-22, duck=true }
```

`validate_edl()` enforces:

- monotonic, gap-free, non-overlapping `video[]` covering `[0, duration]`,
- ranges within `[0, duration]` for every track,
- crossfade duration ≤ ½ of the shorter neighbor cue,
- effect parameter ranges (`slow_mo.speed ∈ [0.05, 1]`, zoom > 0),
- annotation geometry (radius/width/height > 0 where required),
- voiceover-cue ↔ `voiceover` consistency,
- when a `resolver` is supplied: `clip_id` exists, cue duration ≤ available
  source footage at `source_in`.

### 5.2 Render orchestration

`stitch.render.render(RenderConfig)` (see `render.py`):

1. Load and (optionally) trim the EDL to a single preview cue.
2. Connect to the catalog and validate the EDL with `clip_id` resolution.
3. Resolve every video cue: look up the `ClipRow`, probe the source file,
   compute the effective `source_in` (cue override or clip default).
4. Synthesize voiceover via `tts.get_backend(...).synthesize(...)`, cached by
   `(backend, voice_id, settings, text)` (`tts/cache.py`).
5. Resolve the music file (catalog reference or filesystem path).
6. Pre-render OST caption PNGs (one per OST cue) into a content-addressed
   cache directory (`overlay.render_ost_pngs`), using PIL.
7. Build the ffmpeg invocation via `assembly.build_command(...)`.
8. Either print the command (`--print-only`) or run it via
   `progress.run_ffmpeg_with_progress` and stream progress events back to
   the caller.
9. Write a sidecar `<output>.edl.json` next to the rendered mp4 so the file
   carries its own provenance.

### 5.3 The filter graph

`assembly.build_command()` produces a single `-filter_complex` chain. For
each video cue:

- A separate `-ss src_in -t source_consumed -i file` input is added; how much
  source is consumed depends on the effect plan (e.g. `slow_mo` at 0.5×
  consumes half the cue duration; `freeze_first` consumes ~1 frame).
- The per-cue chain is `[i:v] reframe[, effect_snippet] [vi]`. `reframe`
  comes from `reframe.build_reframe_filter` (`crop_{center,left,right}` or
  `letterbox`). `effect_snippet` from `effects.plan_effect` (`freeze_first`,
  `ken_burns_in/out` via `zoompan`, `slow_mo` via `setpts`).
- `transitions.build_join_chain` joins the cues using `concat` for cuts and
  `xfade` for crossfades. Crossfade duration must fit within ½ of the
  shorter neighbor (enforced by validation).

After the joined video stream:

- `annotation.build_annotation_chain` overlays circles / boxes / arrows.
- `overlay.build_ost_overlay_chain` overlays the pre-rendered OST PNGs at
  their cue ranges.

Audio:

- Voiceover (if any): trimmed/padded to the output duration, optionally
  `asplit` for a sidechain when `music.duck=true`.
- Music (if any): `music.build_music_plan` produces an ffmpeg chain that
  loops/trims the bed and applies `sidechaincompress` against the voiceover
  split when ducking is requested.
- `clip_audio` cues: per-cue `atrim` + `adelay` to place the original
  in-clip audio at the right timeline position.
- The label list is `amix`-ed (or padded to silence if empty).

Final encode is `libx264 + aac + faststart` at the output `fps`.

### 5.4 TTS

`stitch/tts/` defines a `TTSBackend` protocol with one implementation
(`elevenlabs.ElevenLabsBackend`). The `EDLVoiceover.backend` literal is
intentionally narrow (`"elevenlabs"`) and is the extension point for future
providers. `TTSCache.key(backend, voice_id, settings, script)` is a stable
SHA of the inputs; cache hits avoid spending credits.

## 6. Cross-cutting concerns

### 6.1 Caching

Two independent caches:

- `core/cache.py: Cache` — JSON blobs at `~/.cache/supaclip/<bucket>/<key>.json`
  (or `--cache-dir`). Used by Extract for audio, segmentation, per-chunk
  analysis, and aggregator output. Disable per-run with `--no-cache`.
- `stitch/tts/cache.py: TTSCache` — WAV blobs at the same root, keyed by
  TTS inputs.

Both are file-system caches with no eviction; users manage size by deleting
the dir.

### 6.2 Logging

`core/log.py: Logger` is the shared progress/output channel. `extract` and
`stitch` both use the same `stage / info / detail / success / warn / error`
vocabulary so the user sees consistent output.

### 6.3 ffmpeg interface

All ffmpeg/ffprobe access flows through `core/ffmpeg.py` (probe, loudness
curve, keyframes, generic `run_ffmpeg`, concat, single-clip cut). `ensure_ffmpeg()`
is called at the entry point of every pipeline so the failure mode is one
clear message rather than a stack trace deep inside subprocess code.

`stitch/progress.py` wraps `run_ffmpeg` with `-progress pipe:` parsing so
long renders can stream `pct / speed / fps` events.

### 6.4 Schema versions

Two independent versioned formats:

- `core/manifest.py: SCHEMA_VERSION = 1` — bumped when `Manifest` changes.
- `core/edl.py: EDL_SCHEMA_VERSION = 1` — bumped when EDL changes; the
  validator flags mismatched versions.

Prompt versions (`extract/analyze.py: PROMPT_VERSION`,
`extract/aggregate.py: PROMPT_VERSION`) participate in cache keys so that
changing a prompt invalidates downstream results without touching code paths.

## 7. End-to-end flow (worked example)

```
$ extract session.mp4
  └─ probe                     → VideoInfo
  └─ extract_loudness_curve    → samples (cached)
  └─ auto segmenter            → ranges      (cached)
  └─ merge_overlapping(IoU)    → ranges
  └─ chunk + analyse per range → events      (cached per chunk)
  └─ aggregate_events          → events      (cached)
  └─ extract_keyframes per ev  → jpgs on disk
  └─ save_manifest             → clips/<source>/manifest.json

$ supaclip catalog add clips/session/manifest.json
  └─ upsert source + extract + clips + FTS rows in catalog.db

(in Claude Code, with `supaclip` MCP registered)
  Claude → catalog_search "police chase"        → [clip rows]
        → get_clip_preview(clip_id=…)          → preview dicts
        → compose EDL JSON
        → validate_edl(edl)                     → {ok: true}
        → render_edl(edl, output_path="x.mp4")  → {output, sidecar, duration}

  render_edl internally:
    └─ load EDL                                 → core/edl.py
    └─ validate                                 → catalog resolver
    └─ resolve cues + probe sources
    └─ synthesize voiceover                     → tts/elevenlabs (cached WAV)
    └─ render OST caption PNGs                  → PIL (cached by content hash)
    └─ build_command                            → -filter_complex
    └─ run_ffmpeg_with_progress                 → libx264/aac mp4 + sidecar EDL
```

## 8. Extension points

- **New analyser backend**: add a class implementing `AnalyzerBackend` in
  `extract/backends/`, register in `analyze.build_backend`. Reuse
  `backends/_shared.py` for JSON parsing and event coercion against the
  active `GameProfile`.
- **New game profile**: drop a profile definition into `extract/profiles.py`
  (taxonomy + signal schema). It immediately participates in coercion and
  prompt scaffolding without touching backend code.
- **New segmenter**: add a function to `extract/segment.py` and dispatch in
  `pipeline._segment`.
- **New EDL effect / transition / annotation shape**: extend the literal in
  `core/edl.py`, add a branch in `stitch/effects.py` (or `transitions.py` /
  `annotation.py`), extend `validate_edl` with the new invariants.
- **New TTS backend**: implement `TTSBackend` in `stitch/tts/`, register in
  `tts.get_backend`, and add the literal to `core/edl.py: TTSBackendName`.
- **New MCP tool**: add a `@server.tool()` function in `catalog/mcp.py`.
  Keep tool surface small and JSON-friendly; prefer wrapping existing
  Python APIs over duplicating logic.
