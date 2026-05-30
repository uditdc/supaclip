from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from supaclip.stitch.tts.base import normalize_settings
from supaclip.stitch.tts.cache import TTSCache
from supaclip.stitch.tts.elevenlabs import (
    ElevenLabsBackend,
    ElevenLabsError,
    _to_voice_settings,
    has_ssml,
)
from supaclip.stitch.tts.google import GoogleBackend, GoogleTTSError


def test_normalize_settings_scales_percentages():
    out = normalize_settings({"stability": 40, "similarity_boost": 75, "style": 30})
    assert out == pytest.approx({"stability": 0.4, "similarity_boost": 0.75, "style": 0.30})


def test_normalize_settings_passes_floats_through():
    out = normalize_settings({"stability": 0.5})
    assert out == {"stability": 0.5}


def test_normalize_settings_clamps_range():
    out = normalize_settings({"a": -5, "b": 250})
    assert out == {"a": 0.0, "b": 1.0}


def test_to_voice_settings_aliases_similarity():
    out = _to_voice_settings({"similarity": 75, "stability": 40})
    assert "similarity_boost" in out
    assert out["similarity_boost"] == pytest.approx(0.75)
    assert out["stability"] == pytest.approx(0.4)


def test_has_ssml_detects_break():
    assert has_ssml('Hello. <break time="0.4s"/> World.')
    assert has_ssml('Hello. <break time="400ms" />')
    assert not has_ssml("Plain text.")


def test_cache_key_stable_for_same_inputs():
    a = TTSCache.key("elevenlabs", "v1", {"stability": 0.4}, "hi")
    b = TTSCache.key("elevenlabs", "v1", {"stability": 0.4}, "hi")
    assert a == b


def test_cache_key_changes_on_text():
    a = TTSCache.key("elevenlabs", "v1", {"stability": 0.4}, "hi")
    b = TTSCache.key("elevenlabs", "v1", {"stability": 0.4}, "bye")
    assert a != b


def test_cache_key_changes_on_settings():
    a = TTSCache.key("elevenlabs", "v1", {"stability": 0.4}, "hi")
    b = TTSCache.key("elevenlabs", "v1", {"stability": 0.5}, "hi")
    assert a != b


def test_cache_get_put_roundtrip(tmp_path):
    cache = TTSCache(tmp_path)
    src = tmp_path / "voice.wav"
    src.write_bytes(b"RIFFfake")
    key = TTSCache.key("elevenlabs", "v1", {}, "hi")
    assert cache.get(key) is None
    stored = cache.put(key, src)
    assert stored is not None and stored.exists()
    assert cache.get(key) == stored


def test_cache_disabled_returns_none(tmp_path):
    cache = TTSCache(tmp_path, enabled=False)
    src = tmp_path / "voice.wav"
    src.write_bytes(b"x")
    key = TTSCache.key("elevenlabs", "v1", {}, "hi")
    assert cache.put(key, src) is None
    assert cache.get(key) is None


def test_synthesize_requires_api_key():
    backend = ElevenLabsBackend(api_key=None)
    with pytest.raises(ElevenLabsError):
        backend.synthesize("hi", "v1", {}, "/tmp/x.wav")


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return self._payload


class _FakeOpener:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.calls: list = []

    def open(self, req):
        self.calls.append(req)
        return _FakeResponse(self.payload)


def test_synthesize_posts_and_writes_wav(tmp_path):
    opener = _FakeOpener(b"\x00" * 128)
    backend = ElevenLabsBackend(api_key="k", opener=opener)
    out_path = tmp_path / "voice.wav"

    with patch("supaclip.stitch.tts.elevenlabs.run_ffmpeg") as mock_ff:
        def fake_ff(args):
            Path(args[-1]).write_bytes(b"RIFFfake")
            return ""
        mock_ff.side_effect = fake_ff
        backend.synthesize("hi there", "voice_xyz",
                            {"stability": 40, "similarity": 75},
                            out_path)

    assert out_path.exists()
    assert len(opener.calls) == 1
    req = opener.calls[0]
    assert req.full_url.endswith("/text-to-speech/voice_xyz")
    assert req.headers["Xi-api-key"] == "k"
    body = json.loads(req.data.decode("utf-8"))
    assert body["text"] == "hi there"
    assert body["voice_settings"]["stability"] == pytest.approx(0.4)
    assert body["voice_settings"]["similarity_boost"] == pytest.approx(0.75)


def _gemini_payload(pcm: bytes) -> bytes:
    import base64
    return json.dumps({
        "candidates": [{
            "content": {"parts": [{
                "inlineData": {"mimeType": "audio/L16", "data": base64.b64encode(pcm).decode()}
            }]}
        }]
    }).encode("utf-8")


def test_google_synthesize_requires_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    backend = GoogleBackend(api_key=None)
    with pytest.raises(GoogleTTSError):
        backend.synthesize("hi", "Kore", {}, "/tmp/x.wav")


def test_google_synthesize_posts_and_writes_wav(tmp_path):
    opener = _FakeOpener(_gemini_payload(b"\x00" * 256))
    backend = GoogleBackend(api_key="k", opener=opener)
    out_path = tmp_path / "voice.wav"

    with patch("supaclip.stitch.tts.google.run_ffmpeg") as mock_ff:
        def fake_ff(args):
            Path(args[-1]).write_bytes(b"RIFFfake")
            return ""
        mock_ff.side_effect = fake_ff
        backend.synthesize("hi there", "Kore", {}, out_path)

    assert out_path.exists()
    assert len(opener.calls) == 1
    req = opener.calls[0]
    assert req.full_url.endswith(":generateContent")
    assert req.headers["X-goog-api-key"] == "k"
    body = json.loads(req.data.decode("utf-8"))
    assert body["contents"][0]["parts"][0]["text"] == "hi there"
    voice_cfg = body["generationConfig"]["speechConfig"]["voiceConfig"]
    assert voice_cfg["prebuiltVoiceConfig"]["voiceName"] == "Kore"
    ff_args = mock_ff.call_args[0][0]
    assert "s16le" in ff_args and "24000" in ff_args


def test_google_synthesize_with_alignment(tmp_path, monkeypatch):
    import supaclip.stitch.tts.align as align_mod
    from supaclip.stitch.tts.base import Alignment

    canned = Alignment(characters=list("hi"), start_times=[0.0, 0.1],
                       end_times=[0.1, 0.2])
    monkeypatch.setattr(align_mod, "align_text_to_audio", lambda wav, text: canned)

    opener = _FakeOpener(_gemini_payload(b"\x00" * 256))
    backend = GoogleBackend(api_key="k", opener=opener)
    out_path = tmp_path / "voice.wav"

    with patch("supaclip.stitch.tts.google.run_ffmpeg") as mock_ff:
        def fake_ff(args):
            Path(args[-1]).write_bytes(b"RIFFfake")
            return ""
        mock_ff.side_effect = fake_ff
        out, alignment = backend.synthesize_with_alignment("hi", "Kore", {}, out_path)

    assert out.exists()
    assert alignment is canned
    assert len(opener.calls) == 1


def test_google_list_voices_returns_prebuilt():
    voices = GoogleBackend(api_key="k").list_voices()
    names = {v.voice_id for v in voices}
    assert {"Kore", "Puck", "Charon"} <= names
