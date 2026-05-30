from __future__ import annotations

import base64
import json
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from supaclip.core.ffmpeg import run_ffmpeg
from supaclip.stitch.tts.base import Alignment, TTSBackend, Voice

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3.1-flash-tts-preview"

PCM_SAMPLE_RATE = 24000

# Prebuilt voices offered by the Gemini speech-generation API. The API has no
# endpoint to enumerate them, so the catalog is maintained here.
PREBUILT_VOICES = [
    ("Zephyr", "Bright"), ("Puck", "Upbeat"), ("Charon", "Informative"),
    ("Kore", "Firm"), ("Fenrir", "Excitable"), ("Leda", "Youthful"),
    ("Orus", "Firm"), ("Aoede", "Breezy"), ("Callirrhoe", "Easy-going"),
    ("Autonoe", "Bright"), ("Enceladus", "Breathy"), ("Iapetus", "Clear"),
    ("Umbriel", "Easy-going"), ("Algieba", "Smooth"), ("Despina", "Smooth"),
    ("Erinome", "Clear"), ("Algenib", "Gravelly"), ("Rasalgethi", "Informative"),
    ("Laomedeia", "Upbeat"), ("Achernar", "Soft"), ("Alnilam", "Firm"),
    ("Schedar", "Even"), ("Gacrux", "Mature"), ("Pulcherrima", "Forward"),
    ("Achird", "Friendly"), ("Zubenelgenubi", "Casual"),
    ("Vindemiatrix", "Gentle"), ("Sadachbia", "Lively"),
    ("Sadaltager", "Knowledgeable"), ("Sulafat", "Warm"),
]


class GoogleTTSError(RuntimeError):
    pass


class GoogleBackend(TTSBackend):
    """Gemini speech-generation TTS (Google AI Studio).

    See https://ai.google.dev/gemini-api/docs/speech-generation. Voices are
    prebuilt and selected by `voice_id` (e.g. "Kore"); delivery style is steered
    through the script text itself (e.g. 'Say cheerfully: ...') rather than
    numeric settings. The API returns raw 24kHz mono PCM, converted to the same
    48kHz stereo wav the rest of the pipeline expects.

    Gemini returns no timestamps, so captions are derived by local forced
    alignment (the optional `align` extra); see supaclip.stitch.tts.align.
    """

    name = "google"

    def __init__(
        self,
        api_key: str | None = None,
        model_id: str | None = None,
        api_root: str = API_ROOT,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        self.model_id = model_id or os.environ.get("GEMINI_TTS_MODEL") or DEFAULT_MODEL
        self.api_root = api_root.rstrip("/")
        self._opener = opener or urllib.request.build_opener()

    def _require_key(self) -> str:
        if not self.api_key:
            raise GoogleTTSError(
                "Google AI Studio API key not set. Pass --api-key or set GEMINI_API_KEY."
            )
        return self.api_key

    def synthesize(
        self,
        text: str,
        voice_id: str,
        settings: dict[str, float],
        out_path: str | Path,
    ) -> Path:
        pcm_bytes = self._request_pcm(text, voice_id, settings)
        return self._pcm_to_wav(pcm_bytes, out_path)

    def synthesize_with_alignment(
        self,
        text: str,
        voice_id: str,
        settings: dict[str, float],
        out_path: str | Path,
    ) -> tuple[Path, Alignment]:
        from supaclip.stitch.tts.align import align_text_to_audio

        pcm_bytes = self._request_pcm(text, voice_id, settings)
        out = self._pcm_to_wav(pcm_bytes, out_path)
        alignment = align_text_to_audio(out, text)
        return out, alignment

    def list_voices(self) -> list[Voice]:
        return [Voice(voice_id=name, name=name, description=desc)
                for name, desc in PREBUILT_VOICES]

    def _request_pcm(self, text: str, voice_id: str, settings: dict[str, float]) -> bytes:
        key = self._require_key()
        generation_config: dict[str, object] = {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": voice_id},
                },
            },
        }
        if "temperature" in settings:
            generation_config["temperature"] = float(settings["temperature"])
        body = {
            "contents": [{"parts": [{"text": text}]}],
            "generationConfig": generation_config,
        }
        req = urllib.request.Request(
            f"{self.api_root}/models/{self.model_id}:generateContent",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "x-goog-api-key": key,
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener.open(req) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            raise GoogleTTSError(f"Google AI Studio HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise GoogleTTSError(f"Google AI Studio request failed: {e.reason}") from e

        audio_b64 = _extract_audio_b64(payload)
        if not audio_b64:
            raise GoogleTTSError(f"Google AI Studio response missing audio data: {payload}")
        return base64.b64decode(audio_b64)

    def _pcm_to_wav(self, pcm_bytes: bytes, out_path: str | Path) -> Path:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".pcm", delete=False) as tmp:
            tmp.write(pcm_bytes)
            tmp_path = tmp.name
        try:
            run_ffmpeg([
                "-f", "s16le", "-ar", str(PCM_SAMPLE_RATE), "-ac", "1",
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


def _extract_audio_b64(payload: dict) -> str | None:
    for candidate in payload.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            data = part.get("inlineData") or part.get("inline_data")
            if data and data.get("data"):
                return data["data"]
    return None
