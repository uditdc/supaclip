from __future__ import annotations

import os
from pathlib import Path


def default_catalog_path() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "supaclip" / "catalog.db"


def resolve_catalog_path(flag_value: str | None = None) -> Path:
    if flag_value:
        return Path(flag_value).expanduser().resolve()
    env = os.environ.get("CLIPPER_CATALOG")
    if env:
        return Path(env).expanduser().resolve()
    return default_catalog_path()
