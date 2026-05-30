from supaclip.stitch.tts.base import Alignment, TTSBackend, Voice
from supaclip.stitch.tts.cache import TTSCache

__all__ = ["Alignment", "TTSBackend", "Voice", "TTSCache", "get_backend"]


def get_backend(name: str, *, api_key: str | None = None) -> TTSBackend:
    if name == "elevenlabs":
        from supaclip.stitch.tts.elevenlabs import ElevenLabsBackend
        return ElevenLabsBackend(api_key=api_key)
    if name == "google":
        from supaclip.stitch.tts.google import GoogleBackend
        return GoogleBackend(api_key=api_key)
    raise ValueError(f"unknown TTS backend: {name!r}")
