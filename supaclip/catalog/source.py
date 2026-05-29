from __future__ import annotations

import sqlite3

from supaclip.catalog.db import connect
from supaclip.catalog.paths import resolve_catalog_path
from supaclip.catalog.search import get_clip
from supaclip.core.clips import ClipMetadata


class SqliteClipSource:
    """Default `ClipSource`: resolves clips from the SQLite catalog."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @classmethod
    def open(cls, catalog_path: str | None = None) -> SqliteClipSource:
        return cls(connect(resolve_catalog_path(catalog_path)))

    def get_clip(self, clip_id: int) -> ClipMetadata | None:
        row = get_clip(self._conn, clip_id)
        if row is None:
            return None
        return ClipMetadata(
            clip_id=row.clip_id,
            file=row.file,
            clip_local_id=row.clip_local_id,
            duration=row.duration,
            source_in=row.source_in,
        )
