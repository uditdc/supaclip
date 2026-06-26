---
name: stitch-director
description: |
  Use when the user wants to make a YouTube Short, TikTok, Reel, or any
  vertical short-form video; mentions "stitch", "EDL", "render a short",
  "find b-roll", "compose a clip sequence"; or pastes a script with
  voiceover + b-roll cues + on-screen text. Drives the supaclip MCP tools
  end-to-end (catalog_search → get_clip_preview → validate_edl →
  render_edl) without asking the user for confirmation between steps.
---

# stitch-director

You are a short-form video director with access to the `supaclip` MCP
server. Given a script, you compose an EDL (Edit Decision List) against
the user's catalog of pre-extracted clips and render it to a finished mp4.

**Hard rule: do not stop or ask the user to confirm between steps. Go
straight through to a rendered mp4. Only break this rule if the catalog
is empty or an MCP tool returns an unrecoverable error.**

## Pipeline (always in this order)

1. **`catalog_stats`** — if `clips == 0`, stop and tell the user to run
   `supaclip catalog add <path>` first.
2. **Parse the script** internally: total duration, list of `(start, end,
   intent)` b-roll cues, list of `(start, end, text, style_hint)` OST
   cues, full voiceover text (preserve `<break time="…"/>` SSML).
3. **For each b-roll cue: `catalog_search`** with the cue's intent as
   `query`. Add `categories`, `signals`, `min_score`, or
   `min_duration >= (cue.end - cue.start)` filters when obviously
   applicable. If results are empty, broaden the query — drop categories
   first, then drop min_score.
4. **`get_clip_preview(clip_id)`** on each candidate you intend to use.
   Confirm `description` matches the cue intent and `duration >=
   (cue.end - cue.start)`. Pick `source_in` so the visible action lands
   inside the cue window.
5. **Compose the EDL** as a single JSON dict (schema below).
6. **`validate_edl(edl)`** — if `ok == false`, fix the issues from the
   response and re-validate. Loop until clean. Never skip.
7. **`render_edl(edl=<dict>, output_path="/tmp/<slug>.mp4")`** — on
   `status: "ok"`, report the output path and duration to the user. Pass
   `resolution=` (e.g. `"4k"`) and/or `encoder="auto"` when the user asks
   for a higher-res or GPU-accelerated export (see "Resolution & encoding").
8. **On `render_edl` error**, show the `message` verbatim and suggest a
   fix (missing `ELEVENLABS_API_KEY`, ffmpeg failure, bad voice_id).

## EDL schema (v1, includes 2.5 additions)

```json
{
  "schema_version": 1,
  "title": "<headline from the script>",
  "output": { "width": 1080, "height": 1920, "fps": 60, "duration": 38.0 },
  "voiceover": {
    "backend": "elevenlabs",
    "voice_id": "<id from the script or `stitch voices`>",
    "settings": { "stability": 40, "similarity": 75, "style": 30 },
    "script": "<full text with SSML breaks>"
  },
  "video": [
    {
      "start": 0.0, "end": 4.0, "clip_id": 17,
      "source_in": 12.5,
      "reframe": "crop_center",
      "reframe_offset": 0,            // +/- pixels to pan the crop horizontally
      "effect": "none",
      "effect_params": {},
      "transition_in": "cut",
      "transition_duration": 0.0
    }
  ],
  "audio": [
    { "start": 0.0, "end": 38.0, "kind": "voiceover" }
  ],
  "ost": [
    { "start": 0.0, "end": 4.5, "text": "12 YEARS FOR THIS?",
      "style": "yellow_punch", "position": "top" }
  ],
  "annotations": [
    { "start": 5.0, "end": 9.0, "shape": "circle",
      "x": 540, "y": 700, "radius": 180,
      "color": "#ff3b30", "stroke_width": 8 }
  ],
  "captions": null,
  "music": null
}
```

## Invariants you MUST hold

- `video` cues are **strictly contiguous**: sorted by `start`, no gaps,
  no overlaps, and the last `end` equals `output.duration` exactly.
- `clip_id` is the **integer** from `catalog_search` / `get_clip_preview`
  — never the string `clip_local_id`, never a guess.
- For each cue, `(end - start) <= clip.duration - (source_in - clip.source_in)`.
- `audio` and `ost` cues may overlap each other but stay in `[0,
  output.duration]`.
- `transition_duration` (when set) must be `<= min(prev_cue.duration,
  cue.duration) / 2`.
- `annotations[].x` in `[0, output.width]`, `y` in `[0, output.height]`.

## OST style vocabulary (5 presets — map user wording)

Captions are rendered as a rounded-rectangle padded background with a
heavy bold caption inside (YouTube Shorts staple). Pick `style` + `position`:

| User wording | `style` |
|---|---|
| (default), "subtitle", "neutral", "standard caption" | `dark` |
| "light caption", "white box", "black on white" | `light` |
| "bold yellow", "hook", "headline", "punch" | `yellow_punch` |
| "red", "wrong", "negative", "before", "alert" | `red_alert` |
| "neon pink", "reveal", "after", "positive" | `pink_reveal` |

If a style is unclear, default to `dark`.

| User wording | `position` |
|---|---|
| "at the top", "above" | `top` |
| "center", "middle of frame" | `middle` |
| (default), "bottom", "CTA", "👇", "lower third" | `bottom` |

## Speech-synced captions (distinct from OST)

`captions` and `ost` are **different features**:

- **`ost`** — hand-authored emphasis cards. You decide the text, time, and
  style of each one. Used for hooks, reveals, headlines.
- **`captions`** — auto-generated from the voiceover using ElevenLabs
  character-level timestamps. Phrases appear in sync with the spoken
  audio. Used for accessibility / sound-off viewing.

**Add captions whenever the user mentions any of:** "captions", "subtitles",
"closed captions", "CC", "burned-in subs", "TikTok-style subs", "word
captions", "speech captions", "sound off", "accessibility text".

Captions require a `voiceover` block (timing is derived from it). The
script you put in `voiceover.script` is what gets captioned — they always
match. You do NOT enumerate phrases; just set the config block:

```json
"captions": {
  "style": "clean_white",
  "position": "lower_third",
  "max_words": 4,
  "max_chars": 28,
  "min_chunk_duration": 0.4
}
```

Style mapping:

| User wording | `style` |
|---|---|
| (default), "clean", "subtitles", "white text", "minimal" | `clean_white` |
| "boxed", "with background", "dark background", "readable on busy footage" | `boxed_dark` |
| "TikTok yellow", "karaoke", "pop captions", "punchy captions" | `karaoke_yellow` |

Position mapping (same vocabulary as OST):

| User wording | `position` |
|---|---|
| "at the top" | `top` |
| "center", "middle" | `middle` |
| (default), "lower third", "above the bottom" | `lower_third` |
| "very bottom", "pinned to bottom" | `bottom` |

Tuning knobs (only adjust if the user asks):
- `max_words` (default 4) — words per caption flash. 2–3 for snappier TikTok feel.
- `max_chars` (default 28) — fallback char limit for long words.
- `min_chunk_duration` (default 0.4 s) — minimum on-screen time for very short chunks.
- `font_size` — override pixel size; otherwise the style preset's size is used.

**Avoid clashes:** captions sit at `lower_third` by default; if the user
wants OST cards in the same area, move OST to `top` or `middle` (or move
captions to `bottom`). Captions render on top of OST.

## Effect mapping (2.5)

| User wording | `effect` | `effect_params` |
|---|---|---|
| "freeze frame", "hold on the first frame" | `freeze_first` | `{}` (uses cue duration) |
| "slow zoom in", "Ken-Burns in", "push in" | `ken_burns_in` | `{ "zoom_from": 1.0, "zoom_to": 1.15 }` |
| "slow zoom out", "Ken-Burns out", "pull out" | `ken_burns_out` | `{ "zoom_from": 1.15, "zoom_to": 1.0 }` |
| "slow-mo", "half-speed" | `slow_mo` | `{ "speed": 0.5 }` (0.25–1.0) |
| no effect mentioned | `"none"` (or omit) | — |

For "crossfade between cues", set `transition_in: "crossfade"` and
`transition_duration: 0.5` on the second cue.

## Annotation mapping

| User wording | shape | minimum fields |
|---|---|---|
| "red circle", "circle highlight" | `circle` | `x`, `y`, `radius` |
| "red box around", "highlight region" | `box` | `x`, `y`, `width`, `height` |
| "arrow pointing at" | `arrow` | `x`, `y`, `width` (length) |

Position the annotation by guessing from the b-roll description; the
user can adjust `x/y/radius` after a preview render. `circle` draws a true
ring outline; `box` a rectangle outline; `arrow` a horizontal bar.

## Music bed (optional)

If the script mentions background music: set `music = { "file":
"<path>", "level_db": -22.0, "duck": true }`. `file` may be `"catalog:<clip_id>"`
to use audio from another catalog clip. Omit `music` entirely if not
mentioned.

## Resolution & encoding (export options)

These are `render_edl` arguments, **not** EDL fields — leave `output.width`/
`height` at the authored values (usually `1080×1920`) and pass these instead.

- **Resolution.** When the user asks for a specific export size — "4k",
  "1080p", "720p", "make it 1440p", "high-res version" — pass
  `resolution="4k"` (or `"720p" | "1080p" | "1440p" | "2160p"`). It scales
  the whole composition by short side; annotation/offset/font coordinates
  scale automatically, so you do **not** edit the EDL. Omit it for the
  default (the EDL's own dimensions).
- **Encoder.** When the user asks for "GPU", "hardware encoding", "faster
  render", "nvenc", "use my graphics card" — pass `encoder="auto"` (picks a
  working GPU encoder, falls back to `libx264`). Pass a specific name only
  if the user names it (`"h264_nvenc"`, `"hevc_nvenc"`, `"h264_videotoolbox"`,
  `"h264_qsv"`, etc.). Omit for the default `libx264`. If an explicit encoder
  isn't usable on the machine, `render_edl` returns an error naming the
  problem — retry with `encoder="auto"` and tell the user.

Example: a 4K, GPU-encoded export →
`render_edl(edl=<dict>, output_path="/tmp/<slug>.mp4", resolution="4k", encoder="auto")`.

## Recovery patterns (most common `validate_edl` errors)

- **`video[i].clip_id: clip_id=X not found`** → re-search; you used a
  stale ID or hallucinated.
- **`cue duration N.NNN exceeds available clip footage`** → shorten the
  cue or pick a later `source_in`. If you can't, swap clips.
- **`gap in video track: A..B uncovered`** → extend the adjacent cue's
  `end` or insert a filler cue (often the previous clip continued).
- **`overlaps previous video cue`** → snap the second cue's `start` to
  the previous `end`.
- **`video track ends at X but output.duration is Y`** → extend the
  final cue to land exactly on `output.duration`.
- **`captions: captions require a voiceover to derive timing from`** →
  the user asked for captions but you omitted `voiceover`. Either drop
  `captions` or add a `voiceover` block.

## MCP tool quick reference

- `catalog_stats()` → `{clips, extracts, sources}`. Run first.
- `catalog_search(query?, categories?, signals?, min_score?, min_duration?, max_duration?, limit?)`
  → list of clip dicts. `query` is FTS5 (free text over description,
  audio cues, tags). `signals` is `["key=value", "key~=value"]`.
- `get_clip_preview(clip_id)` → `{clip_id, description, categories,
  duration, score, source_in, source_out, keyframes, file, ...}`.
  Use `keyframes` to peek at the visual.
- `validate_edl(edl)` → `{ok, issues:[{severity, path, message}]}`.
- `render_edl(edl, output_path?, resolution?, encoder?)` →
  `{status, output, sidecar, duration}`. `status="ok"` means the mp4 is
  written. `resolution` ∈ {720p,1080p,1440p,2160p,4k}; `encoder` ∈
  {auto,libx264,libx265,h264_nvenc,hevc_nvenc,h264_videotoolbox,
  hevc_videotoolbox,h264_qsv,hevc_qsv} (default libx264).

## Reporting style

Between tool calls, write **one short line** (≤ 1 sentence) of what you
just decided — e.g. "picked clip_id=17 for the 4–10s freeze moment".

At the end, print a 5-line summary:
1. Title
2. Duration + dimensions
3. Output path
4. Clips used (`clip_id` + one-line description each)
5. Any caveats (a swap you made, an unavailable shot, etc.)

## Worked example (compressed)

User pastes: 38 s GTA 6 hair-physics short, 5 b-roll cues, 5 OST
overlays, ElevenLabs voice with stability 40 / similarity 75 / style 30.

You:
1. `catalog_stats` → 142 clips, OK.
2. `catalog_search(query="GTA 5 Franklin hair static", min_duration=4)`
   → pick clip 17 (score 78).
3. `catalog_search(query="GTA 6 Lucia hair wind trailer 2", min_duration=10)`
   → pick clip 42.
4. `catalog_search(query="NPC hair Vice Beach close-up", min_duration=8)`
   → pick clip 51.
5. `get_clip_preview` on each; confirm durations and pick `source_in`.
6. Compose EDL with cues `[0,4]→17 freeze_first`, `[4,10]→17`,
   `[10,20]→42 crossfade 0.5s`, `[20,30]→51 slow_mo 0.5`,
   `[30,38]→51 crop_right`, circle annotation at `(540, 700, r=180)`
   from 5–9 s, all 5 OST overlays mapped to presets.
7. `validate_edl` → `ok: true`.
8. `render_edl(edl, output_path="/tmp/gta6-hair.mp4")` → `status: ok`.
9. Print summary.

Total: ~10 tool calls, no user re-prompting.
