# Caption & OST Quality Upgrade — Spec

Status: proposed
Consumer driver: brainrotfactory (Shorts retention; frame-0 hook cards + kinetic captions)
Scope: `supaclip/stitch/captions.py`, `supaclip/stitch/overlay.py`, `supaclip/stitch/assembly.py`, `supaclip/core/edl.py`, `supaclip/stitch/encode.py`, tests.

## Goals

- Raise the perceived production quality of burned-in captions and on-screen text (OST) to match top-tier short-form (CapCut/TikTok) output.
- Keep every change **additive and backward-compatible**: existing EDLs render byte-identically until they opt in.
- Land in tiers so Tier 1 (pure fidelity, no schema change) ships and is validated before any API surface grows.

## Non-goals

- No changes to timing/alignment logic (`chunk_alignment`, `_extract_words`) beyond what animation needs.
- No new TTS/voiceover behavior.
- No per-frame filter-graph programming — animation stays in the existing "static PNG + `enable` window" model (see Design principle 3).

## Current state (as-built)

- Both captions and OST render text → PNG via PIL, then ffmpeg `overlay=...:enable='between(t,s,e)'`. Layer order: video → annotations → OST → captions → watermark (`assembly.py:161-189`).
- Captions: presets `clean_white`, `boxed_dark`, `karaoke_yellow` (`captions.py:34-71`). Karaoke = one PNG per word, whole phrase re-rendered with words `0..active` highlighted, hard-swapped by `enable` windows (`_karaoke_chunk_renders`).
- OST: presets `dark`/`light`/`yellow_punch`/`red_alert`/`pink_reveal` (`overlay.py:28-89`); solid rounded-rect box, one static PNG per cue.
- Fonts: `_resolve_font` (`overlay.py:125-136`) picks the first installed system bold (DejaVu/Liberation/OpenSans). Non-deterministic across machines.
- Rendering: single-pass PIL at output resolution, stroke only, no shadow, no supersampling.
- PNG inputs are added with plain `-i` (no `-loop 1`); a single-frame image persists under the overlay for its enable window. Multiple PNGs with adjacent enable windows is the existing idiom (karaoke already does it per word).

### Confirmed constraints

- All EDL models use `ConfigDict(extra="forbid")` (11 models). New fields **must** be declared. Reading an old EDL that lacks the new fields is fine (defaults apply); a new EDL that sets new fields will be **rejected by an older supaclip** — so consumer (brainrot) and library upgrade together.
- `EDL_SCHEMA_VERSION = 1`; `validate_edl` hard-rejects a mismatch (`edl.py:193`). Additive optional fields do **not** require a bump. Bump only when new defaults should become the standard render.
- `scale_edl` (`encode.py:155`) scales caption `font_size` **only when explicitly set** (`encode.py:199`); preset base sizes are constants and are **not** resolution-scaled. At 4K a 62px caption is tiny — a multi-res consistency bug to fix in Tier 1.

## Design principles

1. **Additive schema.** New optional fields with defaults that reproduce today's output. Never mutate an existing preset's bytes; add new preset names instead.
2. **Presets stay data, effects stay declarative.** An effect (shadow, animation) is expressed as fields on the style dataclass / EDL model, not hardcoded per call site.
3. **Animation via multi-PNG, not per-frame filters.** A "pop" is N pre-rendered scale steps, each with its own `enable` window — same mechanism as karaoke-per-word. No `zoompan`/`geq`/time-varying `overlay` expressions. This keeps the ffmpeg graph shape unchanged and cache-friendly.
4. **Deterministic output.** Bundle fonts as package data; fold every visual parameter into the PNG cache key.

---

## Phase 1 — Fidelity (no schema change, low risk)

Pure rendering-quality improvements. No EDL field additions; output of existing EDLs changes only in that text looks better (crisper, shadowed, better font). Gate behind a bump of a rendering-internal default only if we want bit-stability for existing users — otherwise treat as an intended quality upgrade and snapshot-test the new baseline.

### 1a. Bundle a display font

- Add `supaclip/assets/fonts/` with an OFL-licensed display face (candidate: **Anton** for the classic brainrot look, or **Montserrat ExtraBold** / **Inter Black** for a cleaner caption). Ship 1–2 faces.
- `_resolve_font(fontfile)` precedence becomes: explicit `fontfile` arg → bundled asset → installed system font (current list) → raise. This makes `--fontfile` still authoritative and removes the machine-dependent default.
- Resolve via `importlib.resources` so it works from an installed wheel.
- License note: only ship SIL OFL / Apache fonts; record license files under `assets/fonts/` and list them in packaging `include`.

### 1b. Supersample text

- Render each text PNG at `SS = 2×` (font size, padding, stroke, corner radius all ×2), then downscale to target with `Image.LANCZOS`.
- Applies to `_render_caption_png`, `_render_caption_karaoke_png` (captions.py) and `render_caption_png` (overlay.py).
- Net effect: crisp anti-aliased edges, stroke no longer chunky. Cost: ~4× PIL pixels per PNG (negligible; PNGs are cached).

### 1c. Drop shadow

- Extend the style dataclasses (`CaptionVisualStyle`, `overlay.CaptionStyle`) with:
  - `shadow: RGBA | None`, `shadow_offset: tuple[int,int]`, `shadow_blur: int`.
- Draw order in the PNG: shadow layer (offset, Gaussian-blurred alpha) → stroke → fill.
- Defaults: `None` on the existing presets so current output is unchanged; enable it on the **new** presets added in Phase 3 and on the new default font baseline.

### 1d. Resolution-aware preset sizing

- In `scale_edl`, also scale the **effective** preset font size when `captions.font_size`/OST size is unset, OR compute preset sizes as a fraction of `out_h` at render time (preferred: `font_size = round(out_h * PRESET_FRACTION)`).
- Keeps captions the same relative size at 720p/1080p/1440p/4K.

### 1e. Cache-key correctness (prerequisite for all tiers)

- `_png_filename` / `_karaoke_png_filename` (both files) currently omit `fontfile` and any effect params. Fold into the key: resolved font identity (path or bundled name), supersample factor, shadow params, and (Phase 2/3) animation params. Without this, a font/effect change silently reuses stale PNGs.

### Phase 1 tests

- Golden snapshot: dimensions + SHA of rendered PNGs for each existing preset at 1080×1920.
- A "defaults unchanged" fixture asserting that with shadow `None` + bundled font pinned, output is stable across runs/machines.
- Multi-res test: same relative caption height at 720p and 4K.

---

## Phase 2 — Kinetic captions (additive, opt-in)

New optional fields on `EDLCaptions`; default values reproduce Phase-1 behavior.

### 2a. Schema additions (`core/edl.py`)

```python
CaptionAnimation = Literal["none", "pop", "fade"]

class EDLCaptions(BaseModel):
    # ...existing...
    animate: CaptionAnimation = "none"          # entrance of each active word/chunk
    animate_overshoot: float = 0.12             # pop scale peak = 1 + overshoot
    animate_duration: float = 0.12              # seconds for the entrance
    active_word_bg: str | None = None           # hex; rounded pill behind active word
    active_word_bg_radius: int = 12
    fade_ms: int = 0                            # per-chunk in/out fade (0 = off)
```

Validation (`validate_edl`): `animate_overshoot >= 0`, `0 <= animate_duration <= 1`, `fade_ms >= 0`, `active_word_bg` is hex when set.

### 2b. "pop" animation (multi-PNG)

- For each active word, instead of one PNG, emit `K` PNGs (K≈3) at scales `[1+overshoot, 1+overshoot/2, 1.0]` covering the first `animate_duration` of the word's window; the settled (1.0×) PNG covers the remainder.
- Only the **active** word scales; the rest of the phrase stays at 1.0× in the same PNG (so layout is stable — scale is applied to the whole phrase PNG but anchored so non-active words don't visibly move; simplest correct approach: render the settled phrase, then render the active word's glyphs scaled about their own center on a transparent overlay composited at the same position). Implementation note: keep the phrase-level PNG for layout; add a small per-word scaled overlay PNG for the pop. This avoids reflowing the line.
- Reuses the per-word `enable`-window emission path in `_karaoke_chunk_renders`; K sub-windows per word instead of 1.

### 2c. "fade"

- Per-chunk entrance/exit alpha ramp of `fade_ms`. Implement by adding a short `fade` on the PNG overlay input (`fade=t=in:st=..:d=..:alpha=1`) in the caption overlay chain, or by emitting 2–3 alpha-stepped PNGs. Prefer the `fade` filter on the overlay input — it's one filter, no extra PNGs, and doesn't disturb layout.

### 2d. active-word highlight pill

- When `active_word_bg` set, draw a rounded rect behind the active word using the geometry `_pack_words` already computes (per-word x/width per line). Composited under the glyphs in `_render_caption_karaoke_png`.

### Phase 2 tests

- With all new fields at defaults → identical to Phase 1 (byte golden).
- `animate="pop"`: assert K sub-renders per word with monotonically decreasing scale and correct cumulative windows.
- `active_word_bg`: snapshot one karaoke frame with pill.

---

## Phase 3 — Rich OST / hook cards (additive, opt-in) — highest retention lever

New optional fields on `EDLOSTCue` + new presets. Hook card should *arrive*, not blink.

### 3a. Schema additions (`core/edl.py`)

```python
OSTAnimation = Literal["none", "pop", "slide_up", "fade"]

class EDLOSTCue(BaseModel):
    # ...existing...
    animate_in: OSTAnimation = "none"
    animate_out: OSTAnimation = "none"
    animate_duration: float = 0.18
```

New style presets (add names; do **not** edit existing):
- `yellow_punch_shadow` — `yellow_punch` + drop shadow (Phase 1c).
- `gradient_dark` — vertical gradient box fill.
- `accent_bar` — left accent bar + dark box (news-lower-third look).

### 3b. Entrance/exit animation

- `pop`: same multi-PNG scale-step approach as captions, applied to the whole OST PNG (a hook card popping in reads great and layout stability doesn't matter since it's a standalone card).
- `slide_up`: emit position-stepped windows (y from `+offset` → settled) over `animate_duration`.
- `fade`: `fade` filter on the OST overlay input.

### 3c. Emoji / color glyphs (optional, gated)

- Detect emoji codepoints in cue text; if present and a color-emoji font (Noto Color Emoji, CBDT/COLR) is available, composite emoji glyphs in a second pass (PIL `truetype` can't render color glyphs inline). Gate behind `EDLOSTCue`-level or render-config flag; default off. Ship as a follow-up if it complicates the core PR.

### Phase 3 tests

- New presets: dimension + hash snapshots.
- `animate_in="pop"` / `"slide_up"`: assert window/scale/position step sequences.
- Defaults unchanged golden.

---

## ffmpeg / assembly impact

- No new filter *kinds* for pop/slide (extra PNGs + existing overlay `enable` windows). `fade` is the one new filter, applied to the PNG overlay input before overlay — localized to `build_caption_overlay_chain` / `build_ost_overlay_chain`.
- `assembly.py` already adds one `-i` per render PNG and threads `input_indices`; more PNGs per word/cue just means more indices — no structural change.
- Watch input count: karaoke already multiplies inputs by words; pop multiplies by K again. For a 60–90s Short this is fine (hundreds of `-i` PNGs), but add a guard/log if total inputs exceed a threshold, and keep the PNG cache hot (dedup identical PNGs by content hash — the cache key work in 1e enables this).

## Rollout sequence

1. **PR 1 (Phase 1):** font asset + supersample + shadow fields (default off) + resolution-aware sizing + cache-key fix + snapshot tests. No EDL API change. Ship, eyeball real renders.
2. **PR 2 (Phase 2):** `EDLCaptions` animation/pill/fade fields, defaults preserve PR-1 output. Tests.
3. **PR 3 (Phase 3):** `EDLOSTCue` animation + new presets (+ optional emoji follow-up). Tests.
4. **Consumer (brainrotfactory):** after each library release, `composer.py` opts in — Phase-1 is automatic; then set `captions.animate="pop"`, `captions.active_word_bg`, and the hook OST cue to `yellow_punch_shadow` + `animate_in="pop"`. One small consumer diff per phase.

## Backward-compat / versioning checklist

- [ ] All new fields optional with output-preserving defaults.
- [ ] No existing preset mutated; new looks = new names.
- [ ] `EDL_SCHEMA_VERSION` unchanged for additive fields (bump only if/when new defaults become standard).
- [ ] Consumer and library upgrade together (because of `extra="forbid"`).
- [ ] "Defaults unchanged" golden test in every PR.

## Resolved decisions

- **Bundled font is a fallback only.** The bundled display face sits below an explicit `fontfile` in `_resolve_font` precedence (explicit → bundled → system → raise). brainrotfactory passes its chosen font explicitly via `RenderConfig.fontfile`, so the new default never silently restyles other consumers (movie-clips, etc.). The bundled font only changes the look for callers that pass nothing.
- **Phase 1 is a deliberate baseline change.** No bit-stability flag. Snapshot goldens are regenerated to the new (supersampled, better-font) baseline and shipped with a clear release note. Bit-stability behind a flag is explicitly rejected — it defeats the quality goal. The "defaults unchanged" tests therefore assert *shadow-off / animation-off* parity within the new baseline, not parity with pre-upgrade output.
