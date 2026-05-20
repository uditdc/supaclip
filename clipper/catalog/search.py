from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ORDER_FIELDS = {
    "score": "c.score DESC",
    "duration": "c.duration DESC",
    "created_at": "e.created_at DESC",
}


@dataclass
class ClipRow:
    clip_id: int
    clip_local_id: str
    description: str
    score: int
    duration: float
    source_in: float
    source_out: float
    file: str
    keyframes: list[str]
    categories: list[str]
    game_signals: dict[str, Any]
    audio: dict[str, Any]
    segment_source: str
    resolution: str
    fps: float
    extract_id: int
    extract_segmenter: str
    extract_analyzer: str
    extract_game_profile: str
    extract_created_at: str
    manifest_path: str
    source_id: int
    source_file: str
    source_fingerprint: str
    fts_rank: float | None = None
    matched_categories: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_signal_filter(token: str) -> tuple[str, str, str]:
    """`--signal key=value` (exact) or `--signal key~=value` (substring).
    Returns (key, operator, value) where operator is '=' or '~='."""
    if "~=" in token:
        key, value = token.split("~=", 1)
        return key.strip(), "~=", value.strip()
    if "=" in token:
        key, value = token.split("=", 1)
        return key.strip(), "=", value.strip()
    raise ValueError(
        f"--signal must be of form key=value or key~=value, got: {token!r}"
    )


def search(
    conn: sqlite3.Connection,
    *,
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
    signals: list[tuple[str, str, str]] | None = None,
    order_by: str = "score",
    limit: int = 50,
) -> list[ClipRow]:
    where: list[str] = []
    params: list[Any] = []
    joins: list[str] = []

    select_extra = "NULL AS fts_rank"
    if query:
        joins.append("JOIN clips_fts ft ON ft.rowid = c.id")
        where.append("clips_fts MATCH ?")
        params.append(query)
        select_extra = "ft.rank AS fts_rank"

    if categories:
        placeholders = ",".join("?" * len(categories))
        if all_categories:
            where.append(
                f"""c.id IN (
                    SELECT clip_id FROM clip_categories
                    WHERE category IN ({placeholders})
                    GROUP BY clip_id
                    HAVING COUNT(DISTINCT category) = {len(categories)}
                )"""
            )
        else:
            where.append(
                f"""c.id IN (
                    SELECT clip_id FROM clip_categories
                    WHERE category IN ({placeholders})
                )"""
            )
        params.extend(categories)

    if min_score is not None:
        where.append("c.score >= ?")
        params.append(min_score)
    if max_score is not None:
        where.append("c.score <= ?")
        params.append(max_score)
    if min_duration is not None:
        where.append("c.duration >= ?")
        params.append(min_duration)
    if max_duration is not None:
        where.append("c.duration <= ?")
        params.append(max_duration)
    if segmenter:
        where.append("e.segmenter = ?")
        params.append(segmenter)
    if game_profile:
        where.append("e.game_profile = ?")
        params.append(game_profile)
    if source:
        where.append("(s.file_path = ? OR s.fingerprint = ?)")
        params.extend([source, source])

    for key, op, value in signals or []:
        path = f"$.{key}"
        if op == "=":
            where.append(
                "(json_extract(c.game_signals_json, ?) = ? "
                "OR CAST(json_extract(c.game_signals_json, ?) AS TEXT) = ?)"
            )
            params.extend([path, value, path, value])
        else:  # substring
            where.append(
                "CAST(json_extract(c.game_signals_json, ?) AS TEXT) LIKE ?"
            )
            params.extend([path, f"%{value}%"])

    order_sql = ORDER_FIELDS.get(order_by, ORDER_FIELDS["score"])
    if query:
        order_sql = "ft.rank, " + order_sql

    sql = f"""
        SELECT
            c.id            AS clip_id,
            c.clip_local_id AS clip_local_id,
            c.description   AS description,
            c.score         AS score,
            c.duration      AS duration,
            c.source_in     AS source_in,
            c.source_out    AS source_out,
            c.file          AS file,
            c.keyframes_json,
            c.game_signals_json,
            c.audio_json,
            c.segment_source,
            c.resolution    AS resolution,
            c.fps           AS fps,
            e.id            AS extract_id,
            e.segmenter     AS extract_segmenter,
            e.analyzer      AS extract_analyzer,
            e.game_profile  AS extract_game_profile,
            e.created_at    AS extract_created_at,
            e.manifest_path AS manifest_path,
            s.id            AS source_id,
            s.file_path     AS source_file,
            s.fingerprint   AS source_fingerprint,
            {select_extra}
        FROM clips c
        JOIN extracts e ON e.id = c.extract_id
        JOIN sources  s ON s.id = e.source_id
        {' '.join(joins)}
        {('WHERE ' + ' AND '.join(where)) if where else ''}
        ORDER BY {order_sql}
        LIMIT ?
    """
    params.append(int(limit))

    rows = conn.execute(sql, params).fetchall()
    return [_row_to_clip(conn, r) for r in rows]


def get_clip(conn: sqlite3.Connection, clip_id: int) -> ClipRow | None:
    row = conn.execute(
        """
        SELECT
            c.id            AS clip_id,
            c.clip_local_id AS clip_local_id,
            c.description   AS description,
            c.score         AS score,
            c.duration      AS duration,
            c.source_in     AS source_in,
            c.source_out    AS source_out,
            c.file          AS file,
            c.keyframes_json,
            c.game_signals_json,
            c.audio_json,
            c.segment_source,
            c.resolution    AS resolution,
            c.fps           AS fps,
            e.id            AS extract_id,
            e.segmenter     AS extract_segmenter,
            e.analyzer      AS extract_analyzer,
            e.game_profile  AS extract_game_profile,
            e.created_at    AS extract_created_at,
            e.manifest_path AS manifest_path,
            s.id            AS source_id,
            s.file_path     AS source_file,
            s.fingerprint   AS source_fingerprint,
            NULL            AS fts_rank
        FROM clips c
        JOIN extracts e ON e.id = c.extract_id
        JOIN sources s ON s.id = e.source_id
        WHERE c.id = ?
        """,
        (clip_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_clip(conn, row)


def list_sources(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT s.id, s.file_path, s.fingerprint, s.duration, s.resolution,
               s.fps, s.has_audio,
               (SELECT COUNT(*) FROM extracts e WHERE e.source_id = s.id) AS extract_count,
               (SELECT COUNT(*) FROM clips c
                  JOIN extracts e ON e.id = c.extract_id
                  WHERE e.source_id = s.id) AS clip_count
        FROM sources s
        ORDER BY s.id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_source(conn: sqlite3.Connection, source_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, file_path, fingerprint, duration, resolution, fps, has_audio
        FROM sources WHERE id = ?
        """,
        (source_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    out = {}
    for table in ("sources", "extracts", "clips", "clip_categories"):
        out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return out


def _row_to_clip(conn: sqlite3.Connection, row: sqlite3.Row) -> ClipRow:
    clip_id = row["clip_id"] if "clip_id" in row.keys() else row[0]
    categories = [
        r[0]
        for r in conn.execute(
            "SELECT category FROM clip_categories WHERE clip_id = ? ORDER BY category",
            (clip_id,),
        ).fetchall()
    ]
    manifest_dir = Path(row["manifest_path"]).parent
    file_rel = row["file"]
    file_abs = str((manifest_dir / file_rel).resolve()) if not Path(file_rel).is_absolute() else file_rel
    keyframes = json.loads(row["keyframes_json"] or "[]")
    keyframes_abs = [
        str((manifest_dir / kf).resolve()) if not Path(kf).is_absolute() else kf
        for kf in keyframes
    ]
    return ClipRow(
        clip_id=clip_id,
        clip_local_id=row["clip_local_id"],
        description=row["description"],
        score=row["score"],
        duration=row["duration"],
        source_in=row["source_in"],
        source_out=row["source_out"],
        file=file_abs,
        keyframes=keyframes_abs,
        categories=categories,
        game_signals=json.loads(row["game_signals_json"] or "{}"),
        audio=json.loads(row["audio_json"] or "{}"),
        segment_source=row["segment_source"],
        resolution=row["resolution"],
        fps=row["fps"],
        extract_id=row["extract_id"],
        extract_segmenter=row["extract_segmenter"],
        extract_analyzer=row["extract_analyzer"],
        extract_game_profile=row["extract_game_profile"],
        extract_created_at=row["extract_created_at"],
        manifest_path=row["manifest_path"],
        source_id=row["source_id"],
        source_file=row["source_file"],
        source_fingerprint=row["source_fingerprint"],
        fts_rank=row["fts_rank"] if "fts_rank" in row.keys() else None,
        matched_categories=[c for c in categories],
    )
