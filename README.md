# supaclip

**Turn long gameplay recordings into short-form videos — with Claude as the editor.**

[![CI](https://github.com/uditdc/supaclip/actions/workflows/ci.yml/badge.svg)](https://github.com/uditdc/supaclip/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Code style: Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

supaclip is a local-first pipeline that ingests raw gameplay footage, uses a
vision-language model to understand what's in every moment, and lets an LLM
("Claude as director") assemble polished vertical shorts — voiceover, captions,
effects, and music included.

```
 long_session.mp4
        │
        ▼
   ┌─────────┐   native-aspect clips +     ┌──────────┐   searchable
   │ extract │ ─ Claude-readable manifest ─▶│ catalog  │ ─ SQLite library
   └─────────┘                              └──────────┘        │
                                                                │ Claude browses it
                                                                ▼
                                                          ┌──────────┐
                                                          │  stitch  │ ─▶ short.mp4
                                                          └──────────┘   (1080×1920 … 4K)
```

1. **`extract`** segments a video and analyzes each segment with a VLM, emitting
   master clips and a `manifest.json` describing them.
2. **`catalog`** folds every manifest into one searchable SQLite database.
3. **`stitch`** renders a finished short from an EDL (Edit Decision List) that
   Claude composes by browsing the catalog.

---

## Features

- **Local-first.** Default analyzer is a local Ollama model; ffmpeg does all the
  video work. Hosted models (OpenRouter, Google AI Studio) are opt-in.
- **Pluggable VLM backends** — sampled-frame or native-video analysis over any
  OpenAI-compatible endpoint.
- **Full-text + structured catalog search** (FTS5) across descriptions, audio
  cues, tags, categories, and game-specific signals.
- **Short-form renderer** — 9:16 reframe, crossfades, Ken-Burns, freeze frames,
  slow-mo, styled on-screen text, circle/box/arrow annotations, and a
  ducked music bed.
- **Voiceover + speech-synced captions** via ElevenLabs or Google (Gemini) TTS,
  with on-disk caching so re-renders are free.
- **Resolution & hardware encoding** — export 720p → 4K, with auto-detected
  NVENC / VideoToolbox / QSV GPU encoders and a software fallback.
- **Claude-native** — an MCP server exposes the catalog and renderer to Claude;
  a Claude Code skill drives the whole script → render flow hands-free.

## Requirements

- Python ≥ 3.10
- `ffmpeg` and `ffprobe` on `PATH` (`sudo apt install ffmpeg` / `brew install ffmpeg`)
- An analyzer model — local [Ollama](https://ollama.com) by default, or any
  hosted OpenAI-compatible endpoint
- (Optional) a TTS key for voiceover: `ELEVENLABS_API_KEY` or `GEMINI_API_KEY`

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # add ,mcp for the MCP server; ,align for forced alignment
# or: make install
```

This installs four entry points: `extract`, `stitch`, `supaclip` (an umbrella
dispatcher — `supaclip extract …` ≡ `extract …`), and `supaclip-mcp`.

## Quickstart

```bash
# 1. Configure (see .env.example) — local Ollama works out of the box
cp .env.example .env

# 2. Extract clips + manifest from a recording
extract session.mp4

# 3. Add the manifest to your global catalog
supaclip catalog add clips/manifest.json

# 4. Find clips
supaclip catalog search "police chase" --min-score 70

# 5. Render a short from an example EDL
stitch render examples/edl-gta6-hair.json -o short.mp4
```

> The example EDLs reference `clip_id`s from your own catalog — see
> [`examples/README.md`](examples/README.md) for how to adapt them.

---

## Stages

### `extract` — footage → clips + manifest

```bash
extract session.mp4                                   # auto-segment + analyze
extract session.mp4 --segmenter manual --timestamps cuts.csv
extract session.mp4 --segmenter scene                 # scene detection
extract session.mp4 --segmenter interval --interval 30
extract session.mp4 --json                            # manifest to stdout
```

`cuts.csv` is one `start,end` pair per line (`SS`, `MM:SS`, or `HH:MM:SS`):

```
0:30, 1:05
2:00, 2:45
```

### `catalog` — one searchable library

Each `extract` run produces a per-source `manifest.json`; the catalog folds them
into a single SQLite database you can query across every clip you've extracted.

```bash
supaclip catalog add clips/manifest.json              # ingest (file or directory)
supaclip catalog search "police chase"                # FTS5 free-text
supaclip catalog search --category shootout --min-score 70
supaclip catalog search --category police_chase --category crash --all-categories
supaclip catalog search --signal wanted_level=4
supaclip catalog search --signal "vehicles~=police"
supaclip catalog search --min-duration 20 --order-by duration --json

supaclip catalog list --sources
supaclip catalog stats
supaclip catalog remove clips/manifest.json
```

Catalog location: `~/.local/share/supaclip/catalog.db` (override with
`--catalog FILE` or `SUPACLIP_CATALOG`).

### `stitch` — EDL → finished short

Render vertical shorts (default 1080×1920 @ 60 fps) from an EDL. See
[`docs/stitch.md`](docs/stitch.md) for the full schema and a walkthrough.

```bash
stitch validate examples/edl-gta6-hair.json
stitch render   examples/edl-gta6-hair.json -o short.mp4

# Higher-res / GPU-accelerated export
stitch render   examples/edl-gta6-hair.json --resolution 4k --encoder auto -o short.mp4
stitch encoders                                       # list usable video encoders
```

`--resolution {720p,1080p,1440p,2160p,4k}` scales the whole composition by its
short side; `--encoder auto` picks a working GPU encoder (NVENC / VideoToolbox /
QSV) and falls back to `libx264`. Full details in
[`docs/stitch.md`](docs/stitch.md#resolution--encoding).

**TTS:** ElevenLabs is the default backend (`ELEVENLABS_API_KEY`). Google
(Gemini) is available via `"backend": "google"` on the EDL voiceover with
`GEMINI_API_KEY` — pick a voice with `stitch voices --backend google`. Gemini
returns audio only, so speech-synced captions there use **local forced
alignment** (`pip install 'supaclip[align]'`; the MMS_FA model downloads once,
then caches). ElevenLabs returns word timestamps natively.

## Claude integration

### MCP server

Expose the catalog and renderer to Claude so it can browse your library and
render directly.

```bash
pip install -e ".[mcp]"

# Register with the ABSOLUTE path to the venv binary — Claude Code spawns
# subprocesses without activating any venv.
claude mcp add supaclip "$(pwd)/.venv/bin/supaclip-mcp"

# With a non-default catalog and a TTS key for render_edl:
claude mcp add supaclip \
  --env SUPACLIP_CATALOG=/path/to/catalog.db \
  --env ELEVENLABS_API_KEY=sk_... \
  -- "$(pwd)/.venv/bin/supaclip-mcp"
```

The server loads a `.env` in the working directory on startup, so
`SUPACLIP_CATALOG` and the LLM/TTS keys can live there instead of `--env`.

Tools exposed: `catalog_search` (incl. `order_by="timeline"` for story order),
`catalog_get_clip`, `catalog_get_source`, `catalog_list_sources`,
`catalog_stats`, `catalog_get_summary` (synopsis/themes/characters/beats),
`get_clip_preview`, `probe_clip` (decode-clean + peak, for clean-clip selection
and audio gain), `get_clip_subtitles` (the film's own subtitles, clip-local),
`validate_edl`, and `render_edl`. `render_edl` may spend TTS credits, but
outputs are cached by `(text + voice + settings)` so re-renders are free.

> Prefer registering with `claude mcp add` (above) over a committed
> `.mcp.json` — the absolute venv path is more robust than a relative one, and
> it keeps per-machine config out of the repo.

### Claude Code skill

Persistent skills auto-load by intent and drive the MCP tools end-to-end
without re-prompting:

- [`stitch-director`](.claude/skills/stitch-director/SKILL.md) — a single short
  from a script you supply ("short", "stitch", "EDL", "b-roll").
- [`movie-recap`](.claude/skills/movie-recap/SKILL.md) — a whole film → a
  chronological series of narrated recap shorts.
- [`movie-clips`](.claude/skills/movie-clips/SKILL.md) — standalone highlight
  clips, one per beat, with the film's audio + subtitles (or `--commentary`).

For a one-shot paste-in version of the stitch flow, use
[`docs/claude-prompt.md`](docs/claude-prompt.md).

## Configuration

Put a `.env` in the working directory (see [`.env.example`](.env.example)); CLI
flags override env vars.

| Variable | Purpose |
|---|---|
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | analyzer endpoint (or `OPENAI_*`) |
| `ELEVENLABS_API_KEY` | ElevenLabs TTS (default stitch voiceover) |
| `GEMINI_API_KEY` | Google AI Studio TTS / native-video analyzer |
| `SUPACLIP_CATALOG` | catalog DB path override |

## Documentation

| Doc | Contents |
|---|---|
| [`docs/stitch.md`](docs/stitch.md) | EDL schema, CLI, resolution & encoding, walkthrough |
| [`docs/architecture.md`](docs/architecture.md) | module layout and data flow |
| [`docs/prd.md`](docs/prd.md) | product requirements (Extract) |
| [`docs/phase2.md`](docs/phase2.md), [`docs/phase2.5.md`](docs/phase2.5.md) | Stitch design notes |
| [`docs/claude-prompt.md`](docs/claude-prompt.md) | paste-in director prompt |

## Development

```bash
make test          # pytest
make build         # sdist + wheel into dist/
make dist-check    # twine check dist/*
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for dev setup, code style, and the PR
process, and [`CHANGELOG.md`](CHANGELOG.md) for release history.

## License

[MIT](LICENSE) © Udit
