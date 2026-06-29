from __future__ import annotations

import pytest

from supaclip.extract import llm as llm_mod
from supaclip.extract.llm import retry_call


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(llm_mod.time, "sleep", lambda _s: None)


def test_retry_call_succeeds_after_transient_failures():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("429 rate limit")
        return "ok"

    assert retry_call(flaky, attempts=4) == "ok"
    assert calls["n"] == 3


def test_retry_call_reraises_after_exhausting_attempts():
    calls = {"n": 0}

    def always():
        calls["n"] += 1
        raise RuntimeError("permanent")

    with pytest.raises(RuntimeError, match="permanent"):
        retry_call(always, attempts=3)
    assert calls["n"] == 3
