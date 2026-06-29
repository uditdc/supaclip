---
name: movie-clips
description: |
  Use when the user wants standalone HIGHLIGHT clips from a film — one short
  per scene/beat — rather than a narrated story recap. Triggers: "movie clips",
  "interesting bits", "best scenes as shorts", "one clip per beat", "highlight
  clips", "clip up this movie". Produces one 30-60s vertical clip per story
  beat with the film's ORIGINAL audio and its OWN subtitles styled like ours.
  Optional `--commentary` adds your TTS take over ducked audio.

  For a single narrated story summary across the whole film, use movie-recap
  instead. This skill keeps each scene as its own standalone clip.
---

# movie-clips

You turn a film already in the catalog into a set of **standalone highlight
clips — one per story beat** — using the `supaclip` MCP server. Each clip is a
30-60s vertical short of one scene, with the film's original audio and its own
subtitles burned in, in our style. No story narration stitches them together
(that's `movie-recap`).

**Hard rule: drive straight through to rendered mp4s. Only stop if the catalog
is empty, the movie is ambiguous, or an MCP tool returns an unrecoverable error.**

## Two modes

- **plain** (default) — original audio + the film's own subtitles styled. Most
  faithful to the scene. (Highest copyright exposure: it's the source A/V.)
- **`--commentary`** — your short TTS commentary over **ducked** original audio,
  with your commentary as the captions. More transformative; lower copyright
  risk. Use when the user asks for commentary/reaction/explainer clips.

## Pipeline

1. **`catalog_stats`** — if `clips == 0`, stop; tell the user to
   `extract movie.mp4 --game-profile movie --segmenter auto --max-duration 10800`
   then `supaclip catalog add`. A sidecar `movie.srt` enables the captions.
2. **Identify the movie** — `catalog_list_sources`; if one source use it, else
   match the user's title, else ask once which `source_id`.
3. **Load the spine** — `catalog_get_summary(source_id)` → `beats`. One clip per
   beat. (No summary → fall back to evenly-spaced windows over the runtime.)
4. **For each beat, build and render one clip** (sub-pipeline below).
5. **Report** the list of rendered clips (format under "Reporting").

## Per-beat sub-pipeline

1. **Candidates** — `catalog_search(source="…", order_by="timeline", limit=500)`
   once, then take clips whose `source_in` is inside the beat's `[start,end)`.
   Prefer clips ≥30s; rank by `score` then `duration`.
2. **Pick a clean clip** — call **`probe_clip(clip_id)`** on candidates in rank
   order; use the first with `decodes_clean == true`. Real rips have corrupt
   H.264/AAC regions that abort a render — skip them. Keep `peak_db` from the
   probe for audio gain. (If none are clean, take the top candidate anyway.)
3. **Window** — one video cue `[0, D]`, `clip_id`, `source_in = clip.source_in`,
   `reframe: "crop_center"`, where `D = min(60, clip.duration)` (≥30 if possible).
4. **Audio**:
   - plain → `clip_audio` at a constant gain that peak-normalizes it:
     `level_db = round(-1.0 - peak_db, 1)` (from `probe_clip`), clamped `[0, 24]`.
     A fixed gain (no limiter) keeps the scene's dynamics — no pumping.
   - `--commentary` → `clip_audio` ducked to `level_db: -24` **plus** a
     `voiceover` cue `[0, D]`.
5. **Captions**:
   - plain → `get_clip_subtitles(clip_id)` → put its `cues` into
     `EDLCaptions.cues` (style `karaoke_yellow`, position `lower_third`). The
     film's real dialogue, synced. If `cues` is empty, omit `captions`.
   - `--commentary` → captions derive from the `voiceover` (omit `cues`); your
     commentary appears as the on-screen text.
6. **Commentary script** (`--commentary` only) — write a tight 1-3 sentence take
   on this beat FROM its `summary` (+ the clip's `dialogue`/`get_clip_subtitles`).
   Present tense, your voice. Same `voice_id` across all clips.
7. **Watermark** — `output.watermark = {text:"<brand>", opacity:0.45,
   font_size:40, position:"top"}` (default brand `brainrotfactory.co` unless the
   user gives one).
8. **`validate_edl`** → fix and re-validate until `ok`. **`render_edl`** →
   `output_path="/tmp/<movie-slug>-clipNN-<beat-slug>.mp4"` (zero-pad NN).

## EDL shapes

Plain:
```json
{
  "schema_version": 1,
  "title": "<Movie> — <Beat Title>",
  "output": {"width":1080,"height":1920,"fps":24,"duration":60.0,
             "watermark":{"text":"brainrotfactory.co","opacity":0.45,"font_size":40,"position":"top"}},
  "video": [{"start":0.0,"end":60.0,"clip_id":42,"source_in":3487.0,"reframe":"crop_center"}],
  "audio": [{"start":0.0,"end":60.0,"kind":"clip_audio","level_db":6.0}],
  "captions": {"style":"karaoke_yellow","position":"lower_third",
               "cues":[{"start":1.0,"end":4.0,"text":"<source dialogue line>"}]},
  "ost": [], "music": null
}
```

`--commentary` differs only in audio + captions + voiceover:
```json
  "voiceover": {"backend":"google","voice_id":"Zephyr","settings":{},"script":"<your take>"},
  "audio": [{"start":0.0,"end":60.0,"kind":"clip_audio","level_db":-24.0},
            {"start":0.0,"end":60.0,"kind":"voiceover"}],
  "captions": {"style":"karaoke_yellow","position":"lower_third"}
```

## Invariants

- Video track is contiguous and covers `[0, output.duration]` (one cue is fine).
- `clip_id` is the integer from search/probe; cue `(end-start) <= clip.duration`.
- `captions.cues` (if any) lie within `[0, duration]`; plain captions need
  cues, `--commentary` captions need the voiceover — never both empty.
- Same `voice_id`, watermark, and caption style across the whole set.

## fps & fit

`fps: 24` (most films); match the source if known. The 9:16 `crop_center` may
show the film's baked-in cinemascope letterbox — that's expected.

## Recovery

- **`probe_clip` decodes_clean=false for all candidates** → use the top one and
  note the clip may have artifacts.
- **`cue duration exceeds available footage`** → lower `D` to the clip duration.
- **`captions require a voiceover or explicit cues`** → plain clip with no
  source subtitles: drop `captions` (footage + audio + watermark only).
- **render error** → show `message`; missing `GEMINI_API_KEY` (commentary VO)
  is the usual cause.

## MCP tool quick reference

- `catalog_stats`, `catalog_list_sources`, `catalog_get_source`,
  `catalog_get_summary(source_id)` → beats.
- `catalog_search(source?, order_by="timeline", limit?)` → clips in story order.
- **`probe_clip(clip_id, max_seconds?)`** → `{decodes_clean, peak_db, source_in,
  duration}`. Use to skip corrupt clips and set audio gain.
- **`get_clip_subtitles(clip_id, max_seconds?)`** → `{cues:[{start,end,text}]}`
  (clip-local) — the film's own dialogue for plain-mode captions.
- `validate_edl(edl)` → `{ok, issues}`. `render_edl(edl, output_path?, resolution?, encoder?)`.

## Reporting

One short line per beat ("beat 9 First Contact → clip_50, 60s, 5 caption lines").
At the end: movie title; mode (plain/commentary); the list of output paths; one
caveat line (clips skipped as corrupt, beats with no dialogue, etc.).

## Copyright note (surface to the user)

Plain clips are the source's own A/V — high Content-ID exposure. `--commentary`
(your VO + ducked audio + your captions) is more transformative and lower risk,
but still no guarantee. For the lowest risk, recommend `movie-recap`.
