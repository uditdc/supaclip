# Example EDLs

These are sample Edit Decision Lists for `stitch` — the same JSON an LLM
director produces when composing a short. Use them as a schema reference or a
starting template.

| File | Demonstrates |
|---|---|
| [`edl-gta6-hair.json`](edl-gta6-hair.json) | Minimal v1 EDL: cuts, voiceover, on-screen text |
| [`edl-gta6-hair-v15.json`](edl-gta6-hair-v15.json) | Effects (Ken-Burns, freeze, slow-mo), crossfades, a `reframe_offset` pan, and a circle annotation |

## Before you render

The `video[].clip_id` values point at clips **in your own catalog**, and
`voiceover.voice_id` is a placeholder (`REPLACE_WITH_VOICE_ID`). To run an
example end to end:

1. Extract and ingest some footage so your catalog has clips:
   ```bash
   extract session.mp4
   supaclip catalog add clips/manifest.json
   supaclip catalog search "..."        # note the integer clip_id values
   ```
2. Edit the example: swap the `clip_id`s for ones from your catalog, and set
   `voiceover.voice_id` to a real voice (`stitch voices`).
3. Validate, then render:
   ```bash
   stitch validate examples/edl-gta6-hair-v15.json
   stitch render   examples/edl-gta6-hair-v15.json -o short.mp4
   ```

`stitch validate` checks clip references, timeline contiguity, and effect
parameters against your catalog before any TTS or ffmpeg work happens — run it
first. See [`docs/stitch.md`](../docs/stitch.md) for the full schema.
