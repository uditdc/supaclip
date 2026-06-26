from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.ffmpeg import extract_loudness_curve
from .audio import detect_peaks
from .backends.frames import FramesBackend, PreparedRequest
from .chunking import chunk_segment
from .profiles import GameProfile

TOKENS_PER_TILE_ESTIMATE = 256


@dataclass
class DebugWriteResult:
    directory: Path
    frames: int
    estimated_tokens: int


@dataclass
class ChunkedDebugResult:
    directory: Path
    chunks: int
    total_frames: int
    estimated_tokens: int


def write_chunked_debug_dump(
    backend: FramesBackend,
    source_path: str,
    start: float,
    end: float,
    profile: GameProfile,
    out_dir: Path,
    *,
    no_chunk: bool = False,
    send: bool = False,
    write_videos: bool = True,
    target_seconds: float = 30.0,
    max_seconds: float = 45.0,
    overlap_seconds: float = 5.0,
) -> ChunkedDebugResult:
    """End-to-end debug dump: audio → chunks → per-chunk prepare() → combined preview."""
    out_dir.mkdir(parents=True, exist_ok=True)

    samples = extract_loudness_curve(source_path)
    in_range = [(t, db) for (t, db) in samples if start <= t <= end and db > -200]
    peaks = detect_peaks(samples)
    peaks_in_range = [p for p in peaks if start <= p.time <= end]

    if no_chunk:
        chunks = [(start, end)]
    else:
        chunks = chunk_segment(
            start, end, samples,
            target_seconds=target_seconds,
            max_seconds=max_seconds,
            overlap_seconds=overlap_seconds,
        )

    chunks_payload = {
        "source": source_path,
        "segment": {"start": start, "end": end, "duration": end - start},
        "config": {
            "target_seconds": target_seconds,
            "max_seconds": max_seconds,
            "overlap_seconds": overlap_seconds,
            "no_chunk": no_chunk,
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
            {"idx": i, "start": cs, "end": ce, "duration": round(ce - cs, 3)}
            for i, (cs, ce) in enumerate(chunks)
        ],
    }
    (out_dir / "chunks.json").write_text(
        json.dumps(chunks_payload, indent=2), encoding="utf-8",
    )
    _write_chunks_svg(out_dir / "chunks.svg", start, end, in_range, peaks_in_range, chunks)

    full_padded: Path | None = None
    if write_videos and chunks:
        full_padded = out_dir / "source_segment.mp4"
        try:
            _render_full_source_padded(
                source_path, start, end - start, full_padded,
                tile_px=512,
            )
        except subprocess.CalledProcessError as e:
            (out_dir / "video_error.txt").write_text(
                (e.stderr or b"").decode("utf-8", errors="replace"),
                encoding="utf-8",
            )
            full_padded = None

    chunk_results: list[tuple[int, tuple[float, float], DebugWriteResult]] = []
    total_frames = 0
    total_tokens = 0
    for i, (cs, ce) in enumerate(chunks):
        chunk_dir = out_dir / f"chunk_{i:02d}"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        chunk_workdir = chunk_dir / "_frames"
        prepared = backend.prepare(source_path, cs, ce, profile, workdir=chunk_workdir)

        response_text: str | None = None
        if send:
            try:
                response_text = backend.send(prepared)
            except Exception as e:  # noqa: BLE001
                response_text = f"// send failed: {type(e).__name__}: {e}"

        result = write_debug_dump(
            prepared, chunk_dir,
            response_text=response_text,
            write_videos=False,
        )
        chunk_results.append((i, (cs, ce), result))
        total_frames += result.frames
        total_tokens += result.estimated_tokens

        if write_videos and full_padded is not None:
            try:
                _render_frames_view(prepared, chunk_dir / "frames_view.mp4")
                _slice_padded_source(
                    full_padded, cs - start, ce - cs,
                    chunk_dir / "source_segment.mp4",
                )
            except subprocess.CalledProcessError as e:
                (chunk_dir / "video_error.txt").write_text(
                    (e.stderr or b"").decode("utf-8", errors="replace"),
                    encoding="utf-8",
                )

    _write_chunks_html(
        out_dir, source_path, start, end, chunks, peaks_in_range, chunk_results,
        backend.model, profile.name, total_tokens,
    )

    return ChunkedDebugResult(
        directory=out_dir,
        chunks=len(chunks),
        total_frames=total_frames,
        estimated_tokens=total_tokens,
    )


def write_debug_dump(
    prepared: PreparedRequest,
    out_dir: Path,
    *,
    response_text: str | None = None,
    write_videos: bool = True,
) -> DebugWriteResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    (out_dir / "prompt.txt").write_text(prepared.prompt, encoding="utf-8")

    for f in prepared.frames:
        dst = frames_dir / f"f{f.idx:02d}_t{f.t_rel:07.3f}.jpg"
        shutil.copyfile(f.main_path, dst)

    if prepared.sprite_path is not None and prepared.sprite_path.exists():
        shutil.copyfile(prepared.sprite_path, out_dir / "sprite.jpg")

    summary = _build_summary(prepared)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8",
    )

    request_body = _build_request_body(prepared)
    (out_dir / "request.json").write_text(
        json.dumps(request_body, indent=2), encoding="utf-8",
    )

    if response_text is not None:
        (out_dir / "response.json").write_text(response_text, encoding="utf-8")

    if write_videos and prepared.frames:
        try:
            _render_frames_view(prepared, out_dir / "frames_view.mp4")
            _render_source_segment(prepared, out_dir / "source_segment.mp4")
        except subprocess.CalledProcessError as e:
            (out_dir / "video_error.txt").write_text(
                (e.stderr or b"").decode("utf-8", errors="replace"),
                encoding="utf-8",
            )

    _write_html(prepared, out_dir, summary)

    return DebugWriteResult(
        directory=out_dir,
        frames=len(prepared.frames),
        estimated_tokens=summary["tokens"]["estimated_total"],
    )


def _build_summary(prepared: PreparedRequest) -> dict[str, Any]:
    prompt_tokens = max(1, len(prepared.prompt) // 4)
    image_tokens = len(prepared.frames) * TOKENS_PER_TILE_ESTIMATE
    return {
        "backend": prepared.backend,
        "model": prepared.model,
        "profile": prepared.profile_name,
        "segment": {
            "start": prepared.segment[0],
            "end": prepared.segment[1],
            "duration": prepared.duration,
        },
        "source": prepared.source_path,
        "config": prepared.config,
        "frames": [
            {
                "idx": f.idx,
                "t_rel": round(f.t_rel, 3),
                "t_abs": round(f.t_abs, 3),
            }
            for f in prepared.frames
        ],
        "tokens": {
            "prompt_text_estimate": prompt_tokens,
            "images": 1,
            "grid": list(prepared.grid),
            "estimated_total": prompt_tokens + image_tokens,
            "tokens_per_tile_assumed": TOKENS_PER_TILE_ESTIMATE,
            "method": "256 tokens/cell × cells in the sprite (verify with usage.prompt_tokens after one real call)",
        },
        "ffmpeg_command": prepared.ffmpeg_command,
    }


def _build_request_body(prepared: PreparedRequest) -> dict[str, Any]:
    cols, rows = prepared.grid
    content: list[dict[str, Any]] = [
        {"type": "text", "text": prepared.prompt},
        {
            "type": "image_url",
            "image_url": {"url": f"<sprite: {cols}x{rows} grid of {len(prepared.frames)} frames — sprite.jpg>"},
        },
    ]
    return {
        "model": prepared.model,
        "base_url": prepared.base_url,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": "You are a careful video analyst. Reply with JSON only."},
            {"role": "user", "content": content},
        ],
    }


def _render_frames_view(prepared: PreparedRequest, out_path: Path) -> None:
    frames = prepared.frames
    if not frames:
        return
    listfile = out_path.with_suffix(".concat.txt")
    with listfile.open("w", encoding="utf-8") as fh:
        for f in frames:
            fh.write(f"file '{f.main_path.resolve()}'\n")
            fh.write("duration 1.0\n")
        fh.write(f"file '{frames[-1].main_path.resolve()}'\n")
    total = float(len(frames))
    try:
        subprocess.run([
            "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(listfile),
            "-vsync", "cfr", "-r", "30", "-t", f"{total:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            str(out_path),
        ], check=True, capture_output=True)
    finally:
        try:
            listfile.unlink()
        except OSError:
            pass


def _render_source_segment(prepared: PreparedRequest, out_path: Path) -> None:
    if not prepared.source_path:
        return
    tile = prepared.config["tile_px"]
    start = prepared.segment[0]
    duration = prepared.duration
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", prepared.source_path,
        "-t", f"{duration:.3f}",
        "-vf",
        f"scale={tile}:{tile}:force_original_aspect_ratio=decrease,"
        f"pad={tile}:{tile}:(ow-iw)/2:(oh-ih)/2:color=gray",
        "-r", "30",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-an",
        str(out_path),
    ], check=True, capture_output=True)


def _write_html(prepared: PreparedRequest, out_dir: Path, summary: dict[str, Any]) -> None:
    rows = []
    for f in prepared.frames:
        rows.append(
            f'<div style="display:flex;align-items:center;margin:4px 0;">'
            f'<img src="frames/f{f.idx:02d}_t{f.t_rel:07.3f}.jpg" '
            f'style="height:140px;border:1px solid #444;">'
            f'<div style="margin-left:12px;font-family:monospace;font-size:12px;">'
            f't={f.t_rel:.2f}s'
            f'</div></div>'
        )

    tok = summary["tokens"]
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>supaclip debug — {prepared.segment[0]:.1f}-{prepared.segment[1]:.1f}s</title>
<style>body{{font-family:sans-serif;background:#111;color:#ddd;padding:20px;}}
h2{{color:#7af;}} pre{{background:#222;padding:10px;border-radius:4px;overflow:auto;}}
video{{max-width:100%;border:1px solid #444;}}</style></head><body>
<h1>supaclip debug — segment {prepared.segment[0]:.1f}–{prepared.segment[1]:.1f}s ({prepared.duration:.1f}s)</h1>
<p>model: <code>{prepared.model}</code> · profile: <code>{prepared.profile_name}</code></p>

<h2>Stats</h2>
<pre>frames sent: {len(prepared.frames)} in 1 sprite ({prepared.grid[0]}×{prepared.grid[1]} grid)
est tokens:  {tok['estimated_total']} (prompt {tok['prompt_text_estimate']} + {len(prepared.frames)} cells @ {tok['tokens_per_tile_assumed']} tok/cell)
tile size:   {prepared.config['tile_px']}² per cell (square — no pan-and-scan)</pre>

<h2>Source segment vs. what the model sees</h2>
<div style="display:flex;gap:12px;flex-wrap:wrap;">
  <div><div style="font-family:monospace;color:#7af;">SOURCE ({prepared.duration:.1f}s)</div>
       <video controls width="448" src="source_segment.mp4"></video></div>
  <div><div style="font-family:monospace;color:#7af;">FRAMES PREVIEW ({len(prepared.frames)} frames × 1s)</div>
       <video controls width="448" src="frames_view.mp4"></video></div>
</div>

<h2>Sprite sent to the model ({prepared.grid[0]}×{prepared.grid[1]} grid)</h2>
<img src="sprite.jpg" style="max-width:100%;border:1px solid #444;">

<h2>Frames</h2>
{''.join(rows)}

<h2>Prompt</h2>
<pre>{_html_escape(prepared.prompt)}</pre>
</body></html>"""
    (out_dir / "preview.html").write_text(html, encoding="utf-8")


def _render_full_source_padded(
    source_path: str, start: float, duration: float, out_path: Path,
    *, tile_px: int = 512,
) -> None:
    """Decode source once → h264 padded to square at tile_px. Reused as the cut basis."""
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", source_path,
        "-t", f"{duration:.3f}",
        "-vf",
        f"scale={tile_px}:{tile_px}:force_original_aspect_ratio=decrease,"
        f"pad={tile_px}:{tile_px}:(ow-iw)/2:(oh-ih)/2:color=gray",
        "-r", "30",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-an",
        "-g", "30",
        str(out_path),
    ], check=True, capture_output=True)


def _slice_padded_source(full_padded: Path, offset: float, duration: float, out_path: Path) -> None:
    """Cut a sub-clip from the pre-rendered padded source via stream copy. ~instant."""
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
        "-ss", f"{offset:.3f}", "-i", str(full_padded),
        "-t", f"{duration:.3f}",
        "-c", "copy", "-avoid_negative_ts", "make_zero",
        str(out_path),
    ], check=True, capture_output=True)


def _write_chunks_svg(
    path: Path,
    start: float,
    end: float,
    in_range: list[tuple[float, float]],
    peaks: list,
    chunks: list[tuple[float, float]],
) -> None:
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

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" style="background:#111;font-family:monospace;">',
        f'<rect x="{pad_l}" y="{pad_t}" width="{plot_w}" height="{plot_h}" '
        f'fill="#1a1a1a" stroke="#333"/>',
    ]

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


def _write_chunks_html(
    out_dir: Path,
    source_path: str,
    start: float,
    end: float,
    chunks: list[tuple[float, float]],
    peaks: list,
    chunk_results: list[tuple[int, tuple[float, float], DebugWriteResult]],
    model: str,
    profile_name: str,
    total_tokens: int,
) -> None:
    colors = ["#e84", "#4e8", "#8e4", "#4ae", "#a4e", "#ea4", "#8a4", "#4e4"]
    chunk_sections: list[str] = []
    for i, (cs, ce), result in chunk_results:
        color = colors[i % len(colors)]
        chunk_sections.append(
            f'<section style="border-left:4px solid {color};padding:10px 16px;margin:14px 0;'
            f'background:#181818;">'
            f'<h3 style="margin:0 0 8px 0;color:{color};">'
            f'chunk {i:02d} · {cs:.1f}–{ce:.1f}s ({ce - cs:.1f}s) · '
            f'{result.frames} frames · ~{result.estimated_tokens} tokens'
            f'</h3>'
            f'<div style="display:flex;gap:12px;flex-wrap:wrap;align-items:flex-start;">'
            f'  <div><div style="font-family:monospace;color:#888;">SOURCE</div>'
            f'       <video controls width="384" src="chunk_{i:02d}/source_segment.mp4"></video></div>'
            f'  <div><div style="font-family:monospace;color:#888;">FRAMES PREVIEW ({result.frames}×1s)</div>'
            f'       <video controls width="384" src="chunk_{i:02d}/frames_view.mp4"></video></div>'
            f'  <div><div style="font-family:monospace;color:#888;">SPRITE</div>'
            f'       <img src="chunk_{i:02d}/sprite.jpg" style="height:216px;border:1px solid #444;"></div>'
            f'  <div style="font-family:monospace;font-size:12px;color:#aaa;">'
            f'    <a href="chunk_{i:02d}/preview.html" style="color:{color};">chunk preview ▸</a><br>'
            f'    <a href="chunk_{i:02d}/prompt.txt" style="color:#888;">prompt.txt</a><br>'
            f'    <a href="chunk_{i:02d}/request.json" style="color:#888;">request.json</a><br>'
            f'    <a href="chunk_{i:02d}/summary.json" style="color:#888;">summary.json</a>'
            f'  </div>'
            f'</div></section>'
        )

    src_name = Path(source_path).name
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>supaclip debug — {src_name} [{start:.1f}-{end:.1f}s]</title>
<style>body{{font-family:sans-serif;background:#111;color:#ddd;padding:20px;max-width:1300px;}}
h1{{margin-top:0;}} h2{{color:#7af;margin-top:28px;}}
pre{{background:#222;padding:10px;border-radius:4px;overflow:auto;}}
video{{border:1px solid #444;background:#000;}}
a{{text-decoration:none;}} a:hover{{text-decoration:underline;}}</style></head><body>
<h1>{src_name} · {start:.1f}–{end:.1f}s ({end - start:.1f}s)</h1>
<p>model: <code>{model}</code> · profile: <code>{profile_name}</code> · {len(chunks)} chunks · {len(peaks)} audio peaks · ~{total_tokens} total tokens</p>

<h2>Audio waveform + chunk boundaries</h2>
<embed src="chunks.svg" type="image/svg+xml" width="1200" height="220">

<h2>Source segment</h2>
<video controls width="800" src="source_segment.mp4"></video>

<h2>Per-chunk view</h2>
{''.join(chunk_sections)}
</body></html>"""
    (out_dir / "preview.html").write_text(html, encoding="utf-8")


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
