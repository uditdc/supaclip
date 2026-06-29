# 0001 — Movie-recap capability: layering and roadmap

- **Status:** accepted
- **Date:** 2026-06-29
- **Context:** turning a full-length film into a chronological *series* of
  ~60s narrated recap shorts ("movie recap" / "explained in N parts" genre).

## Decision

Build the movie-recap capability as **layers on the existing supaclip engine,
not as a fork or a separate program (yet)**:

1. **Core primitives** that are generally useful go into supaclip itself
   (`extract` / `catalog` / `stitch`). These are domain-agnostic and benefit
   every profile, not just movies.
2. **Narrative/product logic** stays a thin application layer expressed through
   supaclip's existing extension seams — a Claude Code **skill** for
   orchestration and a **profile** for domain vocabulary. The `movie-recap`
   skill and the `movie` profile already live here; no engine code is
   duplicated.
3. **Separate software** is deferred until the recap grows a genuine product
   surface (UI/hosted service, accounts, billing, asset library, publishing
   integrations, or a divergent license/release cadence). At that point the
   product should **depend on supaclip as a library**, not fork it.

## Where each piece lives

| Capability | Altitude | Home |
|---|---|---|
| Subtitle/dialogue ingest → per-scene dialogue → FTS | primitive | core (`extract`/`catalog`) |
| `timeline` (chronological) catalog ordering | primitive | core (`catalog/search.py`) |
| Generic source-level summary storage + MCP read tool | primitive | core (planned) |
| Synopsis **input** priming (`--video-intro`/`--context-file`) | primitive | core (exists) |
| Beat-sheet shape / act structure | product | app layer |
| Narrative scoring *policy* ("story-meaningful" > "loud") | product | app layer / optional plugin |
| N-part chaptering + full-arc narration | product | app layer (`movie-recap` skill) |

## Roadmap (phased; each phase independently useful)

1. **Subtitle ingest + per-scene dialogue + FTS** (schema v2). — *done*
2. **Source synopsis/theme/beat-sheet rollup + storage + MCP tool.**
   Decision: produced *both* ways — the user may prime analysis with a
   synopsis/cast up front (existing `VideoContext`), and an LLM rollup over the
   ordered dialogue+descriptions generates the stored beat-sheet.
3. **Narrative scoring + beat-aware selection** (story-meaningful clip choice).
4. **`movie-recap` skill v2** — map parts onto the beat-sheet, coverage- and
   continuity-checked.

## Consequences

- supaclip stays a clean, general engine; the recap product is one consumer.
- Phase-1 work (dialogue in the catalog) raises quality for *all* profiles
  (podcast, tutorial, sports commentary), not just movies.
- No premature second codebase to maintain; the skill+profile seam already
  delivers "software inheriting supaclip's features."

## Locked sub-decisions

- **Dialogue source:** sidecar SRT/VTT + embedded subtitle stream; **no ASR**
  (see [0002](0002-subtitle-dialogue-ingestion.md)).
- **Synopsis/theme:** both user-primed and auto-generated (Phase 2).
