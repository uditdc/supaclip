# 0003 — Source-level synopsis / theme / beat-sheet (Phase 2)

- **Status:** accepted (implemented)
- **Date:** 2026-06-29
- **Relates to:** [0001](0001-movie-recap-layering.md) roadmap Phase 2;
  builds on [0002](0002-subtitle-dialogue-ingestion.md).

## Context

A coherent whole-film recap needs a model of the *whole story*, not just a
per-scene list. The catalog had per-scene descriptions (and, after Phase 1,
dialogue) but no source-level synopsis, theme, character roster, or act
structure to anchor a multi-part narration and guarantee arc coverage.

## Decision

Generate and store a **`SourceSummary`** — the "story spine" — at extract time.

- **Produced both ways** (the locked decision): a user may prime the pass with
  an authoritative synopsis + cast via the existing `VideoContext`
  (`--video-intro` / `--context-file`); an LLM rollup then fills in and
  structures the rest. With no user input the rollup stands alone.
- **Rollup pass** (`extract/summarize.py: summarize_source`): one text-only LLM
  call over the final scenes in story order, **each with its dialogue**,
  returning `{synopsis, themes, tone, characters[{name, role}],
  beats[{title, start, end, summary}]}`. Beats are coerced (clamped to
  duration, inverted/empty dropped, sorted). Best-effort: failure → `None`,
  manifest simply has no summary. Cached; `--no-summary` skips it.
- **Shared transport.** The provider call used by both aggregate and summarize
  was factored into `extract/llm.py` (`LLMConfig` + `call_json`).
  `aggregate.AggregateConfig` is now an alias of `LLMConfig` and `aggregate._call`
  a thin delegate, preserving the surface the tests patch.
- **Storage.** Manifest gains an optional top-level `summary` (manifest
  `SCHEMA_VERSION` 2→3). The catalog gains a `source_summaries` table keyed by
  `source_id`, upserted on ingest (catalog `SCHEMA_VERSION` 2→3; the table is
  created by the idempotent DDL, so the v2→v3 step needs no data migration).
  Read via `search.get_source_summary` and the `catalog_get_summary` MCP tool.

## Consumption

The `movie-recap` skill now loads the spine first (`catalog_get_summary`) and
**chapters parts onto the `beats`** rather than uniform clock-time slices —
guaranteeing full-arc coverage and natural act breaks — and names characters
from the spine's `characters` list for consistency across parts. It degrades to
inferring structure from the ordered scenes when no summary exists.

## Alternatives considered

- **Skill-side generation** (Claude writes the synopsis live from timeline
  scenes). Rejected as the default: a stored, cached, deterministic summary is
  reusable and works headless. The skill can still override/augment it.
- **Per-extract vs per-source storage.** Chose per-source (`source_id` PK,
  last ingest wins) since the spine describes the movie, and that's the key the
  recap skill queries by.

## Consequences

- The recap is anchored in a faithful, dialogue-grounded story model.
- One extra LLM call per extract (cached, opt-out) — acceptable; extract
  already makes LLM calls.
- Files: `extract/{llm,summarize}.py` (new), `extract/aggregate.py` (refactor),
  `core/manifest.py`, `extract/{pipeline,cli}.py`,
  `catalog/{schema,ingest,search,mcp}.py`; tests in `tests/test_summarize.py`
  and `tests/test_catalog.py`.

## Next (Phase 3+)

Narrative scoring (story-meaningful > loud) for beat-aware *clip selection*,
and optionally priming the per-scene VLM analysis with dialogue (deferred from
Phase 1).
