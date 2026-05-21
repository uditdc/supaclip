from __future__ import annotations

from supaclip.extract.backends._shared import _coerce, _prune_overlaps
from supaclip.extract.backends.gemma import _frame_count_for
from supaclip.extract.analyze import SegmentEvent
from supaclip.extract.profiles import GTA_PROFILE


def test_coerce_parses_multiple_events_and_sorts_by_start():
    parsed = {
        "events": [
            {"start": 8.0, "end": 14.0, "description": "crash", "categories": ["crash"],
             "base_interest": 60},
            {"start": 0.0, "end": 7.5, "description": "chase", "categories": ["police_chase"],
             "base_interest": 80,
             "game_signals": {"wanted_level": 3, "vehicles": ["police_cruiser"]}},
        ]
    }
    out = _coerce(parsed, GTA_PROFILE, duration=14.5)
    assert [e.description for e in out.events] == ["chase", "crash"]
    assert out.events[0].categories == ["police_chase"]
    assert out.events[0].game_signals == {"wanted_level": 3, "vehicles": ["police_cruiser"]}
    assert out.events[1].base_interest == 60


def test_coerce_clamps_times_to_segment_duration():
    parsed = {"events": [
        {"start": -2.0, "end": 100.0, "description": "x"},
    ]}
    out = _coerce(parsed, GTA_PROFILE, duration=12.0)
    assert len(out.events) == 1
    assert out.events[0].start == 0.0
    assert out.events[0].end == 12.0


def test_coerce_drops_categories_outside_taxonomy():
    parsed = {"events": [
        {"start": 0.0, "end": 5.0, "description": "x",
         "categories": ["police_chase", "not_a_real_tag"]},
    ]}
    out = _coerce(parsed, GTA_PROFILE, duration=5.0)
    assert out.events[0].categories == ["police_chase"]


def test_coerce_drops_signal_keys_outside_profile():
    parsed = {"events": [
        {"start": 0.0, "end": 5.0, "description": "x",
         "game_signals": {"wanted_level": 4, "made_up": "value"}},
    ]}
    out = _coerce(parsed, GTA_PROFILE, duration=5.0)
    assert out.events[0].game_signals == {"wanted_level": 4}


def test_coerce_drops_events_shorter_than_min_duration():
    parsed = {"events": [
        {"start": 0.0, "end": 0.5, "description": "blip"},
        {"start": 1.0, "end": 5.0, "description": "real", "base_interest": 50},
    ]}
    out = _coerce(parsed, GTA_PROFILE, duration=10.0)
    assert len(out.events) == 1
    assert out.events[0].description == "real"


def test_coerce_synthesizes_fallback_event_when_no_events_returned():
    parsed = {"events": []}
    out = _coerce(parsed, GTA_PROFILE, duration=7.0)
    assert len(out.events) == 1
    assert out.events[0].start == 0.0
    assert out.events[0].end == 7.0


def test_coerce_accepts_legacy_flat_payload_as_single_event():
    parsed = {
        "description": "lone situation",
        "categories": ["cruising"],
        "base_interest": 30,
        "game_signals": {"location": "downtown"},
        "audio_cues": ["engine"],
    }
    out = _coerce(parsed, GTA_PROFILE, duration=9.0)
    assert len(out.events) == 1
    ev = out.events[0]
    assert ev.start == 0.0 and ev.end == 9.0
    assert ev.description == "lone situation"
    assert ev.categories == ["cruising"]


def test_prune_overlaps_keeps_higher_interest_on_heavy_overlap():
    a = SegmentEvent(start=0.0, end=10.0, description="low", base_interest=20)
    b = SegmentEvent(start=1.0, end=9.0, description="high", base_interest=80)
    pruned = _prune_overlaps(sorted([a, b], key=lambda e: e.start))
    assert len(pruned) == 1
    assert pruned[0].description == "high"


def test_prune_overlaps_trims_minor_overlap_into_disjoint_pair():
    a = SegmentEvent(start=0.0, end=6.0, description="a", base_interest=50)
    b = SegmentEvent(start=5.0, end=12.0, description="b", base_interest=50)
    pruned = _prune_overlaps([a, b])
    assert len(pruned) == 2
    assert pruned[0].end == 6.0
    assert pruned[1].start == 6.0
    assert pruned[1].end == 12.0


def test_frame_count_scales_with_duration():
    assert _frame_count_for(0.0) == 0
    assert _frame_count_for(10.0) == 6     # floor
    assert _frame_count_for(60.0) == 12    # ~12 frames at 5s spacing
    assert _frame_count_for(600.0) == 24   # ceiling
