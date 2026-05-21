# supaclip — Extract CLI

Phase 1 of `supaclip`. `extract` ingests a local gameplay video, segments it,
analyzes each segment with a vision-language model, and emits:

- native-aspect master `.mp4` clips, and
- a Claude-readable `manifest.json` catalog.

The manifest is the deliverable; Phase 2 ("Stitch") consumes it to assemble
short-form videos.

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
# or: make install
```

Requires `ffmpeg` and `ffprobe` on `PATH`.

Installs four entry points: `extract`, `stitch`, `supaclip`, and (with the
`[mcp]` extra) `supaclip-mcp`. `supaclip` is an umbrella dispatcher —
`supaclip extract …` and `extract …` are equivalent.

## Usage

```bash
# Auto-segment + analyze with local Ollama (default)
extract session.mp4

# Cut at user-supplied ranges
extract session.mp4 --segmenter manual --timestamps cuts.csv

# Scene-detect or fixed windows
extract session.mp4 --segmenter scene
extract session.mp4 --segmenter interval --interval 30

# Print the manifest to stdout
extract session.mp4 --json
```

`cuts.csv` is one `start,end` pair per line (`SS`, `MM:SS`, or `HH:MM:SS`):

```
0:30, 1:05
2:00, 2:45
```

## Configuration

Put a `.env` in the working directory (see `.env.example`). CLI flags override
env vars. Variables: `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`
(also accepts the `OPENAI_*` equivalents for the first two).

## Catalog

Each `extract` run produces a per-source `manifest.json`. The `catalog` adds
those manifests to a single global SQLite database so you can search across
every clip you've ever extracted.

```bash
# Ingest a manifest (or a directory; walks recursively)
supaclip catalog add clips/manifest.json

# FTS5 query over description / audio cues / tags
supaclip catalog search "police chase"

# Structured filters compose freely
supaclip catalog search --category shootout --min-score 70
supaclip catalog search --category police_chase --category crash --all-categories
supaclip catalog search --signal wanted_level=4
supaclip catalog search --signal "vehicles~=police"
supaclip catalog search --min-duration 20 --order-by duration --json

# Catalog admin
supaclip catalog list --sources
supaclip catalog stats
supaclip catalog remove clips/manifest.json
```

Catalog location: `~/.local/share/supaclip/catalog.db`. Override with
`--catalog FILE` or the `SUPACLIP_CATALOG` env var.

## MCP server

Expose the catalog to Claude so it can query your library directly.

```bash
pip install -e ".[mcp]"

# Register with the ABSOLUTE path to the venv binary — Claude Code spawns
# subprocesses without activating any venv, so `supaclip-mcp` on its own
# won't be on PATH. The script has the venv's python in its shebang.
claude mcp add supaclip "$(pwd)/.venv/bin/supaclip-mcp"
```

Pass env vars for non-default catalog or for the ElevenLabs key that
`render_edl` needs:

```bash
claude mcp add supaclip \
  --env SUPACLIP_CATALOG=/path/to/catalog.db \
  --env ELEVENLABS_API_KEY=sk_... \
  -- "$(pwd)/.venv/bin/supaclip-mcp"
```

Tools exposed: `catalog_search`, `catalog_get_clip`, `catalog_get_source`,
`catalog_list_sources`, `catalog_stats`, plus `get_clip_preview`,
`validate_edl`, and `render_edl` from Stitch. `render_edl` may spend
ElevenLabs credits, but TTS outputs are cached by `(text + voice + settings)`
so re-renders of the same script are free.

## Stitch — short-form video assembly

Render YouTube-Shorts/TikTok-style verticals (default 1080×1920 @ 60 fps)
from an EDL (Edit Decision List) that Claude composes by browsing the
catalog. See [`docs/stitch.md`](docs/stitch.md) for the schema and a
walkthrough.

```bash
stitch validate examples/edl-gta6-hair.json
stitch render   examples/edl-gta6-hair.json -o short.mp4
```

ElevenLabs is the default TTS backend (`ELEVENLABS_API_KEY` in `.env`),
mirroring the pluggable analyzer pattern from Extract.

For the one-shot **script → EDL → validate → render** flow inside Claude
Code, paste [`docs/claude-prompt.md`](docs/claude-prompt.md) into a session
that has the `supaclip` MCP server registered.

A persistent **Claude Code skill** at `.claude/skills/stitch-director.md`
auto-loads the workflow whenever you mention "short", "stitch", "EDL",
"b-roll", etc. — no need to re-paste the prompt every session.
[`docs/phase2.5.md`](docs/phase2.5.md) describes Phase 2.5 (effects,
transitions, annotations, music bed).

## Run tests

```bash
pytest
# or: make test
```

## Build & release

The CLI is packaged as a standard Python wheel + sdist via
[PEP 517](https://peps.python.org/pep-0517/). Common workflows:

```bash
make build         # sdist + wheel into dist/
make wheel         # wheel only
make sdist         # sdist only
make dist-check    # twine check on dist/*
make clean         # remove build artifacts and caches
```

Equivalent without Make:

```bash
python -m pip install --upgrade build twine
python -m build           # writes dist/supaclip-<version>.{tar.gz,whl}
python -m twine check dist/*
```

Publishing (requires `TWINE_USERNAME` / `TWINE_PASSWORD` or a `~/.pypirc`):

```bash
make publish-test  # upload to TestPyPI
make publish       # upload to PyPI
```

The wheel installs `extract`, `stitch`, `supaclip`, and `supaclip-mcp`
entry points; users only need `ffmpeg`/`ffprobe` on `PATH` to run it.
