from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import Any

from ..core.ffmpeg import measure_peak_db, segment_decodes_clean
from ..extract.subtitles import cues_for_range, load_for_video
from .db import connect
from .paths import resolve_catalog_path
from .search import (
    ClipRow,
    get_clip,
    get_source,
    get_source_summary,
    list_sources,
    search,
    stats,
)


def _catalog_path() -> Path:
    return resolve_catalog_path(os.environ.get("SUPACLIP_CATALOG"))


def _clip_to_dict(c: ClipRow) -> dict[str, Any]:
    return c.to_dict()


def _build_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:
        raise SystemExit(
            "MCP support not installed. Install with: pip install -e \".[mcp]\"\n"
            f"({e})"
        ) from e

    server = FastMCP("supaclip")
    catalog_path = _catalog_path()

    def _conn():
        return contextlib.closing(connect(catalog_path))

    @server.tool()
    def catalog_search(
        query: str | None = None,
        categories: list[str] | None = None,
        all_categories: bool = False,
        min_score: int | None = None,
        max_score: int | None = None,
        min_duration: float | None = None,
        max_duration: float | None = None,
        segmenter: str | None = None,
        game_profile: str | None = None,
        source: str | None = None,
        signals: list[str] | None = None,
        order_by: str = "score",
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Search clips across the catalog.

        `query` is an FTS5 expression over description, audio_cues, and tags.
        `categories` filters by category (OR by default; AND if all_categories).
        `signals` is a list of "key=value" or "key~=value" filters over each
        clip's game_signals JSON.
        `order_by` is one of "score" (default), "duration", "created_at", or
        "timeline" (chronological by source_in) — use "timeline" with a `source`
        filter to walk a film's scenes in story order.
        Returns a list of clip dicts including absolute file/keyframe paths.
        """
        from .search import parse_signal_filter

        parsed_signals = [parse_signal_filter(s) for s in signals or []]
        with _conn() as conn:
            rows = search(
                conn,
                query=query,
                categories=categories,
                all_categories=all_categories,
                min_score=min_score,
                max_score=max_score,
                min_duration=min_duration,
                max_duration=max_duration,
                segmenter=segmenter,
                game_profile=game_profile,
                source=source,
                signals=parsed_signals or None,
                order_by=order_by,
                limit=limit,
            )
            return [_clip_to_dict(r) for r in rows]

    @server.tool()
    def catalog_get_clip(clip_id: int) -> dict[str, Any] | None:
        """Fetch a single clip by its catalog id."""
        with _conn() as conn:
            row = get_clip(conn, clip_id)
            return _clip_to_dict(row) if row else None

    @server.tool()
    def catalog_get_source(source_id: int) -> dict[str, Any] | None:
        """Fetch a source video by its catalog id."""
        with _conn() as conn:
            return get_source(conn, source_id)

    @server.tool()
    def catalog_list_sources() -> list[dict[str, Any]]:
        """List every source video in the catalog with extract/clip counts."""
        with _conn() as conn:
            return list_sources(conn)

    @server.tool()
    def catalog_get_summary(source_id: int) -> dict[str, Any] | None:
        """Whole-film story spine for a source, if one was generated at extract.

        Returns {synopsis, themes, tone, characters:[{name, role}],
        beats:[{title, start, end, summary}], generated_by} — use this to anchor
        a full-arc recap (chapter on the beats, name characters consistently).
        Returns None when the source has no stored summary.
        """
        with _conn() as conn:
            return get_source_summary(conn, source_id)

    @server.tool()
    def catalog_stats() -> dict[str, Any]:
        """Row counts and DB size."""
        with _conn() as conn:
            s = stats(conn)
        size = catalog_path.stat().st_size if catalog_path.exists() else 0
        return {"catalog": str(catalog_path), "size_bytes": size, **s}

    @server.tool()
    def probe_clip(clip_id: int, max_seconds: float = 60.0) -> dict[str, Any] | None:
        """Pre-flight a clip's media before using it in an EDL.

        Real-world movie rips have corrupt H.264/AAC regions that decode
        tolerantly but abort a render. Returns `decodes_clean` (skip the clip
        if false — pick another in the same beat) and `peak_db` (to set a
        constant audio gain, e.g. level_db = target_peak - peak_db). Probes the
        first `max_seconds` of the clip.
        """
        with _conn() as conn:
            row = get_clip(conn, clip_id)
            if row is None:
                return None
            probe_dur = min(float(row.duration), float(max_seconds))
            return {
                "clip_id": row.clip_id,
                "source_in": row.source_in,
                "duration": row.duration,
                "probe_seconds": round(probe_dur, 3),
                "decodes_clean": segment_decodes_clean(row.file, row.source_in, probe_dur),
                "peak_db": measure_peak_db(row.file, row.source_in, probe_dur),
            }

    @server.tool()
    def get_clip_subtitles(clip_id: int, max_seconds: float = 60.0) -> dict[str, Any] | None:
        """The source film's own subtitle lines within a clip's window, re-timed
        to clip-local coordinates (start 0).

        Feed these into `EDLCaptions.cues` to burn the film's real dialogue,
        synced, in our caption style (movie-clips). Empty `cues` if the source
        has no subtitles.
        """
        with _conn() as conn:
            row = get_clip(conn, clip_id)
            if row is None:
                return None
            cues, source = load_for_video(row.file)
            window = min(float(row.duration), float(max_seconds))
            scoped = cues_for_range(cues, row.source_in, row.source_in + window)
            return {
                "clip_id": row.clip_id,
                "source": source,
                "cues": [{"start": c.start, "end": c.end, "text": c.text} for c in scoped],
            }

    @server.tool()
    def get_clip_preview(clip_id: int) -> dict[str, Any] | None:
        """Compact preview of a clip for EDL composition.

        Returns the fields Claude needs to decide whether a clip fits a b-roll
        cue: description, dialogue (spoken lines in the scene, if subtitles were
        ingested), categories, duration, score, keyframe_paths, source file, and
        source_in/source_out (so that EDLVideoCue.source_in can be set correctly).
        """
        with _conn() as conn:
            row = get_clip(conn, clip_id)
            if row is None:
                return None
            return {
                "clip_id": row.clip_id,
                "clip_local_id": row.clip_local_id,
                "description": row.description,
                "dialogue": row.dialogue,
                "categories": row.categories,
                "duration": row.duration,
                "score": row.score,
                "source_in": row.source_in,
                "source_out": row.source_out,
                "source_file": row.source_file,
                "file": row.file,
                "keyframes": row.keyframes,
                "game_signals": row.game_signals,
                "audio": row.audio,
                "resolution": row.resolution,
                "fps": row.fps,
            }

    @server.tool()
    def validate_edl(edl: dict[str, Any]) -> dict[str, Any]:
        """Validate an EDL (gaps, overlaps, clip refs, cue durations).

        Pass the full EDL JSON. Returns {ok, issues:[{severity,path,message}]}.
        Run this before render_edl to catch problems early.
        """
        from supaclip.core.edl import EDL
        from supaclip.core.edl import validate_edl as _v

        try:
            parsed = EDL.model_validate(edl)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "issues": [
                {"severity": "error", "path": "<root>", "message": str(e)}
            ]}
        with _conn() as conn:
            issues = _v(parsed, resolver=lambda cid: get_clip(conn, cid))
        return {
            "ok": not any(i.severity == "error" for i in issues),
            "issues": [i.to_dict() for i in issues],
        }

    @server.tool()
    def render_edl(
        edl: dict[str, Any] | None = None,
        edl_path: str | None = None,
        output_path: str | None = None,
        resolution: str | None = None,
        encoder: str = "libx264",
    ) -> dict[str, Any]:
        """Render an EDL to an mp4 (synthesizes voiceover, reframes,
        concatenates, overlays text, mixes audio).

        Provide either `edl` (the dict) or `edl_path` (a path on disk). If
        `output_path` is omitted, the mp4 is written to a tempfile and the
        path is returned. A sidecar `<output>.edl.json` is always written.

        `resolution` (optional) scales the whole composition by short side:
        one of "720p", "1080p", "1440p", "2160p", "4k". `encoder` selects the
        video encoder ("auto" picks a working GPU encoder, else "libx264").

        Requires a TTS API key in the MCP server's environment if the EDL
        contains a voiceover: `ELEVENLABS_API_KEY` for the default `elevenlabs`
        backend, or `GEMINI_API_KEY` for the `google` backend. May spend TTS
        credits on each call unless the same (text + voice + settings) tuple is
        already cached.
        """
        import json as _json
        import tempfile

        from supaclip.stitch.encode import ENCODER_CHOICES, RESOLUTION_CHOICES
        from supaclip.stitch.render import RenderConfig, render

        if not edl and not edl_path:
            return {"status": "error", "message": "supply edl or edl_path"}
        if resolution is not None and resolution not in RESOLUTION_CHOICES:
            return {"status": "error",
                    "message": f"resolution must be one of {list(RESOLUTION_CHOICES)}"}
        if encoder not in ENCODER_CHOICES:
            return {"status": "error",
                    "message": f"encoder must be one of {list(ENCODER_CHOICES)}"}

        if edl_path:
            path = edl_path
        else:
            fd, path = tempfile.mkstemp(suffix=".json", prefix="edl_")
            with os.fdopen(fd, "w") as fh:
                _json.dump(edl, fh)

        if output_path is None:
            fd, output_path = tempfile.mkstemp(suffix=".mp4", prefix="short_")
            os.close(fd)

        cfg = RenderConfig(
            edl_path=path,
            output_path=output_path,
            catalog_path=str(catalog_path),
            resolution=resolution,
            encoder=encoder,
        )
        try:
            result = render(cfg)
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "message": f"{type(e).__name__}: {e}"}
        return {"status": "ok", "output": result.output,
                "sidecar": result.sidecar, "duration": result.duration}

    return server


def _load_dotenv_if_present() -> None:
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:
        return
    path = find_dotenv(usecwd=True)
    if path:
        load_dotenv(path, override=False)


def main(argv: list[str] | None = None) -> int:
    _load_dotenv_if_present()
    try:
        server = _build_server()
    except SystemExit as e:
        sys.stderr.write(str(e) + "\n")
        return 2
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
