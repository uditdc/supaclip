from __future__ import annotations

DEFAULT_TARGET_SECONDS = 30.0
DEFAULT_MAX_SECONDS = 45.0
DEFAULT_OVERLAP_SECONDS = 5.0
DEFAULT_MIN_CHUNK_SECONDS = 12.0


def chunk_segment(
    start: float,
    end: float,
    samples: list[tuple[float, float]],
    *,
    target_seconds: float = DEFAULT_TARGET_SECONDS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    overlap_seconds: float = DEFAULT_OVERLAP_SECONDS,
) -> list[tuple[float, float]]:
    """Split [start, end] into sub-windows at audio-energy troughs with overlap.

    Returns a list of (chunk_start, chunk_end) tuples covering the segment.
    Boundaries are placed at local minima in the loudness curve so events
    are unlikely to be cut in their middle. Each returned chunk extends
    ±overlap_seconds beyond its core boundary (clamped to the segment edges)
    so adjacent chunks both see the transition.

    If the segment is short enough, returns a single (start, end) tuple
    (no chunking, no overlap).
    """
    duration = end - start
    if duration <= max_seconds:
        return [(start, end)]

    boundaries = _pick_boundaries(
        start, end, samples,
        target_seconds=target_seconds,
        max_seconds=max_seconds,
    )

    chunks: list[tuple[float, float]] = []
    edges = [start] + boundaries + [end]
    for i in range(len(edges) - 1):
        core_start = edges[i]
        core_end = edges[i + 1]
        cs = max(start, core_start - (overlap_seconds if i > 0 else 0.0))
        ce = min(end, core_end + (overlap_seconds if i < len(edges) - 2 else 0.0))
        chunks.append((round(cs, 3), round(ce, 3)))
    return chunks


def _pick_boundaries(
    start: float,
    end: float,
    samples: list[tuple[float, float]],
    *,
    target_seconds: float,
    max_seconds: float,
) -> list[float]:
    """Return interior split points (excluding start/end) at audio troughs."""
    duration = end - start
    n_splits = max(1, int(duration // target_seconds))
    cuts: list[float] = []
    prev = start
    remaining = duration

    in_range = [(t, db) for (t, db) in samples if start < t < end and db > -200 and db == db]

    for i in range(n_splits - 1, -1, -1):
        if i == 0:
            break
        next_chunk_target = remaining - target_seconds
        if next_chunk_target <= 0:
            break
        ideal_cut = prev + target_seconds
        search_lo = max(prev + 0.5, ideal_cut - target_seconds * 0.4)
        search_hi = min(end - 0.5, ideal_cut + target_seconds * 0.4)
        cut = _lowest_in_window(in_range, search_lo, search_hi, fallback=ideal_cut)

        if cut - prev > max_seconds:
            cut = prev + max_seconds
        if end - cut < (target_seconds * 0.4):
            break

        cuts.append(round(cut, 3))
        prev = cut
        remaining = end - prev

    return cuts


def _lowest_in_window(
    in_range: list[tuple[float, float]],
    lo: float,
    hi: float,
    fallback: float,
) -> float:
    if lo >= hi:
        return fallback
    window = [(t, db) for (t, db) in in_range if lo <= t <= hi]
    if not window:
        return fallback
    t_min, _ = min(window, key=lambda x: x[1])
    return t_min


def shift_events_to_segment(
    events: list[dict],
    chunk_start: float,
    chunk_end: float,
    segment_start: float,
) -> list[dict]:
    """Offset event start/end so they're relative to the parent segment.

    Backend returns events in chunk-relative seconds. To merge events across
    chunks we re-express them in segment-relative seconds.
    """
    chunk_offset = chunk_start - segment_start
    chunk_duration = chunk_end - chunk_start
    shifted: list[dict] = []
    for ev in events:
        s = float(ev.get("start", 0.0)) + chunk_offset
        e = float(ev.get("end", chunk_duration)) + chunk_offset
        shifted.append({**ev, "start": round(s, 3), "end": round(e, 3)})
    return shifted
