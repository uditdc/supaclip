from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from ..core.ffmpeg import extract_loudness_curve
from .audio import detect_peaks
from .chunking import chunk_segment


@dataclass
class DryChunkResult:
    directory: Path
    chunks: list[tuple[float, float]]
    peak_count: int


def run_dry_chunk(
    source_path: str,
    start: float,
    end: float,
    out_dir: Path,
    *,
    target_seconds: float = 30.0,
    max_seconds: float = 45.0,
    overlap_seconds: float = 5.0,
) -> DryChunkResult:
    out_dir.mkdir(parents=True, exist_ok=True)

    sys.stderr.write(f"==> Extracting audio loudness curve from {source_path}\n")
    samples = extract_loudness_curve(source_path)
    in_range = [(t, db) for (t, db) in samples if start <= t <= end and db > -200]
    peaks = detect_peaks(samples)
    peaks_in_range = [p for p in peaks if start <= p.time <= end]

    chunks = chunk_segment(
        start, end, samples,
        target_seconds=target_seconds,
        max_seconds=max_seconds,
        overlap_seconds=overlap_seconds,
    )

    payload = {
        "source": source_path,
        "segment": {"start": start, "end": end, "duration": end - start},
        "config": {
            "target_seconds": target_seconds,
            "max_seconds": max_seconds,
            "overlap_seconds": overlap_seconds,
        },
        "audio": {
            "samples_total": len(samples),
            "samples_in_range": len(in_range),
            "peaks_in_range": [
                {"t": round(p.time, 3), "intensity": round(p.intensity, 3)}
                for p in peaks_in_range
            ],
        },
        "chunks": [
            {
                "idx": i,
                "start": cs,
                "end": ce,
                "duration": round(ce - cs, 3),
            }
            for i, (cs, ce) in enumerate(chunks)
        ],
    }
    (out_dir / "chunks.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    _write_svg(out_dir / "chunks.svg", start, end, in_range, peaks_in_range, chunks)
    _write_html(out_dir / "chunks.html", start, end, chunks, peaks_in_range)

    return DryChunkResult(
        directory=out_dir,
        chunks=chunks,
        peak_count=len(peaks_in_range),
    )


def _write_svg(
    path: Path,
    start: float,
    end: float,
    in_range: list[tuple[float, float]],
    peaks: list,
    chunks: list[tuple[float, float]],
) -> None:
    """Render a loudness curve with chunk boundaries and peaks marked."""
    width = 1200
    height = 220
    pad_l, pad_r, pad_t, pad_b = 40, 20, 20, 30
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    duration = max(0.001, end - start)
    if in_range:
        dbs = [db for _, db in in_range]
        lo = min(dbs)
        hi = max(dbs)
    else:
        lo, hi = -60.0, 0.0
    span = (hi - lo) or 1.0

    def x_of(t: float) -> float:
        return pad_l + (t - start) / duration * plot_w

    def y_of(db: float) -> float:
        return pad_t + (1.0 - (db - lo) / span) * plot_h

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" style="background:#111;font-family:monospace;">'
    )

    parts.append(
        f'<rect x="{pad_l}" y="{pad_t}" width="{plot_w}" height="{plot_h}" '
        f'fill="#1a1a1a" stroke="#333"/>'
    )

    if in_range:
        d = " ".join(
            f"{'M' if i == 0 else 'L'} {x_of(t):.1f} {y_of(db):.1f}"
            for i, (t, db) in enumerate(in_range)
        )
        parts.append(f'<path d="{d}" fill="none" stroke="#7af" stroke-width="1"/>')

    colors = ["#e84", "#4e8", "#8e4", "#4ae", "#a4e", "#ea4", "#8a4", "#4e4"]
    for i, (cs, ce) in enumerate(chunks):
        x1 = x_of(cs)
        x2 = x_of(ce)
        color = colors[i % len(colors)]
        parts.append(
            f'<rect x="{x1:.1f}" y="{pad_t}" width="{x2 - x1:.1f}" height="{plot_h}" '
            f'fill="{color}" fill-opacity="0.10" stroke="{color}" stroke-opacity="0.5"/>'
        )
        parts.append(
            f'<text x="{(x1 + x2) / 2:.1f}" y="{pad_t + 14}" fill="{color}" '
            f'font-size="11" text-anchor="middle">c{i}</text>'
        )
        parts.append(
            f'<text x="{(x1 + x2) / 2:.1f}" y="{height - 8}" fill="{color}" '
            f'font-size="10" text-anchor="middle">{cs:.1f}–{ce:.1f}</text>'
        )

    for p in peaks:
        x = x_of(p.time)
        parts.append(
            f'<line x1="{x:.1f}" y1="{pad_t}" x2="{x:.1f}" y2="{pad_t + plot_h}" '
            f'stroke="#f44" stroke-width="1" stroke-dasharray="2,3" opacity="0.6"/>'
        )

    parts.append(
        f'<text x="{pad_l}" y="{pad_t - 6}" fill="#aaa" font-size="11">'
        f'loudness (dB) — red dashes = audio peaks, colored bands = chunks'
        f'</text>'
    )
    parts.append('</svg>')
    path.write_text("\n".join(parts), encoding="utf-8")


def _write_html(
    path: Path,
    start: float,
    end: float,
    chunks: list[tuple[float, float]],
    peaks: list,
) -> None:
    rows = "".join(
        f'<tr><td>{i}</td><td>{cs:.2f}</td><td>{ce:.2f}</td>'
        f'<td>{ce - cs:.2f}</td></tr>'
        for i, (cs, ce) in enumerate(chunks)
    )
    peak_str = ", ".join(f"{p.time:.1f}s" for p in peaks) if peaks else "(none)"
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>dry-chunk — {start:.1f}-{end:.1f}s</title>
<style>body{{font-family:sans-serif;background:#111;color:#ddd;padding:20px;}}
h2{{color:#7af;}}
table{{border-collapse:collapse;margin:8px 0;}}
td,th{{padding:4px 12px;border-bottom:1px solid #333;font-family:monospace;}}
th{{text-align:left;color:#aaa;}}</style></head><body>
<h1>dry-chunk — {start:.1f}–{end:.1f}s ({end - start:.1f}s)</h1>
<p>{len(chunks)} chunks · {len(peaks)} audio peaks in range</p>
<h2>Loudness + chunk boundaries</h2>
<embed src="chunks.svg" type="image/svg+xml" width="1200" height="220">
<h2>Chunks</h2>
<table><tr><th>#</th><th>start</th><th>end</th><th>duration</th></tr>{rows}</table>
<h2>Audio peaks in range</h2>
<p style="font-family:monospace;">{peak_str}</p>
</body></html>"""
    path.write_text(html, encoding="utf-8")
