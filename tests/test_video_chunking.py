from __future__ import annotations

import pytest

from supaclip.extract.backends import video as gv
from supaclip.extract.profiles import GTA_PROFILE


def test_split_chunks_short_input_is_single_chunk():
    assert gv._split_chunks(0.0, 14.0, gv.CHUNK_SECONDS) == [(0.0, 14.0)]


def test_split_chunks_at_boundary_is_single_chunk():
    assert gv._split_chunks(0.0, gv.CHUNK_SECONDS, gv.CHUNK_SECONDS) == [
        (0.0, gv.CHUNK_SECONDS),
    ]


def test_split_chunks_long_input_splits_at_max_len():
    chunks = gv._split_chunks(0.0, 1000.0, 480.0)
    assert chunks == [(0.0, 480.0), (480.0, 960.0), (960.0, 1000.0)]


def test_split_chunks_non_zero_start_offset_preserved():
    chunks = gv._split_chunks(100.0, 700.0, 250.0)
    assert chunks == [(100.0, 350.0), (350.0, 600.0), (600.0, 700.0)]


def test_split_chunks_empty_for_invalid_range():
    assert gv._split_chunks(10.0, 10.0, 60.0) == []
    assert gv._split_chunks(20.0, 5.0, 60.0) == []
    assert gv._split_chunks(0.0, 100.0, 0.0) == []


def test_analyze_segment_merges_events_across_chunks_with_offset(monkeypatch):
    monkeypatch.setattr(gv, "_encode_chunk", lambda *a, **kw: b"\x00")
    monkeypatch.setattr(gv, "CHUNK_SECONDS", 60.0)

    chunk_payloads = iter([
        '{"events":[{"start":0.0,"end":20.0,"description":"chunk0-a","categories":["cruising"],"base_interest":40}]}',
        '{"events":[{"start":5.0,"end":35.0,"description":"chunk1-a","categories":["crash"],"base_interest":70}]}',
        '{"events":[{"start":10.0,"end":25.0,"description":"chunk2-a","categories":["mission"],"base_interest":30}]}',
    ])

    backend = gv.VideoBackend(model="m", base_url="http://x", api_key="test-key")
    monkeypatch.setattr(backend, "_call", lambda *a, **kw: next(chunk_payloads))

    analysis = backend.analyze_segment("/x/video.mp4", 0.0, 180.0, GTA_PROFILE)
    events = analysis.events

    assert [e.description for e in events] == ["chunk0-a", "chunk1-a", "chunk2-a"]
    assert events[0].start == 0.0 and events[0].end == 20.0
    assert events[1].start == 65.0 and events[1].end == 95.0
    assert events[2].start == 130.0 and events[2].end == 145.0


def test_analyze_segment_falls_back_when_chunk_returns_unparseable_json(monkeypatch):
    monkeypatch.setattr(gv, "_encode_chunk", lambda *a, **kw: b"\x00")
    monkeypatch.setattr(gv, "CHUNK_SECONDS", 60.0)

    backend = gv.VideoBackend(model="m", base_url="http://x", api_key="test-key")
    monkeypatch.setattr(backend, "_call", lambda *a, **kw: "not json at all")

    analysis = backend.analyze_segment("/x/video.mp4", 0.0, 30.0, GTA_PROFILE)
    assert len(analysis.events) == 1
    assert "(analyzer failed to return valid JSON)" in analysis.events[0].description


def test_analyze_segment_empty_range_returns_no_events():
    backend = gv.VideoBackend(model="m", base_url="http://x", api_key="test-key")
    analysis = backend.analyze_segment("/x/video.mp4", 0.0, 0.0, GTA_PROFILE)
    assert analysis.events == []


def test_normalize_model_strips_openrouter_prefix_and_suffix():
    assert gv._normalize_model("google/gemma-4-31b-it:free") == "gemma-4-31b-it"
    assert gv._normalize_model("google/gemma-4-31b-it") == "gemma-4-31b-it"
    assert gv._normalize_model("gemma-4-31b-it:free") == "gemma-4-31b-it"
    assert gv._normalize_model("gemma-4-31b-it") == "gemma-4-31b-it"


def test_constructor_requires_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_AI_STUDIO_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        gv.VideoBackend(model="m", base_url="http://x", api_key=None)


def test_constructor_picks_up_gemini_api_key_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    backend = gv.VideoBackend(model="m", base_url="http://x", api_key=None)
    assert backend.api_key == "from-env"
