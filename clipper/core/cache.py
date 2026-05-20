from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


def fingerprint_file(path: str | os.PathLike[str]) -> str:
    p = Path(path).resolve()
    stat = p.stat()
    h = hashlib.sha1()
    h.update(str(p).encode())
    h.update(str(stat.st_size).encode())
    h.update(str(int(stat.st_mtime)).encode())
    return h.hexdigest()


def _hash_key(parts: tuple[Any, ...]) -> str:
    payload = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode()).hexdigest()


class Cache:
    def __init__(self, root: str | os.PathLike[str], enabled: bool = True) -> None:
        self.root = Path(root).expanduser()
        self.enabled = enabled
        if self.enabled:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.enabled = False

    def _path(self, namespace: str, key: str) -> Path:
        return self.root / namespace / f"{key}.json"

    def get(self, namespace: str, key_parts: tuple[Any, ...]) -> Any | None:
        if not self.enabled:
            return None
        try:
            p = self._path(namespace, _hash_key(key_parts))
            if not p.exists():
                return None
            with p.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None

    def set(self, namespace: str, key_parts: tuple[Any, ...], value: Any) -> None:
        if not self.enabled:
            return
        try:
            p = self._path(namespace, _hash_key(key_parts))
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(value, fh)
            tmp.replace(p)
        except OSError:
            return
