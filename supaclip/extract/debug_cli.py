from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _env(*names: str, default: str | None = None) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


def _parse_timestamp(s: str) -> float:
    s = s.strip()
    if not s:
        raise ValueError("empty timestamp")
    parts = s.split(":")
    if len(parts) == 1:
        return float(parts[0])
    if len(parts) == 2:
        return float(parts[0]) * 60 + float(parts[1])
    if len(parts) == 3:
        return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    raise ValueError(f"unrecognized timestamp: {s!r}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="supaclip debug-prompt",
        description=(
            "Dump what the gemma analyzer would send for a segment. By default "
            "the segment is split into audio-aware chunks and each chunk gets its "
            "own debug folder + preview video. Use --dry-chunk to inspect only the "
            "chunk plan (no model artifacts), or --no-chunk to force a single chunk."
        ),
    )
    p.add_argument("video", help="source video file")
    p.add_argument("--start", required=True, help="segment start (SS, MM:SS, or HH:MM:SS)")
    p.add_argument("--end", required=True, help="segment end (SS, MM:SS, or HH:MM:SS)")
    p.add_argument("-o", "--output", default="debug", help="output directory (default: debug/)")
    p.add_argument("--profile", default="gta", help="game profile name or path")
    p.add_argument("--analyzer", choices=("gemma",), default="gemma",
                   help="only `gemma` supports the prepare/send split today")
    p.add_argument("--llm", default=None)
    p.add_argument("--base-url", default=None)
    p.add_argument("--api-key", default=None)
    p.add_argument("--send", action="store_true",
                   help="after dumping, actually send each chunk and save responses")
    p.add_argument("--no-videos", action="store_true",
                   help="skip rendering preview videos")
    p.add_argument("--dry-chunk", action="store_true",
                   help="dump chunk boundaries + audio waveform only (no model calls, "
                        "no frame extraction)")
    p.add_argument("--no-chunk", action="store_true",
                   help="force a single chunk covering the whole range "
                        "(bypasses audio-aware chunking)")
    return p


def _load_dotenv_if_present() -> None:
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:
        return
    path = find_dotenv(usecwd=True)
    if path:
        load_dotenv(path, override=False)


def main(argv: list[str] | None = None) -> int:
    _load_dotenv_if_present()
    args = build_parser().parse_args(argv)

    src = Path(args.video)
    if not src.is_file():
        sys.stderr.write(f"input not found: {src}\n")
        return 2

    try:
        start = _parse_timestamp(args.start)
        end = _parse_timestamp(args.end)
    except ValueError as e:
        sys.stderr.write(f"bad timestamp: {e}\n")
        return 2
    if end <= start:
        sys.stderr.write("--end must be greater than --start\n")
        return 2

    from .backends.gemma import GemmaBackend
    from .debug import write_chunked_debug_dump
    from .dry_chunk import run_dry_chunk
    from .profiles import load_profile

    base_url = args.base_url or _env(
        "LLM_BASE_URL", "OPENAI_BASE_URL",
        default="http://localhost:11434/v1",
    )
    api_key = args.api_key or _env("LLM_API_KEY", "OPENAI_API_KEY")
    llm = args.llm or _env("LLM_MODEL", default="gemma4")

    profile = load_profile(args.profile)
    backend = GemmaBackend(model=llm, base_url=base_url, api_key=api_key)

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.dry_chunk:
        result = run_dry_chunk(str(src), start, end, out_dir)
        sys.stderr.write(
            f"==> Wrote {result.directory}/  ({len(result.chunks)} chunks, "
            f"{result.peak_count} peaks in range)\n"
        )
        sys.stderr.write(f"    open {result.directory}/chunks.html to inspect\n")
        return 0

    sys.stderr.write(
        f"==> Preparing debug dump for {src.name} [{start:.1f}–{end:.1f}s]"
        f"{' (single chunk)' if args.no_chunk else ''}\n"
    )
    result = write_chunked_debug_dump(
        backend, str(src), start, end, profile, out_dir,
        no_chunk=args.no_chunk,
        send=args.send,
        write_videos=not args.no_videos,
    )

    sys.stderr.write(
        f"==> Wrote {result.directory}/  ({result.chunks} chunks, "
        f"{result.total_frames} frames, ~{result.estimated_tokens} tokens)\n"
    )
    sys.stderr.write(f"    open {result.directory}/preview.html to inspect\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
