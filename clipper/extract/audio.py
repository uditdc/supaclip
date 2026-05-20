from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Peak:
    time: float
    intensity: float  # 0..1, normalized within the source


def detect_peaks(
    samples: list[tuple[float, float]],
    percentile: float = 0.85,
    min_gap_seconds: float = 3.0,
) -> list[Peak]:
    """Find local maxima above a percentile of the loudness curve.

    `samples` is a list of (time_seconds, db_value) from ffmpeg astats. dB values
    are negative; -inf or very low values represent silence. Returns peaks
    normalized to intensity in [0, 1].
    """
    if not samples:
        return []

    cleaned = [(t, db) for (t, db) in samples if db > -200 and db == db]  # drop -inf/NaN
    if not cleaned:
        return []

    db_values = [db for _, db in cleaned]
    db_sorted = sorted(db_values)
    idx = int(percentile * (len(db_sorted) - 1))
    threshold = db_sorted[idx]

    lo = min(db_values)
    hi = max(db_values)
    span = (hi - lo) or 1.0

    candidates: list[Peak] = []
    for i, (t, db) in enumerate(cleaned):
        if db < threshold:
            continue
        prev_db = cleaned[i - 1][1] if i > 0 else db
        next_db = cleaned[i + 1][1] if i + 1 < len(cleaned) else db
        if db >= prev_db and db >= next_db:
            candidates.append(Peak(time=t, intensity=(db - lo) / span))

    candidates.sort(key=lambda p: p.intensity, reverse=True)
    kept: list[Peak] = []
    for c in candidates:
        if all(abs(c.time - k.time) >= min_gap_seconds for k in kept):
            kept.append(c)
    kept.sort(key=lambda p: p.time)
    return kept


def audio_factor_for_range(
    samples: list[tuple[float, float]],
    start: float,
    end: float,
) -> float:
    """Return a 0..100 score reflecting the peak loudness inside [start, end]."""
    if not samples or end <= start:
        return 0.0
    in_range = [db for (t, db) in samples if start <= t <= end and db > -200]
    if not in_range:
        return 0.0
    all_db = [db for (_, db) in samples if db > -200]
    if not all_db:
        return 0.0
    lo = min(all_db)
    hi = max(all_db)
    span = (hi - lo) or 1.0
    return round(((max(in_range) - lo) / span) * 100.0, 2)


def peak_loudness_db(
    samples: list[tuple[float, float]],
    start: float,
    end: float,
) -> float | None:
    if not samples:
        return None
    in_range = [db for (t, db) in samples if start <= t <= end and db > -200]
    if not in_range:
        return None
    return round(max(in_range), 2)
