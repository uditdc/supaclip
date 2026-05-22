from __future__ import annotations

import base64
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from supaclip.core.ffmpeg import run_ffmpeg
from supaclip.stitch.tts.base import (
    Alignment,
    TTSBackend,
    Voice,
    normalize_settings,
)

API_ROOT = "https://api.elevenlabs.io/v1"
DEFAULT_MODEL = "eleven_multilingual_v2"


class ElevenLabsError(RuntimeError):
    pass


class ElevenLabsBackend(TTSBackend):
    name = "elevenlabs"

    def __init__(
        self,
        api_key: str | None = None,
        model_id: str = DEFAULT_MODEL,
        api_root: str = API_ROOT,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("ELEVENLABS_API_KEY")
        self.model_id = model_id
        self.api_root = api_root.rstrip("/")
        self._opener = opener or urllib.request.build_opener()

    def _require_key(self) -> str:
        if not self.api_key:
            raise ElevenLabsError(
                "ElevenLabs API key not set. Pass --api-key or set ELEVENLABS_API_KEY."
            )
        return self.api_key

    def synthesize(
        self,
        text: str,
        voice_id: str,
        settings: dict[str, float],
        out_path: str | Path,
    ) -> Path:
        key = self._require_key()
        body = {
            "text": text,
            "model_id": self.model_id,
            "voice_settings": _to_voice_settings(settings),
        }
        req = urllib.request.Request(
            f"{self.api_root}/text-to-speech/{voice_id}",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "xi-api-key": key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
        )
        try:
            with self._opener.open(req) as resp:
                mp3_bytes = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            raise ElevenLabsError(f"ElevenLabs HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise ElevenLabsError(f"ElevenLabs request failed: {e.reason}") from e

        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(mp3_bytes)
            tmp_path = tmp.name
        try:
            run_ffmpeg([
                "-i", tmp_path,
                "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le",
                str(out),
            ])
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return out

    def synthesize_with_alignment(
        self,
        text: str,
        voice_id: str,
        settings: dict[str, float],
        out_path: str | Path,
    ) -> tuple[Path, Alignment]:
        key = self._require_key()
        body = {
            "text": text,
            "model_id": self.model_id,
            "voice_settings": _to_voice_settings(settings),
        }
        req = urllib.request.Request(
            f"{self.api_root}/text-to-speech/{voice_id}/with-timestamps",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "xi-api-key": key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with self._opener.open(req) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            raise ElevenLabsError(f"ElevenLabs HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise ElevenLabsError(f"ElevenLabs request failed: {e.reason}") from e

        audio_b64 = payload.get("audio_base64")
        if not audio_b64:
            raise ElevenLabsError("ElevenLabs response missing audio_base64")
        mp3_bytes = base64.b64decode(audio_b64)

        align_raw = payload.get("normalized_alignment") or payload.get("alignment")
        if not align_raw or "characters" not in align_raw:
            raise ElevenLabsError("ElevenLabs response missing alignment data")
        alignment = Alignment(
            characters=list(align_raw["characters"]),
            start_times=[float(t) for t in align_raw["character_start_times_seconds"]],
            end_times=[float(t) for t in align_raw["character_end_times_seconds"]],
        )

        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(mp3_bytes)
            tmp_path = tmp.name
        try:
            run_ffmpeg([
                "-i", tmp_path,
                "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le",
                str(out),
            ])
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return out, alignment

    def list_voices(self) -> list[Voice]:
        key = self._require_key()
        req = urllib.request.Request(
            f"{self.api_root}/voices",
            headers={"xi-api-key": key, "Accept": "application/json"},
        )
        try:
            with self._opener.open(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise ElevenLabsError(f"ElevenLabs HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise ElevenLabsError(f"ElevenLabs request failed: {e.reason}") from e
        return [
            Voice(voice_id=v["voice_id"], name=v.get("name", ""),
                  description=v.get("description"))
            for v in data.get("voices", [])
        ]


_KEY_ALIAS = {
    "similarity": "similarity_boost",
    "similarity_boost": "similarity_boost",
    "stability": "stability",
    "style": "style",
    "use_speaker_boost": "use_speaker_boost",
}


def _to_voice_settings(raw: dict[str, float]) -> dict[str, float | bool]:
    normalized = normalize_settings({k: v for k, v in raw.items() if k != "use_speaker_boost"})
    out: dict[str, float | bool] = {}
    for k, v in raw.items():
        canonical = _KEY_ALIAS.get(k, k)
        if canonical == "use_speaker_boost":
            out[canonical] = bool(v)
        else:
            out[canonical] = normalized.get(k, float(v))
    return out


_SSML_BREAK = re.compile(r"<break\s+time=\"(\d+(?:\.\d+)?)(ms|s)\"\s*/?>")


def has_ssml(text: str) -> bool:
    return bool(_SSML_BREAK.search(text))
