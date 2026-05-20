from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from clipper.core.ffmpeg import FFmpegError


@dataclass
class ProgressEvent:
    out_time_ms: int
    frame: int | None
    fps: float | None
    bitrate: str | None
    speed: str | None
    pct: float | None  # 0..1 when total_duration known


ProgressCallback = Callable[[ProgressEvent], None]


def run_ffmpeg_with_progress(
    args: list[str],
    total_duration: float | None,
    callback: ProgressCallback | None = None,
) -> str:
    """Run ffmpeg with `-progress pipe:1` and parse key=value blocks on stdout.

    Each completed block (terminated by `progress=continue` or
    `progress=end`) is forwarded to `callback`. Returns the captured stderr
    on success; raises FFmpegError on non-zero exit.
    """
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error",
           "-progress", "pipe:1", *args]

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )

    state: dict[str, str] = {}
    stderr_chunks: list[str] = []

    def drain_stderr():
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_chunks.append(line)

    err_thread = threading.Thread(target=drain_stderr, daemon=True)
    err_thread.start()

    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            state[k.strip()] = v.strip()
        if state.get("progress") in {"continue", "end"} and callback is not None:
            evt = _build_event(state, total_duration)
            try:
                callback(evt)
            except Exception:
                pass
            if state["progress"] == "end":
                break
            state.pop("progress", None)

    rc = proc.wait()
    err_thread.join(timeout=1.0)
    stderr_text = "".join(stderr_chunks)

    if rc != 0:
        tail = "\n".join(stderr_text.strip().splitlines()[-12:])
        raise FFmpegError(f"ffmpeg failed (exit {rc}):\n{tail}")
    return stderr_text


def _build_event(state: dict[str, str], total_duration: float | None) -> ProgressEvent:
    out_time_us = _as_int(state.get("out_time_us")) or 0
    out_time_ms = out_time_us // 1000 if out_time_us else _as_int(state.get("out_time_ms")) or 0
    pct: float | None = None
    if total_duration and total_duration > 0:
        pct = max(0.0, min(1.0, (out_time_ms / 1000.0) / total_duration))
    return ProgressEvent(
        out_time_ms=out_time_ms,
        frame=_as_int(state.get("frame")),
        fps=_as_float(state.get("fps")),
        bitrate=state.get("bitrate"),
        speed=state.get("speed"),
        pct=pct,
    )


def _as_int(s: str | None) -> int | None:
    if s is None or s == "N/A":
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _as_float(s: str | None) -> float | None:
    if s is None or s == "N/A":
        return None
    try:
        return float(s)
    except ValueError:
        return None
