# supaclip — Extract CLI — Product Requirements Document

| | |
|---|---|
| **Project** | `supaclip` (Python package: `supaclip`) |
| **Component** | Phase 1 — Extract CLI (command: `extract`) |
| **Status** | Locked — ready for implementation |
| **Audience** | This document is the implementation brief for Claude Code. It is self-contained; no prior conversation context is required. |

---

## 1. Summary

Build `extract`, a command-line tool that ingests local gameplay video (primarily Grand Theft Auto footage), splits it into meaningful segments, analyzes each segment with a vision-language model to produce a rich textual description and structured metadata, and writes the result as **native-aspect master clips plus a `manifest.json` catalog**.

The manifest is the deliverable. It is designed to be consumed downstream by an LLM ("Claude as director") in a separate Phase 2 "Stitch" tool that assembles short-form videos. Phase 2 is **out of scope** for this PRD.

---

## 2. Background & Rationale

Existing auto-clipping tools fall into two camps, and neither fits the goal:

- **Transcript-driven clippers** (OpusClip-style) only analyze spoken words. They are blind to gameplay, trailers, and any footage without commentary.
- **Gaming highlight tools** (Eklipse-style) detect competitive FPS events (kills, clutches). GTA content is emergent and narrative — police chases, stunts, vehicle chaos, NPC comedy, scenic cruising — and is not reducible to kill events.

Extract takes a third approach: a general vision-language model **describes and categorizes** every segment, producing a programmable, semantically rich catalog rather than a fixed highlight list. This is a better fit for varied gameplay and, unlike closed SaaS, runs locally and is scriptable.

---

## 3. Goals

- **G1.** Accept one or more local video files as input.
- **G2.** Segment footage automatically, and also accept user-supplied cut timestamps.
- **G3.** Analyze each segment with a vision-language model (default: Gemma 4 via a local Ollama endpoint), producing a Claude-readable description, category tags, a score, and game-specific structured signals.
- **G4.** Output native-aspect master clips (no reframing) and a schema-valid `manifest.json`.
- **G5.** Be fully local-capable, scriptable, cacheable, and free of accounts/servers/databases.
- **G6.** Keep the analyzer backend pluggable so a higher-quality model can be swapped in later.

---

## 4. Non-Goals (explicitly out of scope)

The implementation **must not** build any of the following. Listing them so the agent does not over-build:

- **No reframing / cropping.** Clips are cut at the source's native aspect ratio. Aspect conversion (9:16, etc.) belongs to Phase 2.
- **No editing.** No captions, overlays, transitions, music, or montage assembly.
- **No publishing.** No social-platform integration.
- **No transcription.** Analysis is visual; audio is used only for energy/peak detection.
- **No YouTube / network ingestion.** Local files only.
- **No Stitch CLI / MCP server.** Phase 2.
- **No server, database, job queue, web UI, or authentication.**

---

## 5. Target User & Primary Use Case

A developer or content creator working with **GTA gameplay recordings** on their own machine. They run `extract` on a long capture, then hand the resulting manifest to Claude (Phase 2) to direct an edit. Runs entirely offline if a local Ollama install is used.

---

## 6. Architecture Overview

### Pipeline — five stages

```
ingest ──▶ segment ──▶ dedupe ──▶ analyze ──▶ catalog
           (+audio     (merge      (Gemma 4,    (masters +
            energy)    overlaps)    game-aware)  manifest.json)
```

The pipeline is synchronous and single-process. Each stage's output is described in Section 7.

### Repo layout

```
supaclip/
├── pyproject.toml
├── README.md
├── .env.example
├── supaclip/
│   ├── __init__.py
│   ├── core/
│   │   ├── manifest.py      # manifest Pydantic models + read/write
│   │   ├── ffmpeg.py        # ffprobe/ffmpeg wrappers
│   │   ├── cache.py         # content-hashed JSON cache
│   │   └── log.py           # console logging helpers
│   └── extract/
│       ├── __init__.py
│       ├── cli.py           # argparse entry point
│       ├── pipeline.py      # orchestrates the five stages
│       ├── segment.py       # auto | manual | scene | interval
│       ├── audio.py         # loudness curve + peak detection
│       ├── dedupe.py        # temporal-overlap merge
│       ├── analyze.py       # pluggable analyzer interface
│       ├── profiles.py      # game profiles (gta built-in)
│       └── backends/
│           ├── __init__.py
│           └── gemma.py     # default analyzer backend
└── tests/
    └── test_core.py
```

`supaclip/core/` is shared library code that Phase 2 will also use. A `stitch/` package is intentionally absent.

---

## 7. Functional Requirements

### 7.1 Ingest

- **FR-1.1** Accept one or more local video file paths as positional arguments.
- **FR-1.2** Validate each path exists and is a file; exit with a clear error otherwise.
- **FR-1.3** Probe each video with `ffprobe` for width, height, duration, fps, and audio-stream presence.
- **FR-1.4** Reject inputs longer than `--max-duration` (default 5400 s) with an actionable message.
- **FR-1.5** Verify `ffmpeg` and `ffprobe` are on `PATH`; if missing, exit with install guidance.

### 7.2 Segment

Produces a list of candidate `(start, end)` time ranges. Four strategies, selected by `--segmenter`; default is `auto`.

- **FR-2.1 `auto` (default).** A two-pass approach:
  1. **Audio-energy pre-pass** (see FR-2.5) identifies activity peaks.
  2. A low-frame-rate VLM pass over the whole video proposes candidate segment boundaries, **seeded toward the audio-energy peaks**. The VLM returns time ranges (with optional one-line labels). Per-segment detailed analysis happens later in Stage 4 (Analyze).
- **FR-2.2 `manual`.** Read `(start, end)` pairs from the file given by `--timestamps` (CSV: `start,end` per line, accepting `SS`, `MM:SS`, or `HH:MM:SS`). These ranges are used verbatim.
- **FR-2.3 `scene`.** Use PySceneDetect to detect shot boundaries. (Useful mainly for pre-edited footage.)
- **FR-2.4 `interval`.** Fixed windows of `--interval` seconds (default 60) with modest overlap.
- **FR-2.5 Audio-energy signal.** A pre-pass extracts a loudness/energy curve from the audio track using `ffmpeg` (e.g. the `ebur128` or `astats` filter — no additional system dependency) and identifies local-maximum peaks above a percentile threshold. Peaks represent likely action (gunfire, sirens, crashes, explosions, engine revs). The peak list is:
  - used to **seed the `auto` segmenter** toward eventful regions, and
  - retained for **clip scoring** (FR-4.6) under all strategies.
- **FR-2.6** Every produced range must respect `--min-clip` (default 15 s) and `--max-clip` (default 60 s) bounds; ranges outside the bounds are clamped or dropped.
- **FR-2.7** If a video has no audio stream, the audio-energy pre-pass is skipped gracefully and `auto` falls back to unseeded boundary proposal.

### 7.3 Dedupe

- **FR-3.1** After segmentation, compute the temporal **IoU** (intersection ÷ union of time ranges) for every pair of candidate segments.
- **FR-3.2** Any pair with IoU ≥ `--dedup-iou` (default 0.6) is **merged into the union** of the two ranges. Repeat until no pair exceeds the threshold (stable result).
- **FR-3.3** Dedupe runs **before** analysis so duplicate segments are not analyzed twice.
- **FR-3.4** `--no-dedup` disables this stage.
- **FR-3.5** (Optional, lower priority) A semantic dedupe pass *after* analysis may drop clips with near-identical `description` + `categories`, keeping the higher-scored one. Implement only if straightforward.

### 7.4 Analyze

For each deduplicated segment, an analyzer backend produces a description, categories, a base interest score, and game-specific structured signals.

- **FR-4.1 Backend interface.** Define a backend interface (e.g. `analyze_segment(video, start, end, profile) -> SegmentAnalysis`). Selected by `--analyzer`; default `gemma`.
- **FR-4.2 Gemma backend.** Calls a vision-language model over an **OpenAI-compatible chat API** (default endpoint: Ollama at `http://localhost:11434/v1`; default model `gemma4`). The backend samples a handful of frames evenly across the segment, sends them as image content blocks, and requests **structured JSON** output. Output JSON must be defensively parsed (strip code fences, locate outermost braces) and validated with Pydantic; retry once on parse failure.
- **FR-4.3 Description.** A concise prose description of what happens in the segment, written for an LLM reader (Claude). Grounded in the visible footage; no invented detail.
- **FR-4.4 Categories.** Zero or more tags drawn from the active game profile's taxonomy (FR-4.7).
- **FR-4.5 Game-aware signals.** The analyzer extracts the structured `signals` defined by the active game profile (FR-4.7). For the `gta` profile: wanted level (0–5), vehicles in frame, recognized on-screen event text (`WASTED` / `BUSTED` / `MISSION PASSED` / `MISSION FAILED`), location, and NPC involvement.
- **FR-4.6 Scoring.** The analyzer returns a `base_interest` integer (0–100). The final clip `score` is a weighted blend of `base_interest` and an `audio_factor` (0–100) derived from the normalized audio-peak intensity over the clip's time range. Default blend: `score = round(0.7 * base_interest + 0.3 * audio_factor)`.
- **FR-4.7 Game profiles.** Game-awareness is configurable via a **game profile**, not hardcoded. A profile defines: `name`, a category `taxonomy` (list of strings), `signals` (list of structured fields, each with `key`, `type`, `description`), and `prompt_hints` (extra guidance injected into the analyzer prompt). A built-in `gta` profile ships as the default. `--game-profile` accepts either a built-in name or a path to a profile JSON file.
- **FR-4.8** Keyframe images are extracted per segment (`--keyframes`, default 3) for downstream visual reference by Claude.

### 7.5 Catalog

- **FR-5.1** Cut each final segment into its own **native-aspect master `.mp4`** via `ffmpeg` (re-encode acceptable; no scaling, no cropping). Name files `clip_NN.mp4` (zero-padded, 1-based).
- **FR-5.2** Save the extracted keyframes alongside each clip as `clip_NN.kfNN.jpg`.
- **FR-5.3** Write `manifest.json` (Section 8.1) into the output directory.
- **FR-5.4** Default output directory is `clips/`, overridable with `-o/--output`.

---

## 8. Data Schemas

### 8.1 Manifest (`manifest.json`)

```jsonc
{
  "schema_version": 1,
  "source": {
    "file": "/abs/path/session.mp4",
    "duration": 3600.0,
    "resolution": "1920x1080",
    "fps": 60
  },
  "extract": {
    "segmenter": "auto",
    "analyzer": "gemma4",
    "game_profile": "gta",
    "created_at": "2026-05-17T12:00:00+05:30"
  },
  "taxonomy": ["police_chase","shootout","stunt","crash",
               "npc_chaos","cruising","mission","fail"],
  "clips": [
    {
      "id": "clip_007",
      "file": "clips/clip_007.mp4",
      "source_in": 132.4,
      "source_out": 159.0,
      "duration": 26.6,
      "resolution": "1920x1080",
      "fps": 60,
      "description": "Player triggers a 4-star wanted level and leads police on a freeway chase, T-boning a cruiser before being busted.",
      "categories": ["police_chase", "crash"],
      "score": 86,
      "game_signals": {
        "wanted_level": 4,
        "vehicles": ["police cruiser", "sports car"],
        "events": ["BUSTED"],
        "location": "Los Santos freeway",
        "npcs": "multiple police officers"
      },
      "audio": {
        "peak_loudness_db": -8.2,
        "cues": ["sirens", "collision"]
      },
      "keyframes": ["clips/clip_007.kf01.jpg", "clips/clip_007.kf02.jpg"],
      "segment_source": "auto"
    }
  ]
}
```

**Field notes**

- `source_in` / `source_out` — start/end in the source video, seconds (float).
- `description` — prose for an LLM reader; the primary field Claude reasons over in Phase 2.
- `categories` — subset of the manifest-level `taxonomy`.
- `score` — final blended score 0–100 (FR-4.6).
- `game_signals` — keys are defined by the active game profile; the example shows the `gta` profile's fields.
- `segment_source` — which segmentation strategy produced this clip.

The manifest must be implemented as Pydantic models in `core/manifest.py` with read/write helpers, and validate on load.

### 8.2 Game Profile

```jsonc
{
  "name": "gta",
  "taxonomy": ["police_chase","shootout","stunt","crash",
               "npc_chaos","cruising","mission","fail"],
  "signals": [
    { "key": "wanted_level", "type": "int",
      "description": "On-screen wanted stars, 0-5; null if not visible." },
    { "key": "vehicles", "type": "list[str]",
      "description": "Vehicle types visible in the segment." },
    { "key": "events", "type": "list[str]",
      "description": "Recognized on-screen event text: WASTED, BUSTED, MISSION PASSED, MISSION FAILED." },
    { "key": "location", "type": "str",
      "description": "In-game location or environment." },
    { "key": "npcs", "type": "str",
      "description": "Notable NPC presence or interactions." }
  ],
  "prompt_hints": "This is Grand Theft Auto gameplay. Pay attention to the wanted-level stars (top-right HUD), vehicles, pedestrians, and on-screen mission/status text."
}
```

The built-in `gta` profile lives in `profiles.py`. Custom profiles load from a JSON file of the same shape.

---

## 9. CLI Specification

```
extract VIDEO [VIDEO ...] [options]
```

| Flag | Default | Description |
|---|---|---|
| `VIDEO` | — | One or more local video files (positional) |
| `-o, --output DIR` | `clips` | Output directory |
| `--segmenter {auto,manual,scene,interval}` | `auto` | Segmentation strategy |
| `--timestamps FILE` | — | `start,end` pairs file (required for `--segmenter manual`) |
| `--interval SECONDS` | `60` | Window length for `interval` strategy |
| `--game-profile NAME\|FILE` | `gta` | Built-in profile name or path to a profile JSON |
| `--analyzer {gemma,gemma-video}` | `gemma` | Analyzer backend |
| `--llm MODEL` | `gemma4` | Analyzer model id |
| `--base-url URL` | `http://localhost:11434/v1` | OpenAI-compatible endpoint |
| `--api-key KEY` | — | API key (unused for local Ollama) |
| `--keyframes N` | `3` | Keyframes extracted per clip |
| `--dedup-iou FLOAT` | `0.6` | Temporal-overlap merge threshold |
| `--no-dedup` | off | Disable the dedupe stage |
| `--min-clip SECONDS` | `15` | Minimum clip length |
| `--max-clip SECONDS` | `60` | Maximum clip length |
| `--max-duration SECONDS` | `5400` | Reject inputs longer than this |
| `--cache-dir DIR` | `~/.cache/supaclip` | Cache location |
| `--no-cache` | off | Ignore and do not write the cache |
| `--keep-temp` | off | Keep intermediate files |
| `--json` | off | Print the manifest to stdout on completion |
| `-v, --verbose` | off | Detailed progress logging |

---

## 10. Configuration & Environment

- A `.env` file in the working directory is loaded automatically (via `python-dotenv`).
- Environment variables (CLI flags override them):
  - `LLM_BASE_URL` / `OPENAI_BASE_URL` — analyzer endpoint
  - `LLM_API_KEY` / `OPENAI_API_KEY` — analyzer key
  - `LLM_MODEL` — analyzer model id
- Provide a `.env.example` documenting the local-Ollama default and a hosted (OpenRouter) alternative.

---

## 11. Caching

- Content-hashed JSON cache under `--cache-dir`, implemented in `core/cache.py`.
- A source file is fingerprinted cheaply by `size + mtime + resolved path`.
- Cache **segmentation results** keyed by `(fingerprint, segmenter, interval)` and **per-segment analysis** keyed by `(fingerprint, segment range, analyzer model, game profile, prompt version)`.
- Re-running with identical inputs must be near-instant. `--no-cache` bypasses entirely. Cache read/write failures must never abort a run.

---

## 12. Error Handling, Logging & Exit Codes

- All progress goes to **stderr**; `--json` output goes to **stdout**.
- Logging helpers live in `core/log.py`: stage headers, info, success, warning, error, plus a verbose-only detail level.
- ffmpeg/ffprobe failures surface the last lines of stderr in the error message.
- Heavy dependencies (`openai`, `scenedetect`) are **lazy-imported** inside the functions that need them, so `extract --help` stays fast and a missing optional dep fails with a clear message only when that path runs.
- **Exit codes:** `0` success · `1` runtime error · `2` configuration/usage error · `130` interrupted.

---

## 13. Module / File Layout

As in Section 6. Key responsibilities:

- `core/ffmpeg.py` — `ensure_ffmpeg()`, `probe()`, a command runner, audio extraction, keyframe extraction, clip cutting.
- `core/manifest.py` — Pydantic models for the manifest + load/save with validation.
- `core/cache.py` — namespaced content-hashed JSON cache.
- `core/log.py` — console output helpers.
- `extract/segment.py` — the four strategies; returns candidate ranges.
- `extract/audio.py` — loudness curve extraction + peak detection.
- `extract/dedupe.py` — IoU computation + iterative merge.
- `extract/profiles.py` — built-in `gta` profile + loader for custom profile files.
- `extract/analyze.py` — backend interface + dispatch + scoring blend.
- `extract/backends/gemma.py` — frame-sampling, OpenAI-compatible call, JSON parsing.
- `extract/pipeline.py` — orchestrates ingest → segment → dedupe → analyze → catalog.
- `extract/cli.py` — argparse, builds config, invokes pipeline, exit codes.

---

## 14. Dependencies

- **System:** `ffmpeg` and `ffprobe` (also cover audio-energy analysis and keyframe extraction — no extra system dependency).
- **Python (>= 3.10):** `pydantic`, `openai`, `scenedetect`, `python-dotenv`.
- **Dev:** `pytest`.
- Package with `pyproject.toml`; expose a console script `extract = supaclip.extract.cli:main`.

---

## 15. Implementation Notes & Constraints

- Single-process, synchronous. No async, no workers.
- `ffmpeg` is always invoked as a subprocess (no Python ffmpeg bindings).
- The Gemma backend feeds the model **sampled frames as images** over the OpenAI-compatible chat API. If a future backend supports native video input, that is handled inside its own backend module — the interface does not change.
- The analyzer prompt must request JSON-only output and the response must be parsed defensively (tolerate code fences and surrounding prose); retry once before failing.
- Keep a `PROMPT_VERSION` constant in `analyze.py`; include it in the analysis cache key so prompt changes invalidate stale entries.
- Do not hardcode GTA specifics outside the `gta` game profile.

---

## 16. Acceptance Criteria

- **AC-1.** With a running Ollama + `gemma4` and a local gameplay video, `extract session.mp4` produces ≥ 1 `clip_NN.mp4` master file and a `manifest.json` that validates against the Section 8.1 schema.
- **AC-2.** All output clips retain the source's native resolution and aspect ratio (no scaling/cropping).
- **AC-3.** `extract session.mp4 --segmenter manual --timestamps cuts.csv` produces exactly one clip per timestamp row, cut at those ranges.
- **AC-4.** Candidate segments overlapping beyond `--dedup-iou` are merged; with `--no-dedup` they are not.
- **AC-5.** Every clip in the manifest carries a non-empty `description`, a `categories` list drawn from `taxonomy`, a `score` in 0–100, and a `game_signals` object matching the active profile's fields.
- **AC-6.** Re-running the same command reuses the cache and completes without repeating analysis calls; `--no-cache` forces recomputation.
- **AC-7.** A video with no audio track still completes (audio-energy pre-pass skipped, no crash).
- **AC-8.** Missing `ffmpeg`, an unreachable analyzer endpoint, or a missing input file each produce a clear, actionable error and the correct non-zero exit code.
- **AC-9.** `extract --help` runs instantly (heavy imports are deferred).

---

## 17. Testing Requirements

- **Unit tests** (`tests/`, pytest), no video/model/network required, covering:
  - temporal IoU computation and the iterative dedupe merge;
  - timestamp parsing (`SS`, `MM:SS`, `HH:MM:SS`) and formatting;
  - manifest model (de)serialization and schema validation;
  - game-profile loading (built-in and from file);
  - audio peak detection on a synthetic loudness curve;
  - the score blend formula;
  - cache get/set/keying behavior.
- **Integration smoke test:** generate a short synthetic video with `ffmpeg` (e.g. `testsrc` + `sine`), run probe / clip-cut / keyframe extraction against it to confirm the ffmpeg paths work. The analyzer backend is **mocked** for this test.

---

## 18. Suggested Build Order

1. `core/log.py`, `core/cache.py`, `core/ffmpeg.py` — foundation + the synthetic-video smoke test.
2. `core/manifest.py` — Pydantic schema + round-trip tests.
3. `extract/profiles.py` — game-profile model, built-in `gta`, loader.
4. `extract/audio.py` — loudness curve + peak detection (unit-test on synthetic data).
5. `extract/segment.py` — the four strategies (use audio peaks in `auto`).
6. `extract/dedupe.py` — IoU + merge (unit-tested).
7. `extract/analyze.py` + `extract/backends/gemma.py`.
8. `extract/pipeline.py` — wire the five stages.
9. `extract/cli.py` — argparse, config, exit codes.
10. `pyproject.toml`, `README.md`, `.env.example`; finalize tests; verify all acceptance criteria.

---

## 19. Assumptions & Open Items

- **A-1.** The Gemma backend sends sampled frames as images over the OpenAI-compatible API. If the target Gemma deployment accepts native video, that can be added inside `backends/gemma.py` without changing the analyzer interface.
- **A-2.** Frame sampling rates (whole-video boundary pass vs. per-segment analysis) start at sensible defaults and may be tuned during implementation; expose them as constants.
- **A-3.** Semantic dedupe (FR-3.5) is optional; ship temporal dedupe first.
- **A-4.** Phase 2 ("Stitch": Claude-directed assembly via an MCP server and an EDL) consumes this manifest but is not part of this PRD.