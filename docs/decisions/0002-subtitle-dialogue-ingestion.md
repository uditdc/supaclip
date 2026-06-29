# 0002 — Subtitle ingestion & per-scene dialogue (Phase 1)

- **Status:** accepted (implemented)
- **Date:** 2026-06-29
- **Relates to:** [0001](0001-movie-recap-layering.md) roadmap Phase 1.

## Context

The analyzer was visual-only: the default `frames` backend sends a sprite grid
of sampled frames to a vision model, so the catalog described what a scene
*looked like*, never what was *said*. A film's plot lives in its dialogue, so
recaps built from descriptions alone were impressionistic and prone to invented
detail. The catalog had no notion of spoken text.

## Decision

Ingest dialogue from subtitles and store it per scene; index it for search.

- **Sources, in cost order** (`extract/subtitles.py: load_for_video`): explicit
  `--subtitles PATH` → sidecar `<stem>.srt`/`.vtt` (incl. language-suffixed) →
  embedded text subtitle stream (`core/ffmpeg.py: extract_subtitle_text`, via
  `ffmpeg -map 0:s:0 -f webvtt -`).
- **No speech-to-text.** ASR (Whisper) was explicitly deferred to keep zero new
  dependencies and instant, deterministic ingestion. With no subtitles the
  stage no-ops and descriptions stay vision-only; `--no-subtitles` forces that.
- **Parser** tolerates both SRT and WebVTT (comma/dot millisecond separators,
  2- or 3-field timecodes, `WEBVTT`/`NOTE` headers, cue settings, `<tag>`/`{tag}`
  stripping).
- **Alignment:** a clip's `dialogue` is every cue overlapping its
  `[source_in, source_out)` window, concatenated (`dialogue_for_range`).
- **Storage:** `Clip.dialogue` (manifest `SCHEMA_VERSION` 1→2); `clips.dialogue`
  column and a new `dialogue` column in the `clips_fts` index (catalog
  `SCHEMA_VERSION` 1→2). Free-text `catalog_search` now matches spoken lines,
  and `get_clip_preview` returns `dialogue`.

## Migration

FTS5 cannot add a column in place, so `_migrate_v1_to_v2` adds `clips.dialogue`
via `ALTER TABLE`, drops `clips_fts`, recreates it with the new column set, and
repopulates it from the persisted clip rows (description, dialogue, audio cues,
tags). Runs automatically on connect for any pre-v2 catalog. Covered by
`tests/test_catalog.py: test_migrate_v1_to_v2_adds_dialogue_and_rebuilds_fts`.

## Not done here (deliberately)

- **Priming the VLM with scene dialogue.** Passing a scene's lines into the
  analyzer prompt (so cheap local models produce grounded descriptions) is the
  natural next increment, but it touches the profile prompt templates and both
  backends and is harder to test deterministically — kept separate from this
  storage+search foundation.
- ASR fallback; source-level synopsis/theme/beats (Phase 2); narrative scoring
  (Phase 3).

## Consequences

- Every profile gains dialogue-aware search, not just `movie`.
- The `movie-recap` skill can pull a film in story order *with* its spoken plot.
- Files: `extract/subtitles.py` (new), `core/ffmpeg.py`, `core/manifest.py`,
  `extract/pipeline.py`, `extract/cli.py`, `catalog/{schema,ingest,search,mcp}.py`;
  tests in `tests/test_subtitles.py` and `tests/test_catalog.py`.
