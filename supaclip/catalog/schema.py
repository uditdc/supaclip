from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY,
    file_path   TEXT NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,
    duration    REAL NOT NULL,
    resolution  TEXT NOT NULL,
    fps         REAL NOT NULL,
    has_audio   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS extracts (
    id            INTEGER PRIMARY KEY,
    source_id     INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    segmenter     TEXT NOT NULL,
    analyzer      TEXT NOT NULL,
    game_profile  TEXT NOT NULL,
    taxonomy_json TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    UNIQUE (source_id, created_at, segmenter, analyzer, game_profile)
);
CREATE INDEX IF NOT EXISTS idx_extracts_source ON extracts(source_id);

CREATE TABLE IF NOT EXISTS clips (
    id                INTEGER PRIMARY KEY,
    extract_id        INTEGER NOT NULL REFERENCES extracts(id) ON DELETE CASCADE,
    clip_local_id     TEXT NOT NULL,
    file              TEXT NOT NULL,
    source_in         REAL NOT NULL,
    source_out        REAL NOT NULL,
    duration          REAL NOT NULL,
    resolution        TEXT NOT NULL,
    fps               REAL NOT NULL,
    description       TEXT NOT NULL,
    score             INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
    segment_source    TEXT NOT NULL,
    game_signals_json TEXT NOT NULL DEFAULT '{}',
    audio_json        TEXT NOT NULL DEFAULT '{}',
    keyframes_json    TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_clips_extract ON clips(extract_id);
CREATE INDEX IF NOT EXISTS idx_clips_score   ON clips(score);

CREATE TABLE IF NOT EXISTS clip_categories (
    clip_id  INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    PRIMARY KEY (clip_id, category)
);
CREATE INDEX IF NOT EXISTS idx_clip_categories_category ON clip_categories(category);

CREATE VIRTUAL TABLE IF NOT EXISTS clips_fts USING fts5(
    description,
    audio_cues,
    tags,
    tokenize='porter unicode61'
);
"""


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    current = _read_version(conn)
    if current is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    elif current < SCHEMA_VERSION:
        conn.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION),),
        )
    conn.commit()


def _read_version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None
