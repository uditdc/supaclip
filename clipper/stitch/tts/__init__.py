from clipper.stitch.tts.base import TTSBackend, Voice
from clipper.stitch.tts.cache import TTSCache

__all__ = ["TTSBackend", "Voice", "TTSCache", "get_backend"]


def get_backend(name: str, *, api_key: str | None = None) -> TTSBackend:
    if name == "elevenlabs":
        from clipper.stitch.tts.elevenlabs import ElevenLabsBackend
        return ElevenLabsBackend(api_key=api_key)
    raise ValueError(f"unknown TTS backend: {name!r}")
