from __future__ import annotations

from supaclip.extract import aggregate as aggregate_mod
from supaclip.extract.aggregate import AggregateConfig, aggregate_events
from supaclip.extract.profiles import GTA_PROFILE


def _cfg() -> AggregateConfig:
    return AggregateConfig(model="m", base_url="http://stub", api_key=None)


def test_aggregate_returns_input_unchanged_when_single_event():
    events = [{"start": 0.0, "end": 10.0, "description": "x"}]
    out = aggregate_events(events, source_duration=10.0, profile=GTA_PROFILE, cfg=_cfg())
    assert out == events


def test_aggregate_returns_input_unchanged_when_llm_call_fails(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("no service")
    monkeypatch.setattr(aggregate_mod, "_call", boom)
    events = [
        {"start": 0.0, "end": 10.0, "description": "a"},
        {"start": 12.0, "end": 20.0, "description": "b"},
    ]
    out = aggregate_events(events, source_duration=30.0, profile=GTA_PROFILE, cfg=_cfg())
    assert len(out) == 2
    assert out[0]["description"] == "a"


def test_aggregate_merges_per_llm_output(monkeypatch):
    monkeypatch.setattr(aggregate_mod, "_call", lambda prompt, cfg: '''
    {"events": [
        {"start": 0.0, "end": 20.0,
         "description": "merged chase", "categories": ["police_chase"],
         "base_interest": 75, "game_signals": {"wanted_level": 3}, "audio_cues": ["sirens"]}
    ]}
    ''')
    events = [
        {"start": 0.0, "end": 11.0, "description": "chase part 1",
         "categories": ["police_chase"], "base_interest": 70},
        {"start": 9.0, "end": 20.0, "description": "chase part 2",
         "categories": ["police_chase"], "base_interest": 80},
    ]
    out = aggregate_events(events, source_duration=20.0, profile=GTA_PROFILE, cfg=_cfg())
    assert len(out) == 1
    assert out[0]["description"] == "merged chase"
    assert out[0]["categories"] == ["police_chase"]
    assert out[0]["game_signals"] == {"wanted_level": 3}


def test_aggregate_falls_back_when_llm_returns_unparseable(monkeypatch):
    monkeypatch.setattr(aggregate_mod, "_call", lambda prompt, cfg: "not json at all")
    events = [
        {"start": 0.0, "end": 10.0, "description": "a"},
        {"start": 10.0, "end": 20.0, "description": "b"},
    ]
    out = aggregate_events(events, source_duration=20.0, profile=GTA_PROFILE, cfg=_cfg())
    assert len(out) == 2
