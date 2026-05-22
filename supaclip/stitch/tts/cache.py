from __future__ import annotations

import hashlib
import json
from pathlib import Path


class TTSCache:
    """File-cache for synthesized voiceover wavs.

    Key is sha1 over (backend, voice_id, normalized settings, text). The cached
    artifact is a .wav at <root>/tts/<hash>.wav.
    """

    def __init__(self, root: str | Path, enabled: bool = True) -> None:
        self.root = Path(root).expanduser() / "tts"
        self.enabled = enabled
        if self.enabled:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.enabled = False

    @staticmethod
    def key(backend: str, voice_id: str, settings: dict[str, float], text: str) -> str:
        payload = json.dumps(
            {"backend": backend, "voice_id": voice_id,
             "settings": {k: round(float(v), 6) for k, v in sorted(settings.items())},
             "text": text},
            sort_keys=True,
        )
        return hashlib.sha1(payload.encode()).hexdigest()

    def path_for(self, key: str) -> Path:
        return self.root / f"{key}.wav"

    def path_for_alignment(self, key: str) -> Path:
        return self.root / f"{key}.alignment.json"

    def get(self, key: str) -> Path | None:
        if not self.enabled:
            return None
        p = self.path_for(key)
        return p if p.exists() else None

    def put(self, key: str, src: str | Path) -> Path | None:
        if not self.enabled:
            return None
        dst = self.path_for(key)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            Path(src).replace(dst)
            return dst
        except OSError:
            try:
                from shutil import copyfile
                copyfile(src, dst)
                return dst
            except OSError:
                return None

    def get_alignment(self, key: str) -> dict | None:
        if not self.enabled:
            return None
        p = self.path_for_alignment(key)
        if not p.exists():
            return None
        try:
            with p.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None

    def put_alignment(self, key: str, data: dict) -> Path | None:
        if not self.enabled:
            return None
        dst = self.path_for_alignment(key)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            with dst.open("w", encoding="utf-8") as fh:
                json.dump(data, fh)
            return dst
        except OSError:
            return None
