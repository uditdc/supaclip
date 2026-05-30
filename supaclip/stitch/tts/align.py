from __future__ import annotations

import re
import wave
from pathlib import Path

from supaclip.stitch.tts.base import Alignment


class AlignmentError(RuntimeError):
    pass


_WORD_RE = re.compile(r"\S+")
_NORM_RE = re.compile(r"[^a-z']")

WordTiming = tuple[int, int, float, float]  # (char_start, char_end, start_s, end_s)


def _split_words(text: str) -> list[tuple[str, int, int]]:
    """Split into whitespace-delimited words, keeping each word's character span
    in the original string so inter-word punctuation/spacing is preserved."""
    return [(m.group(0), m.start(), m.end()) for m in _WORD_RE.finditer(text)]


def _normalize_word(word: str) -> str:
    """Reduce a display word to the lowercase [a-z'] form the acoustic model
    expects. Pure-punctuation / numeric tokens collapse to an empty string."""
    return _NORM_RE.sub("", word.lower())


def align_text_to_audio(wav_path: str | Path, text: str) -> Alignment:
    """Force-align `text` against the speech in `wav_path`, returning a
    character-level Alignment matching the ElevenLabs contract.

    Uses a local torchaudio MMS_FA aligner (no network/API cost). The model
    weights download once on first use; the result is cached upstream by the
    TTS alignment cache.
    """
    words = _split_words(text)
    if not words:
        raise AlignmentError("cannot align empty text")

    normalized = [_normalize_word(w) for w, _, _ in words]
    aligned_idx = [i for i, n in enumerate(normalized) if n]
    if not aligned_idx:
        raise AlignmentError("text has no alignable words after normalization")

    spans = _run_mms_fa(wav_path, [normalized[i] for i in aligned_idx])
    if len(spans) != len(aligned_idx):
        raise AlignmentError(
            f"aligner returned {len(spans)} spans for {len(aligned_idx)} words"
        )

    timings = _assign_word_timings(words, aligned_idx, spans)
    return build_char_alignment(text, timings)


def _assign_word_timings(
    words: list[tuple[str, int, int]],
    aligned_idx: list[int],
    spans: list[tuple[float, float]],
) -> list[WordTiming]:
    """Give every display word a (char_start, char_end, start, end). Aligned
    words take their span; un-alignable words (punctuation/numbers) collapse to
    a zero-width point interpolated between their nearest aligned neighbors."""
    by_idx = {i: spans[k] for k, i in enumerate(aligned_idx)}
    result: list[WordTiming] = []
    for i, (_, cs, ce) in enumerate(words):
        if i in by_idx:
            s, e = by_idx[i]
        else:
            prev_e = next((by_idx[j][1] for j in range(i - 1, -1, -1) if j in by_idx), None)
            next_s = next((by_idx[j][0] for j in range(i + 1, len(words)) if j in by_idx), None)
            if prev_e is None and next_s is None:
                s = e = 0.0
            elif prev_e is None:
                s = e = next_s
            elif next_s is None:
                s = e = prev_e
            else:
                s = e = (prev_e + next_s) / 2
        result.append((cs, ce, float(s), float(e)))
    return result


def build_char_alignment(text: str, words: list[WordTiming]) -> Alignment:
    """Spread word-level timings across the original characters: each word's
    characters split its [start, end] evenly, and the gap characters between
    words (spaces, punctuation) interpolate across the silence between them.

    Guarantees len(characters) == len(text) with monotonic non-decreasing times,
    the shape `chunk_alignment`/`_extract_words` consume.
    """
    n = len(text)
    starts: list[float | None] = [None] * n
    ends: list[float | None] = [None] * n

    prev_end = 0.0
    clamped: list[WordTiming] = []
    for cs, ce, s, e in words:
        s = max(s, prev_end)
        e = max(e, s)
        clamped.append((cs, ce, s, e))
        prev_end = e

    for cs, ce, s, e in clamped:
        length = ce - cs
        if length <= 0:
            continue
        dur = e - s
        for k, idx in enumerate(range(cs, ce)):
            starts[idx] = s + dur * k / length
            ends[idx] = s + dur * (k + 1) / length

    i = 0
    while i < n:
        if starts[i] is not None:
            i += 1
            continue
        j = i
        while j < n and starts[j] is None:
            j += 1
        left_t = ends[i - 1] if i > 0 and ends[i - 1] is not None else 0.0
        right_t = starts[j] if j < n and starts[j] is not None else left_t
        run = j - i
        for k, idx in enumerate(range(i, j)):
            t = left_t + (right_t - left_t) * (k + 1) / (run + 1)
            starts[idx] = t
            ends[idx] = t
        i = j

    return Alignment(
        characters=list(text),
        start_times=[float(x) for x in starts],
        end_times=[float(x) for x in ends],
    )


_MMS_FA = None


def _require_torchaudio():
    try:
        import torch
        import torchaudio
    except ImportError as e:
        raise AlignmentError(
            "forced alignment needs the 'align' extra: pip install 'supaclip[align]'"
        ) from e
    return torch, torchaudio


def _load_mms_fa():
    global _MMS_FA
    if _MMS_FA is None:
        _, torchaudio = _require_torchaudio()
        bundle = torchaudio.pipelines.MMS_FA
        _MMS_FA = (
            bundle.get_model(),
            bundle.get_tokenizer(),
            bundle.get_aligner(),
            bundle.sample_rate,
        )
    return _MMS_FA


def _load_waveform(wav_path: str | Path, target_sr: int, torch, torchaudio):
    """Load a PCM WAV to a mono [1, time] float tensor at target_sr.

    Reads via the stdlib `wave` module rather than `torchaudio.load`, which in
    newer torchaudio routes through TorchCodec (an extra FFmpeg-linked dep). The
    TTS backends emit plain PCM WAVs, so this is sufficient and dependency-free.
    """
    with wave.open(str(wav_path), "rb") as wf:
        n_channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sr = wf.getframerate()
        raw = wf.readframes(wf.getnframes())

    if sample_width == 2:
        dtype, max_val = torch.int16, 32768.0
    elif sample_width == 4:
        dtype, max_val = torch.int32, 2147483648.0
    else:
        raise AlignmentError(f"unsupported WAV sample width: {sample_width * 8}-bit")

    samples = torch.frombuffer(bytearray(raw), dtype=dtype).to(torch.float32) / max_val
    samples = samples.view(-1, n_channels).t()
    samples = samples.mean(dim=0, keepdim=True) if n_channels > 1 else samples.view(1, -1)
    if sr != target_sr:
        samples = torchaudio.functional.resample(samples, sr, target_sr)
    return samples


def _run_mms_fa(wav_path: str | Path, norm_tokens: list[str]) -> list[tuple[float, float]]:
    torch, torchaudio = _require_torchaudio()
    model, tokenizer, aligner, sample_rate = _load_mms_fa()

    waveform = _load_waveform(wav_path, sample_rate, torch, torchaudio)

    with torch.inference_mode():
        emission, _ = model(waveform)
        token_spans = aligner(emission[0], tokenizer(norm_tokens))

    ratio = waveform.size(1) / emission.size(1)
    spans: list[tuple[float, float]] = []
    for word_spans in token_spans:
        start = word_spans[0].start * ratio / sample_rate
        end = word_spans[-1].end * ratio / sample_rate
        spans.append((float(start), float(end)))
    return spans
