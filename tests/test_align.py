from __future__ import annotations

import builtins

import pytest

from supaclip.stitch.captions import _extract_words
from supaclip.stitch.tts import align
from supaclip.stitch.tts.align import (
    AlignmentError,
    _normalize_word,
    _split_words,
    align_text_to_audio,
    build_char_alignment,
)


def test_split_words_keeps_char_spans():
    assert _split_words("hi  there") == [("hi", 0, 2), ("there", 4, 9)]


def test_normalize_word():
    assert _normalize_word("Hello,") == "hello"
    assert _normalize_word("don't") == "don't"
    assert _normalize_word("12") == ""
    assert _normalize_word("...") == ""


def test_build_char_alignment_spreads_word_timing():
    text = "one two three"
    words = [(0, 3, 0.0, 0.3), (4, 7, 0.4, 0.7), (8, 13, 0.8, 1.3)]
    a = build_char_alignment(text, words)

    assert len(a.characters) == len(text)
    assert a.start_times[0] == pytest.approx(0.0)
    for s, e in zip(a.start_times, a.end_times):
        assert e >= s - 1e-9
    for x, y in zip(a.start_times, a.start_times[1:]):
        assert y >= x - 1e-9

    words_out = _extract_words(a)
    assert [w.text for w in words_out] == ["one", "two", "three"]
    assert words_out[0].start == pytest.approx(0.0)
    assert words_out[2].end == pytest.approx(1.3)


def test_build_char_alignment_interpolates_punctuation_gaps():
    text = "Hello, world."
    # "Hello," spans chars [0,6); "world." spans [7,13)
    words = [(0, 6, 0.0, 0.5), (7, 13, 1.0, 1.5)]
    a = build_char_alignment(text, words)
    assert len(a.characters) == len(text)
    space_idx = text.index(" ")
    assert 0.5 <= a.start_times[space_idx] <= 1.0
    for x, y in zip(a.start_times, a.start_times[1:]):
        assert y >= x - 1e-9


def test_align_text_to_audio_builds_alignment(monkeypatch):
    monkeypatch.setattr(
        align, "_run_mms_fa",
        lambda wav, tokens: [(float(i), i + 0.5) for i in range(len(tokens))],
    )
    a = align_text_to_audio("ignored.wav", "Hello, world.")
    assert len(a.characters) == len("Hello, world.")
    words_out = _extract_words(a)
    assert [w.text for w in words_out] == ["Hello,", "world."]


def test_align_text_to_audio_rejects_unalignable_text(monkeypatch):
    monkeypatch.setattr(align, "_run_mms_fa", lambda wav, tokens: [])
    with pytest.raises(AlignmentError):
        align_text_to_audio("ignored.wav", "123 456")


def test_require_torchaudio_missing_dep(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name in ("torch", "torchaudio"):
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(AlignmentError) as excinfo:
        align._require_torchaudio()
    assert "align" in str(excinfo.value)
