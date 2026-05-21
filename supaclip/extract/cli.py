from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _env(*names: str, default: str | None = None) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="extract",
        description="Split local gameplay video into clips and a manifest.json catalog.",
    )
    p.add_argument("videos", metavar="VIDEO", nargs="+", help="local video file(s)")
    p.add_argument("-o", "--output", default="clips", help="output directory (default: clips)")
    p.add_argument("--segmenter", choices=("auto", "manual", "scene", "interval", "file"), default="auto")
    p.add_argument("--timestamps", help="start,end pairs file (required for --segmenter manual)")
    p.add_argument("--interval", type=float, default=60.0, help="window length for interval strategy")
    p.add_argument("--game-profile", default="gta", help="built-in profile name or path to JSON")
    p.add_argument("--analyzer", choices=("gemma", "gemma-video"), default="gemma-video",
                   help="default: gemma-video (Google AI Studio, requires GEMINI_API_KEY); "
                        "use `gemma` for the OpenAI-compatible frames fallback")
    p.add_argument("--llm", default=None, help="analyzer model id (default: env LLM_MODEL or gemma4)")
    p.add_argument("--base-url", default=None, help="OpenAI-compatible endpoint")
    p.add_argument("--api-key", default=None, help="API key (unused for local Ollama)")
    p.add_argument("--keyframes", type=int, default=3)
    p.add_argument("--dedup-iou", type=float, default=0.6)
    p.add_argument("--no-dedup", action="store_true")
    p.add_argument("--no-chunk", action="store_true",
                   help="disable audio-trough chunking + aggregator pass for long segments")
    p.add_argument("--min-clip", type=float, default=15.0)
    p.add_argument("--max-clip", type=float, default=60.0)
    p.add_argument("--max-duration", type=float, default=5400.0)
    p.add_argument(
        "--cache-dir",
        default=str(Path("~/.cache/supaclip").expanduser()),
    )
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--keep-temp", action="store_true")
    p.add_argument("--json", dest="emit_json", action="store_true",
                   help="print the manifest(s) to stdout on completion")
    p.add_argument("-v", "--verbose", action="store_true")
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
    parser = build_parser()
    args = parser.parse_args(argv)

    from .pipeline import ExtractConfig, run
    from ..core.log import Logger

    log = Logger(verbose=args.verbose)

    base_url = args.base_url or _env(
        "LLM_BASE_URL", "OPENAI_BASE_URL",
        default="http://localhost:11434/v1",
    )
    api_key = args.api_key or _env("LLM_API_KEY", "OPENAI_API_KEY")
    llm = args.llm or _env("LLM_MODEL", default="gemma4")

    if args.segmenter == "manual" and not args.timestamps:
        parser.error("--segmenter manual requires --timestamps FILE")

    for v in args.videos:
        if not Path(v).is_file():
            log.error(f"input not found: {v}")
            return 2

    cfg = ExtractConfig(
        videos=[str(Path(v).resolve()) for v in args.videos],
        output_dir=args.output,
        segmenter=args.segmenter,
        timestamps_file=args.timestamps,
        interval=args.interval,
        game_profile=args.game_profile,
        analyzer=args.analyzer,
        llm=llm,
        base_url=base_url,
        api_key=api_key,
        keyframes=args.keyframes,
        dedup_iou=args.dedup_iou,
        no_dedup=args.no_dedup,
        min_clip=args.min_clip,
        max_clip=args.max_clip,
        max_duration=args.max_duration,
        cache_dir=args.cache_dir,
        no_cache=args.no_cache,
        keep_temp=args.keep_temp,
        verbose=args.verbose,
        no_chunk=args.no_chunk,
    )

    try:
        manifests = run(cfg, log)
    except KeyboardInterrupt:
        log.error("interrupted")
        return 130
    except SystemExit as e:
        log.error(str(e))
        return 2 if isinstance(e.code, str) else int(e.code or 1)
    except FileNotFoundError as e:
        log.error(str(e))
        return 2
    except Exception as e:  # noqa: BLE001
        log.error(f"{type(e).__name__}: {e}")
        cause = e.__cause__ or e.__context__
        while cause is not None:
            log.error(f"  caused by {type(cause).__name__}: {cause}")
            cause = cause.__cause__ or cause.__context__
        body = getattr(getattr(e, "response", None), "text", None)
        if body:
            log.error(f"  response body: {body[:1000]}")
        if args.verbose:
            import traceback
            traceback.print_exc(file=sys.stderr)
        return 1

    if args.emit_json:
        payload = [m.model_dump(mode="json") for m in manifests]
        json.dump(payload if len(payload) > 1 else payload[0], sys.stdout, indent=2)
        sys.stdout.write("\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
