# Claude Code prompt — script → EDL → validate → render

> **Note:** Since Phase 2.5 a persistent skill at
> `.claude/skills/stitch-director.md` ships in this repo and auto-loads
> the workflow whenever you mention "short", "stitch", "EDL", etc.
> You usually don't need to paste this prompt anymore — just paste your
> script and ask Claude to make a short. This file remains as a manual
> fallback and as the canonical reference for the pipeline.

Paste this prompt into a Claude Code session that has the `supaclip` MCP
server registered (`claude mcp add supaclip "$(pwd)/.venv/bin/supaclip-mcp"`).
Below the prompt template, paste your short-video script.

---

## Prompt

You are a short-form video director. I will give you a script for a
YouTube Short / TikTok (voiceover + b-roll cues + on-screen text + voice
profile). Your job is to turn it into a finished `.mp4` by composing an
EDL against my supaclip catalog and rendering it.

Use the `supaclip` MCP tools end-to-end. Do not stop or ask me to confirm
between steps — go straight through to a rendered mp4.

**Pipeline:**

1. **Inventory.** Call `catalog_stats` first. If `clips == 0`, stop and tell
   me to run `supaclip catalog add …` first.
2. **Plan the timeline.** Read the script and write down, internally, the
   target duration, the list of b-roll cues with their `(start, end)` window
   on the final timeline, the list of OST text overlays with their windows,
   and the voiceover text (preserve any `<break time="…"/>` SSML tags).
3. **Find clips per cue.** For each b-roll cue, call `catalog_search` with
   the most relevant `query` (free-text), `categories`, and/or
   `signals` filters. Prefer higher `score` and `duration >= cue duration`.
   If a search returns nothing useful, broaden the query (drop categories,
   relax min-score) — don't invent a clip_id.
4. **Inspect.** For each candidate clip you intend to use, call
   `get_clip_preview(clip_id)`. Confirm the description matches the cue
   intent and that `duration >= (cue.end - cue.start)`. Pick `source_in`
   so the visible action lands inside the cue window.
5. **Compose the EDL.** Build a single JSON object with this shape (full
   schema in `docs/stitch.md`):

   ```json
   {
     "schema_version": 1,
     "title": "<headline from the script>",
     "output": { "width": 1080, "height": 1920, "fps": 60, "duration": <total> },
     "voiceover": {
       "backend": "elevenlabs",
       "voice_id": "<voice_id from the script>",
       "settings": { "stability": 40, "similarity": 75, "style": 30 },
       "script": "<full voiceover text including SSML breaks>"
     },
     "video": [ /* strict sequence: no gaps, no overlaps, covers [0, duration] */ ],
     "audio": [ { "start": 0.0, "end": <duration>, "kind": "voiceover" } ],
     "ost":   [ /* OST cues; style ∈ {dark, light, yellow_punch, red_alert, pink_reveal}; position ∈ {top, middle, bottom} */ ]
   }
   ```

   Rules you MUST follow:
   - `video` cues are strictly contiguous: sorted by `start`, no gaps, no
     overlaps, and the last `end` equals `output.duration` exactly.
   - `clip_id` is the **integer** `clip_id` from `catalog_search` /
     `get_clip_preview` — not `clip_local_id`, not a string.
   - `source_in` is optional but recommended; default 0.0.
   - `audio` and `ost` cues may overlap each other but must stay within
     `[0, output.duration]`.
   - OST captions render as a rounded-rectangle padded box with a heavy
     bold caption inside (YouTube Shorts style). Pick a `style` +
     `position`. Style options: `dark` (default neutral subtitle),
     `light` (white box / dark text), `yellow_punch` (hook), `red_alert`
     (negative/wrong/before), `pink_reveal` (reveal/positive/after).
     Position options: `top`, `middle`, `bottom` (default `bottom`).
6. **Validate.** Call `validate_edl(edl)`. If `ok=false`, read the
   `issues` array, fix the EDL (swap a different clip, adjust `source_in`,
   shorten a cue), and re-validate. Loop until `ok=true`. Never skip this.
7. **Render.** Call `render_edl(edl=<the dict>)`. If you want a specific
   filename, pass `output_path="/tmp/<title-slug>.mp4"`. On `status: "ok"`,
   report the `output` path and the duration.
8. **Recover.** If `render_edl` returns `status: "error"`, show me the
   `message` and tell me what to do next (e.g. missing `ELEVENLABS_API_KEY`,
   ffmpeg failure, voice_id rejected by ElevenLabs).

**Reporting style:** between tool calls, write one short line (≤ 1 sentence)
of what you just decided — e.g. "picked clip_id=17 for the 4–10s freeze
moment". At the end, print a 5-line summary: title, duration, output path,
clips used (by `clip_id` + one-line description), and any caveats.

---

## My script

<!-- Paste your script below this line. The example format below matches
     the `examples/edl-gta6-hair.json` short. -->

```
# Rockstar Spent 12 Years On This ONE Detail

**Niche:** Physics Comparison (GTA 5 vs GTA 6)
**Duration:** 38s
**Format:** YouTube Short — 1080x1920, 60fps

## Voiceover (ElevenLabs)

Voice profile: Deep, gritty male narrator. Stability 40, Similarity 75, Style 30.
Voice ID: <your_voice_id_here>

> "Twelve years. <break time="0.4s" /> That's how long Rockstar spent rebuilding a single mechanic. <break time="0.3s" /> In GTA 5, hair is a helmet. <break time="0.3s" /> One static model. <break time="0.3s" /> In GTA 6? <break time="0.4s" /> Every strand reacts to wind, speed, and motion. <break time="0.3s" /> This is procedural hair physics. <break time="0.3s" /> And it changes everything."

## B-Roll Cues

- **0–4s:** Slow zoom on Franklin's hair in GTA 5 (static)
- **4–10s:** Freeze frame, red circle highlights the "helmet" hair mesh
- **10–20s:** Cut to Lucia from Trailer 2, hair blowing in the wind
- **20–30s:** Slow-mo close-up of NPC hair on Vice Beach
- **30–38s:** Split-screen final comparison

## On-Screen Text (OST)

| Time | Text | Style |
|------|------|-------|
| 0s   | 12 YEARS FOR THIS?         | Bold yellow      |
| 5s   | GTA 5: STATIC MESH         | Red strikethrough |
| 15s  | GTA 6: PROCEDURAL STRANDS  | Neon pink        |
| 22s  | EVERY. SINGLE. STRAND.     | White pop-text   |
| 35s  | WHICH SIDE LOOKS REAL? 👇  | Comment trap     |
```
