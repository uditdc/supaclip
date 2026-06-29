from __future__ import annotations

from supaclip.extract import summarize as summarize_mod
from supaclip.extract.llm import LLMConfig
from supaclip.extract.profiles import MOVIE_PROFILE
from supaclip.extract.summarize import summarize_source

EVENTS = [
    {"start": 0.0, "end": 40.0, "description": "A boy arrives at a quiet farm.",
     "dialogue": "You'll be safe here."},
    {"start": 40.0, "end": 90.0, "description": "Soldiers search the woods.",
     "dialogue": "Find them before nightfall."},
]


def _cfg() -> LLMConfig:
    return LLMConfig(model="m", base_url="http://stub", api_key=None)


def _good_json() -> str:
    return """
    {
      "synopsis": "A boy hides on a farm while soldiers hunt the countryside.",
      "themes": ["survival", "loss of innocence"],
      "tone": "tense wartime drama",
      "characters": [{"name": "The boy", "role": "protagonist"}, {"bogus": 1}],
      "beats": [
        {"title": "Arrival", "start": 0.0, "end": 40.0, "summary": "He reaches the farm."},
        {"title": "The hunt", "start": 40.0, "end": 200.0, "summary": "Soldiers close in."},
        {"title": "bad", "start": 10.0, "end": 5.0, "summary": "inverted, dropped"}
      ]
    }
    """


def test_summarize_parses_and_clamps(monkeypatch):
    monkeypatch.setattr(summarize_mod, "call_json", lambda *a, **k: _good_json())
    out = summarize_source(EVENTS, source_duration=90.0, profile=MOVIE_PROFILE, cfg=_cfg())
    assert out is not None
    assert out.synopsis.startswith("A boy hides")
    assert out.themes == ["survival", "loss of innocence"]
    assert out.generated_by == "m"
    # malformed character dropped, valid one kept
    assert [c.name for c in out.characters] == ["The boy"]
    # inverted beat dropped; remaining sorted; end clamped to duration
    assert [b.title for b in out.beats] == ["Arrival", "The hunt"]
    assert out.beats[1].end == 90.0


def test_summarize_none_on_empty_events():
    assert summarize_source([], source_duration=10.0, profile=MOVIE_PROFILE, cfg=_cfg()) is None


def test_summarize_none_on_call_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no service")
    monkeypatch.setattr(summarize_mod, "call_json", boom)
    assert summarize_source(EVENTS, source_duration=90.0, profile=MOVIE_PROFILE, cfg=_cfg()) is None


def test_summarize_none_on_unparseable(monkeypatch):
    monkeypatch.setattr(summarize_mod, "call_json", lambda *a, **k: "sorry, no json")
    assert summarize_source(EVENTS, source_duration=90.0, profile=MOVIE_PROFILE, cfg=_cfg()) is None


def test_summarize_hierarchical_for_long_films(monkeypatch):
    # > WINDOW_SCENES events -> windowed summarize + reduce pass
    n = summarize_mod.WINDOW_SCENES * 2
    dur = float(n * 10)
    events = [
        {"start": i * 10.0, "end": i * 10.0 + 10.0,
         "description": f"scene {i}", "dialogue": f"line {i}"}
        for i in range(n)
    ]

    def fake_call(prompt, cfg, system=None):
        if "CONTIGUOUS PORTION" in prompt:  # window pass
            return ('{"synopsis": "a portion happens", '
                    '"characters": [{"name": "Vance", "role": "lead"}], '
                    '"beats": [{"title": "beat", "start": 0.0, "end": 30.0, "summary": "x"}]}')
        if "combine them into one coherent" in prompt:  # reduce pass
            return '{"synopsis": "the whole film in brief", "themes": ["survival"], "tone": "tense"}'
        raise AssertionError("unexpected single-pass prompt for a long film")

    monkeypatch.setattr(summarize_mod, "call_json", fake_call)
    out = summarize_source(events, source_duration=dur, profile=MOVIE_PROFILE, cfg=_cfg())
    assert out is not None
    assert out.synopsis == "the whole film in brief"
    assert out.themes == ["survival"]
    # one beat per window (2 windows), characters de-duplicated to one "Vance"
    assert len(out.beats) == 2
    assert [c.name for c in out.characters] == ["Vance"]
