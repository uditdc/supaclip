# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Hardware encoding.** `stitch render --encoder` supports NVENC, VideoToolbox,
  and QSV (plus libx264/libx265). `--encoder auto` functionally probes GPU
  encoders and falls back to `libx264`. New `stitch encoders` subcommand lists
  usable encoders; `render_edl` MCP tool gains an `encoder` argument.
- **Resolution presets.** `stitch render --resolution {720p,1080p,1440p,2160p,4k}`
  scales the whole composition by its short side, scaling annotation geometry,
  reframe offsets, and font sizes with it. Also exposed via `render_edl`.
- **`--preset` / `--crf` flags** on `stitch render` for encoder rate control.

### Changed
- **Circle annotations** now render as a true ring outline (PIL PNG overlay)
  instead of a square `drawbox` bounding box.
- **`reframe_offset`** is now applied as a clamped horizontal crop pan (it was
  previously parsed but ignored).

## [0.1.0] — Alpha

Initial pipeline.

### Added
- **`extract`** — segment a local video (auto / manual / scene / interval) and
  analyze each segment with a pluggable vision-language backend (sampled frames
  or native video over OpenAI-compatible / Google AI Studio endpoints). Emits
  native-aspect master clips and a `manifest.json`.
- **`catalog`** — global SQLite library with FTS5 full-text and structured
  (`category`, `signal`, score, duration) search across all extracted clips.
- **`stitch`** — render vertical short-form videos from an EDL: 9:16 reframe,
  concat/crossfade, Ken-Burns, freeze frame, slow-mo, styled on-screen text,
  box/arrow annotations, speech-synced captions, and a ducked music bed.
- **TTS** — ElevenLabs and Google (Gemini) voiceover backends with on-disk
  caching; local forced alignment for caption timing when the backend returns
  audio only.
- **MCP server** (`supaclip-mcp`) and a `stitch-director` Claude Code skill that
  drive the catalog → EDL → render flow.

[Unreleased]: https://github.com/uditdc/supaclip/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/uditdc/supaclip/releases/tag/v0.1.0
