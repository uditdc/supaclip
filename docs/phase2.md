# supaclip — Phase 2 "Stitch" Plan

## Context

Phase 1 (`extract`) is shipped and working: it produces native-aspect master clips plus a `manifest.json` per source video, and a sqlite catalog + MCP server already lets Claude query across every clip ever extracted (search by description, categories, score, game signals, audio cues). What's missing is everything **downstream** of that catalog — the part the PRD originally scoped as "Phase 2: Claude as director."

Goal of this plan: ship the **Stitch** half — a CLI + MCP surface that lets Claude, given a user-supplied short-video script (voiceover text, b-roll cues, on-screen text, voice profile), browse the catalog, pick clips, and render a finished 1080×1920@60fps YouTube-Short/TikTok-style mp4. The motivating example is the "Rockstar Spent 12 Years On This ONE Detail" GTA 6 hair-physics short: 38 s, ElevenLabs narration with SSML breaks, 5 b-roll segments, 5 OST overlays.

### Locked scope decisions
- **EDL is the contract.** Claude composes a typed Pydantic JSON Edit Decision List; `stitch render edl.json` renders deterministically. No Claude calls inside the renderer.
- **MVP effects:** 9:16 reframe (center-crop with optional per-clip offset), clip concat trimmed to cue timings, ElevenLabs voiceover mix, OST via ffmpeg `drawtext` with style presets. **Out of scope for MVP:** Ken-Burns zoom, freeze frames, red-circle highlights, slow-mo, split-screen, animated text pops. These become Phase 2.5.
- **Pluggable TTS** mirroring the analyzer backend pattern. ElevenLabs default; cached by `(text + voice + settings)` hash.
- **Script is user-supplied.** Claude's job is parse → search catalog → compose EDL. No script generation in MVP.

---

## Architecture

```
script.md ──▶ Claude (via MCP)
              │  catalog_search, get_clip_preview, validate_edl, render_edl
              ▼
            edl.json ──▶ stitch render ──▶ short.mp4
                          │
                          ├─ tts (ElevenLabs) ──▶ voice.wav  (cached)
                          ├─ per-cue: cut + reframe to 9:16
                          ├─ concat + mix audio + drawtext overlays
                          └─ encode 1080x1920@60fps h264/aac
```

Two surfaces:
1. **MCP tools** (Claude uses these): existing `catalog_search` + new `get_clip_preview`, `validate_edl`, `render_edl`.
2. **CLI** (`clipper stitch …`): `render`, `validate`, `voice-preview`, `voices` (list ElevenLabs voices).

---

## New & modified files

### New — EDL contract (shared core)
- `clipper/core/edl.py` — Pydantic models. Single source of truth for the JSON Claude emits.

```python
class EDLOutput(BaseModel):
    width: int = 1080
    height: int = 1920
    fps: int = 60
    duration: float                       # total seconds

class EDLVoiceover(BaseModel):
    backend: Literal["elevenlabs"] = "elevenlabs"
    voice_id: str
    settings: dict[str, float]            # stability, similarity_boost, style
    script: str                           # may contain SSML <break time="..."/>

class EDLVideoCue(BaseModel):
    start: float                          # timeline seconds
    end: float
    clip_id: str                          # catalog id, e.g. "clip_007"
    source_in: float | None = None        # default: clip.source_in
    reframe: Literal["crop_center", "crop_left", "crop_right", "letterbox"] = "crop_center"

class EDLAudioCue(BaseModel):
    start: float
    end: float
    kind: Literal["voiceover", "clip_audio", "silence"]
    level_db: float | None = None         # default: 0 for voiceover, -18 for clip_audio
    duck: bool = False                    # duck this track under voiceover

class EDLOSTCue(BaseModel):
    start: float
    end: float
    text: str
    style: Literal[
        "bold_yellow", "red_strike", "neon_pink",
        "white_pop", "comment_trap"
    ] = "white_pop"

class EDL(BaseModel):
    schema_version: int = 1
    title: str
    output: EDLOutput
    voiceover: EDLVoiceover | None = None
    video: list[EDLVideoCue]
    audio: list[EDLAudioCue]
    ost: list[EDLOSTCue]
```

Plus `load_edl()` / `save_edl()` + `validate_edl(edl, catalog) -> list[ValidationIssue]` (checks: cues cover timeline, no gaps/overlaps in video track, referenced `clip_id`s resolve in the catalog, cue durations fit clip durations, voiceover present iff audio references it).

### New — Stitch package `clipper/stitch/`
- `__init__.py`
- `cli.py` — argparse subcommands:
  - `stitch render EDL [-o OUTPUT] [--no-cache] [-v] [--json]`
  - `stitch validate EDL` — exits 0/2 with issues to stderr
  - `stitch voice-preview --text "…" --voice-id … [-o preview.wav]`
  - `stitch voices` — list available ElevenLabs voices
- `render.py` — orchestrator. Stages: load+validate EDL → resolve clip paths (via catalog) → TTS (if voiceover) → cut+reframe each video cue → assemble filter graph → ffmpeg → write `<output>.mp4` + sidecar `<output>.edl.json`.
- `reframe.py` — `build_reframe_filter(src_w, src_h, dst_w=1080, dst_h=1920, mode)` returning an ffmpeg filter chain string (`scale=…,crop=…` for crop modes; `scale=…,pad=…` for letterbox).
- `overlay.py` — `STYLE_PRESETS` dict mapping OST style names → ffmpeg `drawtext` params (font, size, color, box, shadow, position). Returns drawtext expressions with `enable='between(t,start,end)'`.
- `assembly.py` — builds the full `filter_complex` graph: per-cue inputs → reframed video segments → `concat` filter → drawtext chain for OST → audio mix (`amix` with `duck` via sidechain compress if voiceover present). Returns the ffmpeg command list.
- `tts/__init__.py`
- `tts/base.py` — `class TTSBackend: def synthesize(text, voice_id, settings, out_path) -> Path` + `list_voices() -> list[Voice]`.
- `tts/elevenlabs.py` — HTTP client (no SDK dep needed — direct `requests`/`urllib` to `api.elevenlabs.io`). Streams MP3, writes wav via ffmpeg. Handles SSML breaks. API key from `ELEVENLABS_API_KEY` env or `--api-key`.
- `tts/cache.py` — thin wrapper over `core.cache.Cache` namespacing TTS outputs by `sha1(text + voice_id + sorted(settings) + backend)`.

### Modified — ffmpeg helpers
- `clipper/core/ffmpeg.py` — extend with:
  - `cut_subrange(src, in_s, out_s, out_path)` — re-encode a slice (already exists as `cut_clip`; rename internal if needed but keep API stable).
  - `run_ffmpeg(args, log)` — wrap subprocess for filter-graph commands; surface last 30 lines of stderr on failure (consistent with existing pattern).
  - `concat_demux(inputs: list[Path], out_path)` — for the simple case of pre-prepared MP4 segments (used as a fallback).
  - The main render uses a single `filter_complex` pipeline, not concat-demux, to handle drawtext + audio mix in one pass.

### Modified — Top-level CLI
- `clipper/cli.py` — add `stitch` to the subcommand dispatcher alongside `extract`, `catalog`, `mcp`.
- `pyproject.toml` — add console script `stitch = clipper.stitch.cli:main`; no new hard deps (uses stdlib `urllib` for ElevenLabs HTTP).

### Modified — MCP server
- `clipper/catalog/mcp.py` — add three tools:
  - `get_clip_preview(clip_id: str)` → `{description, categories, duration, score, keyframe_paths, source_file, source_in, source_out}`. Thin wrapper over existing `search.py` row resolution.
  - `validate_edl(edl: dict)` → `{ok: bool, issues: [{severity, message, path}]}`. Calls `core.edl.validate_edl()`.
  - `render_edl(edl: dict | path, output_path: str)` → `{status, output, sidecar, log_excerpt}`. Spawns the stitch render in-process, streams a summary back. Optional MCP tool — gated on `--allow-render` server flag so a default MCP install doesn't burn TTS credits unsolicited.

### Tests — `tests/test_stitch.py`
- EDL Pydantic round-trip + validation (gaps, overlaps, missing clip refs).
- `reframe.build_reframe_filter` snapshots for each mode at 16:9 → 9:16 and 4:3 → 9:16.
- `overlay.STYLE_PRESETS` produces valid drawtext expressions.
- `assembly.build_command` snapshot for a 3-cue + 2-OST + voiceover EDL.
- TTS cache key stability (text/settings change ⇒ new key; same input ⇒ hit).
- ElevenLabs backend mocked at HTTP layer (no network).
- Integration smoke: synthetic `testsrc`+`sine` clips, EDL referencing them, real ffmpeg render, output probed for resolution=1080x1920, fps=60, duration matches EDL ±100 ms.

### Docs
- `docs/stitch.md` — EDL schema, CLI usage, an end-to-end walkthrough of the GTA 6 hair-physics example (from `script.md` → MCP session → `edl.json` → rendered short).
- Update `README.md` — short Phase 2 section pointing at `docs/stitch.md`.

---

## Reusable foundations (do NOT re-implement)

| Need | Reuse |
|---|---|
| Logger (stage/info/success/warn/error) | `clipper/core/log.py:Logger` |
| Content-hashed JSON cache | `clipper/core/cache.py:Cache` + `fingerprint_file` |
| ffprobe / cut_clip | `clipper/core/ffmpeg.py:probe`, `cut_clip` |
| Manifest models | `clipper/core/manifest.py` (Stitch reads, never writes) |
| Catalog lookup by `clip_id` | `clipper/catalog/search.py:_row_to_clip` resolves relative→absolute paths already |
| argparse + env fallback patterns | `clipper/extract/cli.py:_env`, error/exit-code wrapper |
| Backend interface pattern | `clipper/extract/backends/` (mirror for `clipper/stitch/tts/`) |

---

## Implementation order

1. `core/edl.py` — models + validation, with unit tests. Lock the contract first.
2. `stitch/reframe.py` + `stitch/overlay.py` — pure filter-string builders, unit-tested in isolation.
3. `stitch/tts/{base,elevenlabs,cache}.py` — TTS, mocked test, then a manual smoke against ElevenLabs.
4. `stitch/assembly.py` + `stitch/render.py` — wire the filter graph; integration test against synthetic clips.
5. `stitch/cli.py` + top-level `clipper/cli.py` wiring + `pyproject.toml` console script.
6. `catalog/mcp.py` extensions (`get_clip_preview`, `validate_edl`, `render_edl`).
7. `docs/stitch.md` walkthrough; update README.
8. End-to-end dry-run with the user's GTA 6 hair-physics script against existing catalog clips.

---

## Verification

Run **all** of the following before declaring done:

1. `pytest` — unit + integration suites green, including the synthetic-clip render integration test.
2. `clipper stitch validate examples/edl-gta6-hair.json` — exits 0 on a hand-authored EDL of the example script.
3. `clipper stitch voice-preview --text "Twelve years." --voice-id <id> -o /tmp/preview.wav` — produces an audible wav; second run hits cache (no HTTP).
4. `clipper stitch render examples/edl-gta6-hair.json -o /tmp/short.mp4` — produces an mp4 where `ffprobe` reports `1080x1920`, `60 fps`, duration within ±100 ms of EDL `output.duration`, both video + audio streams present.
5. MCP smoke: start `clipper-mcp`, from Claude call `catalog_search` → `get_clip_preview` → `validate_edl` → `render_edl` end-to-end against the example script. Confirm the rendered file plays in VLC and that on-screen text appears at the expected timestamps.
6. Re-run step 4 with `--no-cache`: TTS regenerates, output is byte-similar (modulo encoder nondeterminism — verify by re-probing dimensions/duration, not hash).
7. Negative cases: EDL with a gap → `validate` reports it and `render` refuses; EDL with `clip_id` not in catalog → `validate` flags it; ElevenLabs key missing → clear error with exit code 2.

---

## Open items / Phase 2.5 (not in this plan)

- Smart reframe (face/motion-tracked crop window) — MVP uses fixed center-crop with optional offset.
- Ken-Burns zoom, freeze frame, red-circle highlight, slow-mo, split-screen, animated text pops.
- Auto script generation from a topic (`stitch script --topic …`).
- B-roll image/text-overlay assets sourced from outside the catalog (e.g. an "infographic" still).
- Music bed track with auto-ducking under voiceover.
- Renderer parallelism (per-cue prepare in parallel before final filter_complex).
