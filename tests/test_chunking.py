from __future__ import annotations

from supaclip.extract.chunking import chunk_segment, shift_events_to_segment


def test_short_segment_is_not_chunked():
    chunks = chunk_segment(0.0, 30.0, samples=[])
    assert chunks == [(0.0, 30.0)]


def test_long_segment_is_split_with_overlap():
    chunks = chunk_segment(0.0, 120.0, samples=[])
    assert len(chunks) >= 3
    assert chunks[0][0] == 0.0
    assert chunks[-1][1] == 120.0
    for i in range(len(chunks) - 1):
        assert chunks[i + 1][0] < chunks[i][1], "adjacent chunks must overlap"


def test_chunks_cover_full_segment():
    chunks = chunk_segment(10.0, 160.0, samples=[])
    assert chunks[0][0] == 10.0
    assert chunks[-1][1] == 160.0


def test_chunk_boundaries_snap_to_audio_troughs():
    samples = [(t * 1.0, -10.0) for t in range(120)]
    for quiet in (28, 29, 30, 31, 32, 60, 61, 62):
        samples[quiet] = (float(quiet), -60.0)
    chunks = chunk_segment(0.0, 120.0, samples=samples, target_seconds=30.0)
    cut_times = [chunks[i][1] - 5.0 for i in range(len(chunks) - 1)]
    assert any(28 <= ct <= 32 for ct in cut_times), \
        "expected a chunk boundary to land at the quiet point around t≈30"


def test_chunks_respect_max_seconds_cap():
    chunks = chunk_segment(0.0, 200.0, samples=[], target_seconds=30.0, max_seconds=45.0)
    for cs, ce in chunks:
        core = ce - cs - 10.0  # subtract ~10s of overlap (5 each side)
        assert core <= 45.0 + 0.1, f"chunk {cs}-{ce} core too large"


def test_shift_events_to_segment_offsets_correctly():
    events = [
        {"start": 2.0, "end": 8.0, "description": "x"},
        {"start": 12.5, "end": 18.0, "description": "y"},
    ]
    shifted = shift_events_to_segment(events, chunk_start=30.0, chunk_end=50.0, segment_start=0.0)
    assert shifted[0]["start"] == 32.0
    assert shifted[0]["end"] == 38.0
    assert shifted[1]["start"] == 42.5
    assert shifted[1]["end"] == 48.0
    assert shifted[0]["description"] == "x"


def test_shift_events_with_non_zero_segment_start():
    events = [{"start": 1.0, "end": 5.0, "description": "z"}]
    shifted = shift_events_to_segment(events, chunk_start=110.0, chunk_end=130.0, segment_start=100.0)
    assert shifted[0]["start"] == 11.0
    assert shifted[0]["end"] == 15.0
