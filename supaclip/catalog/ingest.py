from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..core.cache import fingerprint_file
from ..core.manifest import Clip, Manifest, load_manifest


@dataclass
class IngestResult:
    manifest_path: Path
    source_id: int
    extract_id: int
    clip_count: int
    created: bool


def add_manifest(conn: sqlite3.Connection, manifest_path: str | Path) -> IngestResult:
    path = Path(manifest_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"manifest not found: {path}")
    manifest = load_manifest(path)
    return _ingest_one(conn, manifest, path)


def add_directory(conn: sqlite3.Connection, root: str | Path) -> list[IngestResult]:
    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        raise FileNotFoundError(f"directory not found: {root_path}")
    results: list[IngestResult] = []
    for manifest in sorted(root_path.rglob("manifest.json")):
        results.append(add_manifest(conn, manifest))
    return results


def remove_manifest(conn: sqlite3.Connection, manifest_path: str | Path) -> int:
    path = Path(manifest_path).expanduser().resolve()
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT id FROM extracts WHERE manifest_path = ?", (str(path),)
    ).fetchall()
    if not rows:
        return 0
    extract_ids = [r[0] for r in rows]
    placeholders = ",".join("?" * len(extract_ids))
    clip_rowids = [
        r[0]
        for r in cur.execute(
            f"SELECT id FROM clips WHERE extract_id IN ({placeholders})",
            extract_ids,
        ).fetchall()
    ]
    for rowid in clip_rowids:
        cur.execute("DELETE FROM clips_fts WHERE rowid = ?", (rowid,))
    cur.execute(
        f"DELETE FROM extracts WHERE id IN ({placeholders})", extract_ids
    )
    conn.commit()
    return len(extract_ids)


def _ingest_one(
    conn: sqlite3.Connection, manifest: Manifest, manifest_path: Path
) -> IngestResult:
    source_id = _upsert_source(conn, manifest)
    cur = conn.cursor()

    existing = cur.execute(
        """SELECT id FROM extracts
           WHERE source_id=? AND created_at=? AND segmenter=?
             AND analyzer=? AND game_profile=?""",
        (
            source_id,
            manifest.extract.created_at,
            manifest.extract.segmenter,
            manifest.extract.analyzer,
            manifest.extract.game_profile,
        ),
    ).fetchone()

    if existing is None:
        cur.execute(
            """INSERT INTO extracts
               (source_id, segmenter, analyzer, game_profile,
                taxonomy_json, created_at, manifest_path)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                source_id,
                manifest.extract.segmenter,
                manifest.extract.analyzer,
                manifest.extract.game_profile,
                json.dumps(manifest.taxonomy),
                manifest.extract.created_at,
                str(manifest_path),
            ),
        )
        extract_id = cur.lastrowid
        created = True
    else:
        extract_id = existing[0]
        cur.execute(
            """UPDATE extracts
               SET taxonomy_json=?, manifest_path=?
               WHERE id=?""",
            (json.dumps(manifest.taxonomy), str(manifest_path), extract_id),
        )
        _delete_clips_for_extract(cur, extract_id)
        created = False

    for clip in manifest.clips:
        _insert_clip(cur, extract_id, clip)

    if manifest.summary is not None:
        _upsert_summary(cur, source_id, manifest.summary)

    conn.commit()
    return IngestResult(
        manifest_path=manifest_path,
        source_id=source_id,
        extract_id=extract_id,
        clip_count=len(manifest.clips),
        created=created,
    )


def _upsert_source(conn: sqlite3.Connection, manifest: Manifest) -> int:
    source_file = Path(manifest.source.file)
    if source_file.exists():
        fp = fingerprint_file(source_file)
    else:
        fp = f"missing:{source_file}"

    cur = conn.cursor()
    row = cur.execute(
        "SELECT id FROM sources WHERE fingerprint = ?", (fp,)
    ).fetchone()
    has_audio = 1
    if row is not None:
        cur.execute(
            """UPDATE sources
               SET file_path=?, duration=?, resolution=?, fps=?
               WHERE id=?""",
            (
                str(source_file),
                manifest.source.duration,
                manifest.source.resolution,
                manifest.source.fps,
                row[0],
            ),
        )
        return row[0]
    cur.execute(
        """INSERT INTO sources
           (file_path, fingerprint, duration, resolution, fps, has_audio)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            str(source_file),
            fp,
            manifest.source.duration,
            manifest.source.resolution,
            manifest.source.fps,
            has_audio,
        ),
    )
    return cur.lastrowid


def _upsert_summary(cur: sqlite3.Cursor, source_id: int, summary) -> None:
    cur.execute(
        """INSERT OR REPLACE INTO source_summaries
           (source_id, synopsis, themes_json, tone, characters_json,
            beats_json, generated_by)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            source_id,
            summary.synopsis,
            json.dumps(summary.themes),
            summary.tone,
            json.dumps([c.model_dump() for c in summary.characters]),
            json.dumps([b.model_dump() for b in summary.beats]),
            summary.generated_by,
        ),
    )


def _delete_clips_for_extract(cur: sqlite3.Cursor, extract_id: int) -> None:
    rows = cur.execute(
        "SELECT id FROM clips WHERE extract_id = ?", (extract_id,)
    ).fetchall()
    for (rowid,) in rows:
        cur.execute("DELETE FROM clips_fts WHERE rowid = ?", (rowid,))
    cur.execute("DELETE FROM clips WHERE extract_id = ?", (extract_id,))


def _insert_clip(cur: sqlite3.Cursor, extract_id: int, clip: Clip) -> None:
    cur.execute(
        """INSERT INTO clips
           (extract_id, clip_local_id, file, source_in, source_out, duration,
            resolution, fps, description, dialogue, score, segment_source,
            game_signals_json, audio_json, keyframes_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            extract_id,
            clip.id,
            clip.file,
            clip.source_in,
            clip.source_out,
            clip.duration,
            clip.resolution,
            clip.fps,
            clip.description,
            clip.dialogue,
            clip.score,
            clip.segment_source,
            json.dumps(clip.game_signals),
            json.dumps(clip.audio.model_dump()),
            json.dumps(clip.keyframes),
        ),
    )
    rowid = cur.lastrowid
    for category in clip.categories:
        cur.execute(
            "INSERT OR IGNORE INTO clip_categories(clip_id, category) VALUES (?, ?)",
            (rowid, category),
        )
    audio_cues = " ".join(clip.audio.cues or [])
    tags = _build_tags(clip)
    cur.execute(
        "INSERT INTO clips_fts(rowid, description, dialogue, audio_cues, tags) "
        "VALUES (?, ?, ?, ?, ?)",
        (rowid, clip.description, clip.dialogue, audio_cues, tags),
    )


def _build_tags(clip: Clip) -> str:
    parts: list[str] = []
    parts.extend(clip.categories)
    for value in clip.game_signals.values():
        parts.extend(_stringify(value))
    return " ".join(p for p in parts if p)


def _stringify(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for v in value:
            out.extend(_stringify(v))
        return out
    if isinstance(value, dict):
        out = []
        for v in value.values():
            out.extend(_stringify(v))
        return out
    return [str(value)]
