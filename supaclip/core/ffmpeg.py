from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class FFmpegError(RuntimeError):
    pass


@dataclass
class VideoInfo:
    path: str
    width: int
    height: int
    duration: float
    fps: float
    has_audio: bool

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"


def ensure_ffmpeg() -> None:
    missing = [b for b in ("ffmpeg", "ffprobe") if shutil.which(b) is None]
    if missing:
        names = " and ".join(missing)
        raise FFmpegError(
            f"Required binary not found: {names}. Install ffmpeg "
            "(e.g. `sudo apt install ffmpeg` or `brew install ffmpeg`)."
        )


def _run(cmd: list[str], capture: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            check=True,
            text=True,
            capture_output=capture,
        )
    except FileNotFoundError as e:
        raise FFmpegError(str(e)) from e
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or "").strip().splitlines()[-12:]
        raise FFmpegError(
            f"{cmd[0]} failed (exit {e.returncode}):\n" + "\n".join(tail)
        ) from e


def probe(path: str | Path) -> VideoInfo:
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise FFmpegError(f"Input not found: {p}")
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(p),
    ]
    out = _run(cmd).stdout
    data = json.loads(out)
    streams = data.get("streams", [])
    fmt = data.get("format", {})

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not video:
        raise FFmpegError(f"No video stream in {p}")

    duration = float(fmt.get("duration") or video.get("duration") or 0.0)
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    fps = _parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1")

    return VideoInfo(
        path=str(p.resolve()),
        width=width,
        height=height,
        duration=duration,
        fps=fps,
        has_audio=audio is not None,
    )


def _parse_fps(rate: str) -> float:
    try:
        num, den = rate.split("/")
        d = float(den)
        return float(num) / d if d else 0.0
    except (ValueError, ZeroDivisionError):
        try:
            return float(rate)
        except ValueError:
            return 0.0


def extract_loudness_curve(video_path: str | Path, hop_seconds: float = 0.5) -> list[tuple[float, float]]:
    """Sample short-term loudness with the astats filter.

    Returns list of (timestamp_seconds, rms_db) measurements.
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(video_path),
        "-vn",
        "-af",
        f"aresample=8000,asetnsamples=n={int(8000 * hop_seconds)}:p=0,"
        "astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level",
        "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as e:
        raise FFmpegError(str(e)) from e
    out: list[tuple[float, float]] = []
    current_t: float | None = None
    for line in (proc.stderr or "").splitlines():
        line = line.strip()
        if line.startswith("[") and "]" in line:
            line = line.split("]", 1)[1].strip()
        if "pts_time:" in line:
            for tok in line.split():
                if tok.startswith("pts_time:"):
                    try:
                        current_t = float(tok.split(":", 1)[1])
                    except ValueError:
                        current_t = None
                    break
        elif "lavfi.astats.Overall.RMS_level=" in line:
            try:
                val = float(line.split("=", 1)[1])
            except ValueError:
                continue
            if current_t is not None:
                out.append((current_t, val))
    return out


def extract_keyframes(
    video_path: str | Path,
    start: float,
    end: float,
    count: int,
    out_pattern: str,
) -> list[str]:
    """Extract `count` JPEG keyframes between start and end. Returns the written paths."""
    if count <= 0 or end <= start:
        return []
    span = end - start
    paths: list[str] = []
    for i in range(count):
        # midpoints of equal slices
        offset = start + span * (i + 0.5) / count
        out_path = out_pattern.format(i=i + 1)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
            "-ss", f"{offset:.3f}", "-i", str(video_path),
            "-frames:v", "1", "-q:v", "3", out_path,
        ]
        _run(cmd)
        paths.append(out_path)
    return paths


def extract_subtitle_text(video_path: str | Path, stream_index: int = 0) -> str | None:
    """Extract an embedded subtitle stream as WebVTT text.

    Returns the cue text for `0:s:<stream_index>`, or None if the stream is
    absent or not a text subtitle (image subtitles like PGS can't be converted).
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(video_path), "-map", f"0:s:{stream_index}",
        "-f", "webvtt", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as e:
        raise FFmpegError(str(e)) from e
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return None
    return proc.stdout


def list_encoders() -> set[str]:
    """Return the set of encoder names this ffmpeg build can use (`ffmpeg -encoders`)."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError as e:
        raise FFmpegError(str(e)) from e

    names: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] == "V":
            names.add(parts[1])
    return names


def probe_encoder(codec: str) -> bool:
    """Return True if ffmpeg can actually initialize `codec` (real 1-frame encode).

    `ffmpeg -encoders` lists codecs compiled in, not ones that work here: a build
    can advertise h264_nvenc with no GPU present. This catches that.
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=black:s=64x64:r=1:d=1",
        "-c:v", codec, "-frames:v", "1", "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def run_ffmpeg(args: list[str]) -> str:
    """Run an `ffmpeg` invocation. `args` should NOT include the leading `ffmpeg`.
    Returns stderr text (where ffmpeg writes progress/info). Raises FFmpegError on non-zero exit."""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error", *args]
    proc = _run(cmd)
    return proc.stderr or ""


def concat_demux(inputs: list[str | Path], out_path: str | Path) -> None:
    """Stream-copy concat using the concat demuxer. All inputs must share codec params."""
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    listfile = Path(out_path).with_suffix(".concat.txt")
    with listfile.open("w", encoding="utf-8") as fh:
        for p in inputs:
            fh.write(f"file '{Path(p).resolve()}'\n")
    try:
        run_ffmpeg([
            "-f", "concat", "-safe", "0", "-i", str(listfile),
            "-c", "copy", str(out_path),
        ])
    finally:
        try:
            listfile.unlink()
        except OSError:
            pass


def cut_clip(
    video_path: str | Path,
    start: float,
    end: float,
    out_path: str | Path,
) -> None:
    """Cut [start, end) into out_path. Stream-copies when possible (snaps to the
    nearest input keyframe ≤ start; ffmpeg writes an mp4 edit list so players
    still begin at the requested time)."""
    duration = max(0.0, end - start)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", str(video_path),
        "-t", f"{duration:.3f}",
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        str(out_path),
    ]
    _run(cmd)
