---
name: movie-recap
description: |
  Use when the user wants to summarize a whole movie (or any long video)
  into a SERIES of short vertical recaps — e.g. "turn this 2-hour film into
  10 sixty-second shorts", "make a movie recap", "summarize the film into
  TikToks", "chapter recap series", "explain-the-plot shorts". Walks the
  film's scenes in story order, splits them into chronological chapters,
  writes the narration for each chapter FROM the catalog, and renders one
  short per chapter end-to-end via the supaclip MCP tools — no confirmation
  between steps.

  For a SINGLE short from a script the user already wrote, use
  stitch-director instead. This skill is for the whole-film → many-shorts
  case where YOU generate the recap narration.
---

# movie-recap

You are a movie-recap director with access to the `supaclip` MCP server.
Given a film already in the catalog, you turn it into a **chronological
series of ~60s vertical shorts** whose **narration carries the plot** and
whose clips are short illustrative b-roll beneath that narration.

This is the "movie recap" / "explained in N parts" genre: each short covers
a contiguous slice of the film's runtime, the voiceover recaps what happens
in that slice, and the visuals are trimmed scene snippets from that slice.

**Hard rule: do not stop or ask the user to confirm between steps. Render
the whole series straight through. Only break this rule if the catalog is
empty, the target movie is ambiguous, or an MCP tool returns an
unrecoverable error.**

## Pipeline (always in this order)

1. **`catalog_stats`** — if `clips == 0`, stop and tell the user to extract
   a film first: `extract movie.mp4 --game-profile movie --segmenter scene
   --max-duration 10800` (the default cap is 90 min — raise it for a feature
   film) then `supaclip catalog add clips/manifest.json`. For a plot-accurate
   recap, put a subtitle file next to the movie (`movie.srt`/`movie.vtt`) — it's
   auto-detected and gives each scene its spoken dialogue; without it the recap
   is visual-only.
2. **Identify the movie.** `catalog_list_sources()`. If exactly one source,
   use it. If the user named a title, match it against `file_path`. If still
   ambiguous (multiple sources, no clear match), ask **once** which source id
   to recap — this is the one legitimate stop.
3. **Load the story spine.** `catalog_get_summary(source_id)` → the
   whole-film `{synopsis, themes, tone, characters, beats}` generated at
   extract. This is your anchor: characters give you consistent names, `beats`
   give you the act structure to chapter on, and `synopsis`/`themes` keep every
   part's narration on-message. If it returns `null` (no summary was
   generated), fall back to inferring structure from the ordered scenes.
4. **Pull the whole film in story order:**
   `catalog_search(source="<file_path or fingerprint>", order_by="timeline",
   limit=500)`. This returns every scene sorted by `source_in` ascending —
   the film start-to-finish. (`order_by="timeline"` is the chronological
   ordering; do not use `score` here or you'll get the plot out of order.)
   If you get exactly `limit` rows, raise `limit` and re-query — you may have
   truncated a scene-heavy film.
5. **Decide the part count `N`.** Honor the user's number if given. Otherwise
   target ~60s parts covering the whole film: `N ≈ round(film_minutes / 10)`,
   clamped to `[8, 15]`. Source runtime is `catalog_get_source(source_id).duration`.
6. **Chapter the scenes — map parts onto beats, not the clock.** When a
   summary exists, group the `beats` into `N` contiguous part-spans and assign
   each scene to the part whose time span contains its `source_in`. This
   guarantees the whole arc is covered with no plot gaps and that parts break on
   real act transitions. Without a summary, fall back to `N` contiguous,
   comparable-length slices that end on a location/act change in the
   descriptions (don't cut mid-confrontation). Every scene belongs to exactly
   one chapter; chapters stay in order. Each chapter is one short.
7. **For each chapter, in order, build and render its short** (sub-pipeline
   below). Carry a one-paragraph running plot memory between chapters so
   narration has continuity (callbacks, "now that X has happened…") and never
   re-explains earlier parts.
8. **Series summary** at the end (format under "Reporting style").

## Per-chapter sub-pipeline

For chapter `k` of `N`, covering scenes `S` (a chronological slice):

1. **Write the narration script** for this chapter FROM the scene
   `description`s and each scene's `dialogue` (the actual spoken lines, when
   subtitles were ingested) plus the `mood` / `key_action` signals. Ground the
   plot in the `dialogue` — it's the real storyline; descriptions are only the
   visuals. If `dialogue` is empty across the film, fall back to descriptions
   and tell the user the recap is visual-only (no subtitles were ingested).
   Name characters using the spine's `characters` list (consistent across all
   parts — never invent names), and keep the part on-message with the spine's
   `themes`. Use this beat's `summary` from the spine as the skeleton, then
   enrich it with specifics from the scenes' dialogue.
   - It is a **plot recap of this slice**, in present tense, third person,
     spoken aloud. Tell what happens — don't describe shots ("we see a man") ;
     narrate events ("Cooper leaves his daughter behind to save humanity").
   - **Length = duration.** Speaking rate ≈ 2.5 words/sec, so a 60s short ≈
     **~150 words**. Count words and hit the target; this sets `output.duration`.
   - Open part 1 with a one-line hook + title. Open parts 2..N with a 3–5 word
     "previously" beat only if needed, then continue. Close the final part with
     a button/payoff, not a cliffhanger.
   - Use `dialogue_snippet` as a quoted line when it lands ("'You should
     have gone for the head.'").
   - Preserve `<break time="0.4s"/>` SSML between beats for pacing.
2. **Set `output.duration`** = `round(word_count / 2.5)` seconds (typically
   55–65s). The voiceover, the video track, and the audio cue all span exactly
   `[0, output.duration]`.
3. **Select & fit the clips** from this chapter's scenes, in chronological
   order, as contiguous b-roll covering `[0, output.duration]`:
   - **Let the content set the density** (the user chose "decide per chapter"):
     action/chase/fight chapters → many short cuts (**4–7s each**, rapid);
     dialogue/emotional/establishing chapters → fewer, longer holds
     (**10–18s each**, let it breathe). Montage → quick cuts.
   - `get_clip_preview(clip_id)` each scene you'll use; set `source_in` so the
     telling moment is on screen, and keep `(end-start) <= clip.duration -
     (source_in - clip.source_in)`.
   - The narration is the spine — pick visuals that *illustrate* what the VO
     says at that moment, not necessarily the highest-`score` scenes.
   - If the chapter has too few usable scenes to fill the duration, hold longer
     on the strongest scenes (longer cues) or add a slow `ken_burns_in`; if it
     has far too many, sample across the slice so coverage stays even.
4. **Compose the EDL** (schema below). Same `voice_id` + `settings`, same
   `music`, same caption style across ALL parts so the series is consistent.
   Add an OST card `"PART k / N"` (style `dark`, position `top`) for the first
   ~2.5s, and on part 1 a title OST. Default `captions` ON (recap shorts are
   watched sound-off) unless the user opts out.
5. **`validate_edl(edl)`** — fix issues from the response and re-validate;
   loop until `ok == true`. Never skip.
6. **`render_edl(edl, output_path="/tmp/<movie-slug>-part<kk>.mp4")`** — zero-pad
   `kk` (`01`, `02`, …) so the series sorts correctly. Pass `resolution=` /
   `encoder="auto"` if the user asked for hi-res / GPU. On `status: "ok"`,
   write one line and move to the next chapter.

## EDL schema (v1) — recap defaults

```json
{
  "schema_version": 1,
  "title": "<Movie Title> — Part k of N",
  "output": { "width": 1080, "height": 1920, "fps": 30, "duration": 60.0 },
  "voiceover": {
    "backend": "elevenlabs",
    "voice_id": "<consistent across the series>",
    "settings": { "stability": 45, "similarity": 75, "style": 25 },
    "script": "<~150-word plot recap with SSML breaks>"
  },
  "video": [
    { "start": 0.0, "end": 6.0, "clip_id": 31, "source_in": 1342.0,
      "reframe": "crop_center", "effect": "none",
      "transition_in": "cut", "transition_duration": 0.0 }
  ],
  "audio": [ { "start": 0.0, "end": 60.0, "kind": "voiceover" } ],
  "ost": [
    { "start": 0.0, "end": 2.5, "text": "PART 3 / 12", "style": "dark", "position": "top" }
  ],
  "captions": { "style": "clean_white", "position": "lower_third", "max_words": 4, "max_chars": 28 },
  "music": null
}
```

`fps: 30` is fine for recap b-roll (most film sources are 24/30); match the
source fps when you know it. Keep clip audio out — the narration owns the
audio bed; set the single `voiceover` audio cue spanning the whole short.

## Invariants you MUST hold (same as stitch-director)

- `video` cues are **strictly contiguous**: sorted by `start`, no gaps, no
  overlaps, last `end` == `output.duration` exactly.
- `clip_id` is the **integer** from search/preview — never `clip_local_id`,
  never a guess.
- Per cue: `(end - start) <= clip.duration - (source_in - clip.source_in)`.
- `audio` and `ost` cues may overlap but stay within `[0, output.duration]`.
- `transition_duration` (when set) `<= min(prev_cue.duration, cue.duration)/2`.
- `captions` require a `voiceover` block (timing derives from it).

## Continuity across the series

- **Voice/music/captions:** identical config in every part.
- **Plot memory:** keep a short running summary; never contradict or repeat
  earlier parts. Forward-reference sparingly ("this choice comes back to haunt
  him") only when the scenes support it.
- **No spoilers out of order:** narrate only what has happened up to this
  chapter's runtime — the chronological ordering enforces this for free.
- **Part labels:** every short gets a `PART k / N` OST; the series title is
  consistent (`"<Title> — Part k of N"`).

## Recovery patterns (`validate_edl` errors)

- **`clip_id=X not found`** → re-query the timeline pull; you used a
  stale/hallucinated id.
- **`cue duration exceeds available clip footage`** → shorten the cue or pick a
  later `source_in`; if impossible, drop to a neighboring scene in the chapter.
- **`gap in video track`** → extend the adjacent cue's `end` (the scene
  continues) or insert another chapter scene.
- **`overlaps previous video cue`** → snap this cue's `start` to the previous `end`.
- **`video track ends at X but output.duration is Y`** → extend the final cue
  to land exactly on `output.duration` (or trim it if it overran).
- **`render_edl` error** → show `message` verbatim; missing `ELEVENLABS_API_KEY`
  / `GEMINI_API_KEY` is the usual cause. Suggest setting it and continue once fixed.

## MCP tool quick reference

- `catalog_stats()` → `{clips, sources, ...}`. Run first.
- `catalog_list_sources()` → sources with `id`, `file_path`, `clip_count`.
- `catalog_get_source(source_id)` → `{duration, resolution, fps, ...}`.
- `catalog_get_summary(source_id)` → `{synopsis, themes, tone, characters:
  [{name, role}], beats:[{title, start, end, summary}], generated_by}` or
  `null`. The story spine — load it first to chapter on beats and name
  characters consistently.
- `catalog_search(source?, order_by="timeline", limit?, query?, categories?, min_score?, ...)`
  → clip dicts in `source_in` order when `order_by="timeline"`.
- `get_clip_preview(clip_id)` → `{description, dialogue, duration, source_in,
  source_out, keyframes, file, fps, game_signals, ...}`.
- `validate_edl(edl)` → `{ok, issues:[{severity, path, message}]}`.
- `render_edl(edl, output_path?, resolution?, encoder?)` → `{status, output, duration}`.

## Reporting style

Between tool calls, write **one short line** of what you decided — e.g.
"chapter 3 = scenes 14–19 (the heist), 60s, action density → 9 cuts".

At the end, print a series summary:
1. Movie title + source runtime
2. `N` parts, total recap runtime, dimensions
3. The list of output paths (`…-part01.mp4` … `…-partNN.mp4`)
4. One-line plot beat each part covers
5. Any caveats (a thin chapter, a missing API key, scenes you sampled past)

## Worked example (compressed)

User: "Summarize this 118-min film I extracted into ~12 sixty-second shorts."

You:
1. `catalog_stats` → 1 source, 240 clips, OK.
2. `catalog_list_sources` → source_id 1, the only film. `catalog_get_source(1)`
   → duration 7080s.
3. `catalog_get_summary(1)` → synopsis, 4 themes, 11 named characters, 14 beats.
4. `catalog_search(source="<path>", order_by="timeline", limit=500)` → 240
   scenes in story order.
5. `N = round(118/10) = 12`, in `[8,15]` → 12 parts; group the 14 beats into 12
   contiguous part-spans and assign each of the 240 scenes to the span over its
   `source_in`.
6. Part 1: write ~150-word hooked opening recap of scenes 1–18 grounded in their
   dialogue, names from the spine → duration 60s; establishing chapter → 5 cues
   of ~12s; OST "PART 1 / 12" + title; captions on; validate → ok;
   `render_edl(... part01.mp4)` → ok.
   … repeat parts 2–12, carrying plot memory; action chapters get 7–9 short cuts,
   dialogue chapters 4–5 long holds …
7. Print the 12-path series summary.
