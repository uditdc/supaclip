# supaclip — Phase 2.5 Plan

## Context

Phase 2 MVP shipped: `stitch` CLI + MCP tools (`catalog_search`,
`get_clip_preview`, `validate_edl`, `render_edl`), the EDL contract,
ElevenLabs TTS with caching, 9:16 reframe, 5 OST text presets,
synchronous ffmpeg render.

Two pain points in daily use:

1. **Every Claude Code session has to be re-prompted** with the
   `docs/claude-prompt.md` boilerplate. The MCP server advertises tool
   schemas but not the *workflow* (search → preview → compose → validate
   → render), the EDL invariants (contiguous video track, integer
   `clip_id`), or the OST style vocabulary.
2. **Effects are bare-bones.** Every clip is a hard cut at center-crop,
   audio is voiceover-only, no transitions, no annotations. The motivating
   example (GTA 6 hair short) explicitly wants freeze frame, slow-mo,
   red-circle highlight, split-screen, animated text — all of which we
   parked.

Goal of 2.5: close those two gaps. Land a Claude Code skill so the
workflow is one-prompt every time, and add the highest-leverage effects
from the out-of-scope list.

---

## Locked scope

| Bucket | In | Out (defer to 3.x) |
|---|---|---|
| Claude integration | **`stitch-director` skill** — auto-loaded, teaches the pipeline + EDL schema + recovery patterns | MCP server-side prompts / resources (those are MCP features, not Claude Code) |
| Effects | freeze-frame, Ken-Burns (zoom-in/out), slow-mo, **xfade transitions** between cues | split-screen, picture-in-picture, animated text pops |
| Annotations | circle / box / arrow drawn over a region for a time window | freeform path / brush, motion-tracked annotations |
| Audio | **music bed** track with sidechain ducking under voiceover; wire `duck: true` on existing `clip_audio` cues | SFX one-shots, multiple voiceover segments, voice cloning helper |
| Reframe | per-cue **pixel-offset** override (`reframe_offset`) | face/motion-tracked smart reframe |
| Iteration | `--print-ffmpeg`, `--preview-cue N`, render-progress streaming | interactive scrubber, partial-render resume |

Backwards compatibility: every new EDL field is optional with a sensible
default. Existing `edl.json` files keep working unchanged.

---

## Architecture additions

### EDL schema v1.1 (backwards compatible)

```python
class EDLVideoCue(BaseModel):
    # existing: start, end, clip_id, source_in, reframe
    reframe_offset: int = 0                # pixel offset from default crop center
    effect: Literal["none","freeze_first","ken_burns_in","ken_burns_out","slow_mo"] = "none"
    effect_params: dict[str, float] = Field(default_factory=dict)
    transition_in: Literal["cut", "crossfade"] = "cut"
    transition_duration: float = 0.0       # seconds; only honored when transition_in != "cut"

class EDLAnnotation(BaseModel):
    start: float
    end: float
    shape: Literal["circle", "box", "arrow"]
    x: int                                 # pixel center
    y: int
    radius: int = 0                        # circle
    width: int = 0                         # box / arrow length
    height: int = 0
    color: str = "#ff3b30"
    stroke_width: int = 8

class EDLMusic(BaseModel):
    file: str                              # absolute path or "catalog:<clip_id>"
    level_db: float = -22.0
    duck: bool = True                      # sidechain-compress under voiceover

class EDL(BaseModel):
    # existing fields...
    annotations: list[EDLAnnotation] = Field(default_factory=list)
    music: EDLMusic | None = None
```

`schema_version` stays at `1` — additions are purely additive.

### New modules

- `supaclip/stitch/effects.py` — per-cue filter builders for `freeze_first`,
  `ken_burns_in/out`, `slow_mo`. Returns a snippet that slots into the
  existing reframe → effect → label chain.
- `supaclip/stitch/transitions.py` — emits `xfade` filter pairs when adjacent
  cues request crossfade. Switches the video graph from `concat` to a
  staged `xfade` chain in that case.
- `supaclip/stitch/annotation.py` — `drawbox` + `geq` or generated PNG
  overlays for circles/arrows; emits ffmpeg filter strings with
  `enable='between(t,…)'`.
- `supaclip/stitch/music.py` — adds a music input, applies
  `sidechaincompress` against the voiceover when `duck=True`.
- `supaclip/stitch/progress.py` — parses ffmpeg `-progress pipe:1` output
  into `(out_time_ms, pct)` events, fed to a callback.

### Modified

- `supaclip/stitch/assembly.py` — incorporate the new builders; choose
  `concat` vs `xfade` based on whether any cue requests crossfade.
- `supaclip/stitch/render.py` — accept a progress callback; default
  callback writes a one-line progress bar via the Logger.
- `supaclip/catalog/mcp.py` — `render_edl` streams progress notifications.
- `supaclip/stitch/cli.py` — `--print-ffmpeg`, `--preview-cue N` flags.

---

## The Claude Code skill

Two files (same content, different paths) so the skill works both
project-locally and globally:

- `.claude/skills/stitch-director.md` (project — committed)
- `~/.claude/skills/stitch-director.md` (user-global — installed via a
  one-line `make install-skill` or copy)

Frontmatter:

```yaml
---
name: stitch-director
description: |
  Use when the user wants to make a YouTube Short / TikTok / Reel /
  vertical short-form video, mentions "stitch", "EDL", "render a short",
  "find b-roll", or pastes a script with voiceover + b-roll cues + OST.
  Drives the supaclip MCP tools end-to-end (catalog_search →
  get_clip_preview → validate_edl → render_edl) without asking the user
  for confirmation between steps.
---
```

Body (target < 150 lines, scannable):

1. **Pipeline checklist** — the 8-step recipe from `claude-prompt.md`,
   condensed.
2. **EDL invariants** — strict-contiguous video track, integer `clip_id`,
   OST style vocabulary, audio kinds.
3. **Tool reference** — one-liner per MCP tool: when to call, what to
   read from the response, common params.
4. **OST style mapping table** — freeform user text → preset name.
5. **Effect mapping** — "freeze frame" → `effect=freeze_first`,
   "slow-mo" → `effect=slow_mo, effect_params.speed=0.5`, etc.
6. **Recovery patterns** — `validate_edl` failure modes and exact fixes
   (duration overflow → shorten cue or pick later `source_in`; missing
   clip → re-search with broadened filters).
7. **One worked example** — the GTA 6 hair short condensed to 20 lines.

The skill body does **not** include the script template (users paste
that). It does include the rule "do not stop or ask between steps".

---

## Implementation order

1. **`.claude/skills/stitch-director.md`** — write first; high
   value-to-effort, immediately unblocks every future session.
2. **EDL schema v1.1** in `supaclip/core/edl.py` + validator updates
   (effect params sanity, annotation in-bounds, music file existence) +
   tests.
3. `supaclip/stitch/effects.py` + unit tests.
4. `supaclip/stitch/transitions.py` + unit tests (xfade snippet correctness).
5. `supaclip/stitch/annotation.py` + unit tests.
6. `supaclip/stitch/music.py` + unit tests (graph snapshot with/without
   ducking).
7. Refactor `supaclip/stitch/assembly.py` to call the new builders; choose
   `concat` vs `xfade` chain; integration test.
8. `supaclip/stitch/progress.py` + wire into `render.py` and `cli.py`.
9. `stitch render --print-ffmpeg` + `--preview-cue N` flags.
10. Update `docs/stitch.md`, `docs/claude-prompt.md`,
    `examples/edl-gta6-hair.json` (add a v1.1 variant exercising
    effects + annotations + music bed), README.

---

## Reusable foundations (do NOT re-implement)

| Need | Reuse |
|---|---|
| Logger | `supaclip/core/log.py:Logger` |
| Cache (music bed file resolution by catalog ref) | `supaclip/core/cache.py:Cache` |
| TTS cache | `supaclip/stitch/tts/cache.py:TTSCache` |
| ffmpeg run wrapper | `supaclip/core/ffmpeg.py:run_ffmpeg` |
| Catalog resolver | `supaclip/catalog/search.py:get_clip` |
| Filter pattern (reframe, overlay) | `supaclip/stitch/reframe.py`, `overlay.py` — mirror their pure-function style |
| Backend pattern (TTS) | `supaclip/stitch/tts/` — annotations could grow into a `supaclip/stitch/annotations/backends/` if we later want a PIL-based renderer |

---

## Verification

1. `pytest` — existing 47 tests still green; new unit tests for each
   effect/annotation/music builder; assembly snapshot test for an
   all-features EDL.
2. Skill loads: in a fresh Claude Code session in this repo, ask "make
   me a short about the GTA 6 trailer" — Claude should immediately call
   `catalog_stats` (no re-prompting), then chain through to a rendered
   mp4. Verify the skill banner appears in the session UI.
3. Skill works globally: copy to `~/.claude/skills/`, switch to an
   unrelated repo with the supaclip MCP registered, repeat the test.
4. `stitch render examples/edl-gta6-hair-v15.json -o /tmp/out.mp4` —
   produces an mp4 with a crossfade between cues 1→2, a 2 s freeze in
   cue 1, slow-mo on cue 4, a red circle over Lucia's hair at 12 s, and
   a music bed ducked −10 dB under voiceover.
5. `stitch render edl.json --print-ffmpeg` writes the ffmpeg invocation
   to stdout without executing it.
6. `stitch render edl.json --preview-cue 2 -o /tmp/cue2.mp4` renders
   only the second cue (with its effects but no transitions to
   neighbors) in < 5 s for a typical clip.
7. MCP `render_edl` call: progress notifications arrive at the Claude
   Code session at ≥ 1 Hz during a render.

---

## Done since Phase 2.5

- Hardware encoding (`--encoder`: nvenc / videotoolbox / qsv, auto-probed) and
  resolution scaling (`--resolution 720p…4k`). See `stitch/encode.py`.
- True circle annotation outline (PIL ring overlay, not a bounding box).
- `reframe_offset` applied as a horizontal crop pan.

## Open items / deferred to 3.x

- Smart reframe (face/motion-tracked crop window).
- Multi-aspect sibling exports (9:16 + 1:1 + 16:9 from one EDL).
- Caption auto-generation from voiceover script (burned-in or .srt).
- Direct social upload (YouTube/TikTok/Reels).
- Vector/semantic catalog search.
- Script generation from a topic (`stitch script --topic …`).
- Renderer parallelism (per-cue prepare in parallel).
- Global "renders.db" tracking every short produced.
