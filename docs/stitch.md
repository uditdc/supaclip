# supaclip — Stitch (Phase 2)

`stitch` renders a short-form (default 1080×1920 @ 60 fps) video from an
**EDL** (Edit Decision List). Claude authors the EDL by browsing the
catalog through MCP; `stitch render` deterministically renders it.

```
script.md  ──▶  Claude (via MCP)
                │  catalog_search · get_clip_preview · validate_edl
                ▼
              edl.json  ──▶  stitch render  ──▶  short.mp4
                              │
                              ├─ ElevenLabs voiceover (cached)
                              ├─ per-cue cut + reframe to 9:16
                              ├─ concat + drawtext overlays + audio mix
                              └─ encode h264/aac
```

## EDL schema

```jsonc
{
  "schema_version": 1,
  "title": "Rockstar Spent 12 Years On This ONE Detail",
  "output": { "width": 1080, "height": 1920, "fps": 60, "duration": 38.0 },

  "voiceover": {
    "backend": "elevenlabs",
    "voice_id": "...",
    "settings": { "stability": 40, "similarity": 75, "style": 30 },
    "script": "Twelve years. <break time=\"0.4s\"/> That's how long..."
  },

  "video": [
    { "start":  0.0, "end":  4.0, "clip_id": 17, "source_in": 12.5, "reframe": "crop_center" },
    { "start":  4.0, "end": 10.0, "clip_id": 17, "source_in": 12.5 },
    { "start": 10.0, "end": 20.0, "clip_id": 42, "reframe": "crop_center" },
    { "start": 20.0, "end": 30.0, "clip_id": 51 },
    { "start": 30.0, "end": 38.0, "clip_id": 51, "reframe": "crop_right" }
  ],

  "audio": [
    { "start": 0.0, "end": 38.0, "kind": "voiceover" }
  ],

  "ost": [
    { "start":  0.0, "end":  4.5, "text": "12 YEARS FOR THIS?",       "style": "yellow_punch", "position": "top" },
    { "start":  5.0, "end": 14.0, "text": "GTA 5: STATIC MESH",        "style": "red_alert",    "position": "bottom" },
    { "start": 15.0, "end": 22.0, "text": "GTA 6: PROCEDURAL STRANDS", "style": "pink_reveal",  "position": "bottom" },
    { "start": 22.0, "end": 30.0, "text": "EVERY. SINGLE. STRAND.",    "style": "dark",         "position": "middle" },
    { "start": 35.0, "end": 38.0, "text": "WHICH SIDE LOOKS REAL?",    "style": "light",        "position": "bottom" }
  ]
}
```

Rules:
- `video` must be a strict sequence: cues sorted by `start`, no gaps, no
  overlaps, covering exactly `[0, output.duration]`.
- `audio` and `ost` may overlap. `audio[].kind` is `voiceover`, `clip_audio`,
  or `silence`.
- `voiceover.backend` is `elevenlabs` (default) or `google` (Gemini / Google
  AI Studio). For `google`, `voice_id` is a prebuilt voice name like `Kore` or
  `Puck` (see `stitch voices --backend google`) and delivery style is steered
  through the script text itself ("Say cheerfully: ...") rather than numeric
  `settings`. The `google` backend returns audio only, so `captions` (which
  need per-character timing) require the `elevenlabs` backend.
- `clip_id` is the integer catalog ID returned by `catalog_search` /
  `get_clip_preview`.
- `source_in` is optional; default is `0.0` (start of the master clip).
  The cue's duration on the source clip is `end - start`.
- `reframe` is `crop_center` (default), `crop_left`, `crop_right`, or
  `letterbox`.
- OST captions are rendered as transparent PNGs (rounded rect background +
  heavy bold font, YouTube-Shorts style) and overlaid by ffmpeg.
  - `style` ∈ `dark` (default), `light`, `yellow_punch`, `red_alert`,
    `pink_reveal` — see `supaclip/stitch/overlay.py:STYLE_PRESETS`.
  - `position` ∈ `top`, `middle`, `bottom` (default `bottom`).
  - Font: by default uses Open Sans ExtraBold / DejaVu Sans Bold; override
    with `stitch render --fontfile <path/to/Bold.ttf>`.

### v1.1 additions (Phase 2.5, all optional)

Each `video[i]` cue may also carry:

| Field | Default | Description |
|---|---|---|
| `reframe_offset` | `0` | future per-cue pixel offset (parsed; not yet applied) |
| `effect` | `"none"` | `"freeze_first"`, `"ken_burns_in"`, `"ken_burns_out"`, `"slow_mo"` |
| `effect_params` | `{}` | `{ "speed": 0.5 }` for slow_mo, `{ "zoom_from": 1.0, "zoom_to": 1.15 }` for ken_burns |
| `transition_in` | `"cut"` | `"crossfade"` to fade in from the previous cue |
| `transition_duration` | `0.0` | seconds; must be ≤ half the shorter neighbor cue |

Top-level optional fields:

```jsonc
"annotations": [
  { "start": 5.0, "end": 9.0, "shape": "circle",
    "x": 540, "y": 700, "radius": 180,
    "color": "#ff3b30", "stroke_width": 8 }
],
"music": {
  "file": "/path/to/bed.mp3",          // or "catalog:<clip_id>"
  "level_db": -22.0,
  "duck": true                          // sidechain-compress under voiceover
},
"captions": {
  "style": "clean_white",               // clean_white | boxed_dark | karaoke_yellow
  "position": "lower_third",            // top | middle | lower_third | bottom
  "max_words": 4,
  "max_chars": 28,
  "min_chunk_duration": 0.4,
  "font_size": null                     // optional override (pixels)
}
```

### Speech-synced captions vs OST

`ost` is **hand-authored** stylized emphasis text — you decide when and where
each card appears. `captions` is **auto-generated** from the voiceover using
character-level timestamps from the TTS backend; phrases appear in sync with
the spoken audio. Use OST for "moments" (hooks, reveals); use captions for
accessibility / sound-off viewing.

Captions require `voiceover` to be set (timing is derived from it). When
enabled, the render fetches alignment from `/v1/text-to-speech/{id}/with-timestamps`
(cached alongside the wav as a `.alignment.json` sidecar). The script is
chunked into short phrases on punctuation and word/char limits, then each
chunk is rendered as a PNG and overlaid on top of OST.

Annotation shapes (MVP rendering via ffmpeg `drawbox`):
- `box` — proper rectangle outline.
- `circle` — drawn as its square bounding box (true outline lands in 3.x).
- `arrow` — horizontal bar of length `width` (no arrow head yet).

## CLI

```bash
# Validate against the catalog (no render, no TTS spend)
stitch validate edl.json

# Render to mp4
stitch render edl.json -o out.mp4

# Inspect the ffmpeg command without running (great for debugging)
stitch render edl.json --print-ffmpeg

# Render only one cue (fast iteration on effects/annotations)
stitch render edl.json --preview-cue 2 -o /tmp/cue2.mp4

# One-off voiceover sample
stitch voice-preview --voice-id <id> --text "Twelve years."

# List ElevenLabs voices
stitch voices
```

Auth: `ELEVENLABS_API_KEY` in `.env` or `--api-key`. Output mp4 lands at
`<edl>.mp4` by default; a sidecar `<output>.edl.json` is written next to
the mp4 so re-renders are reproducible.

## MCP tools

Already exposed: `catalog_search`, `catalog_get_clip`, `catalog_list_sources`,
`catalog_stats`.

Added by Stitch:
- `get_clip_preview(clip_id)` — compact preview with `description`,
  `categories`, `duration`, `score`, `keyframes`, `source_in/out`, etc.
- `validate_edl(edl)` — returns `{ok, issues:[{severity,path,message}]}`.
- `render_edl(edl, output_path?)` — synthesizes the voiceover, reframes,
  concatenates, overlays text, mixes audio. Returns `{status, output,
  sidecar, duration}`. Requires `ELEVENLABS_API_KEY` in the MCP server's
  environment if the EDL has a voiceover. TTS results are cached, so
  re-renders of the same script are free.

## Walkthrough: GTA 6 hair-physics short

1. User pastes the script (voiceover, b-roll cues, OST table) into Claude.
2. Claude calls `catalog_search` per b-roll cue (e.g. "Franklin hair static",
   "Lucia hair wind", "NPC hair Vice Beach") and inspects candidates via
   `get_clip_preview`.
3. Claude assembles an `edl.json` matching the schema above and calls
   `validate_edl` to confirm.
4. User runs `stitch render edl.json -o gta6-hair.mp4`. ElevenLabs
   synthesizes the voiceover (cached for re-runs), ffmpeg renders the
   composition.
5. The mp4 is ready to upload; the sidecar JSON pins the inputs.

## Out of scope (Phase 2.5)

Ken-Burns zoom, freeze frame, red-circle highlights, slow-mo, split-screen,
animated text pops, music-bed track, auto script generation, smart
reframe (face/motion-tracked crop window).
