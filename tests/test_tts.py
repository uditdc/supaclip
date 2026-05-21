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
