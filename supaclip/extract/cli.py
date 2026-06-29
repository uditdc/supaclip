from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .profiles import VideoContext


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
    p.add_argument(
        "--video-intro", default=None,
        help="short text describing this video to give the analyzer context (synopsis, era, setting)",
    )
    p.add_argument(
        "--video-intro-file", default=None,
        help="path to a text file whose contents become the video intro",
    )
    p.add_argument(
        "--character", action="append", default=[], metavar="SPEC",
        help=(
            'reference character: "name=img1[,img2,...][:description]". '
            "Repeatable; repeats with the same name merge their images."
        ),
    )
    p.add_argument(
        "--context-file", default=None,
        help='JSON file with {"intro": "...", "characters": [{"name", "image", "description"}]}',
    )
    p.add_argument("--analyzer", choices=("video", "frames"), default="frames",
                   help="default: frames (short-frame analysis — a model-agnostic "
                        "sprite grid sent to any OpenAI-compatible endpoint); use "
                        "`video` for full-video analysis via Google AI Studio "
                        "(requires GEMINI_API_KEY)")
    p.add_argument("--llm", default=None, help="analyzer model id (default: env LLM_MODEL or gemma4)")
    p.add_argument("--base-url", default=None, help="OpenAI-compatible endpoint")
    p.add_argument("--api-key", default=None, help="API key (unused for local Ollama)")
    p.add_argument("--subtitles", default=None,
                   help="path to an .srt/.vtt file; default auto-detects a sidecar "
                        "next to the video, then an embedded subtitle stream")
    p.add_argument("--no-subtitles", action="store_true",
                   help="skip subtitle ingestion (descriptions will be vision-only)")
    p.add_argument("--no-summary", action="store_true",
                   help="skip the whole-source synopsis/theme/beat-sheet rollup pass")
    p.add_argument("--keyframes", type=int, default=3)
    p.add_argument("--dedup-iou", type=float, default=0.6)
    p.add_argument("--no-dedup", action="store_true")
    p.add_argument("--no-chunk", action="store_true",
                   help="disable audio-trough chunking + aggregator pass for long segments")
    p.add_argument("--analyze-concurrency", type=int, default=4,
                   help="number of analysis chunks to send to the vision model in "
                        "parallel (default: 4; lower if you hit rate limits)")
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


def _parse_character_spec(spec: str) -> dict[str, Any]:
    """Parse 'name=path1[,path2,...][:description]' into a Character-shaped dict.

    Multiple images are comma-separated. The trailing ':description' is detected
    by splitting on the LAST ':' and checking that the left side is a list of
    comma-separated existing files; otherwise the whole RHS is treated as paths.
    """
    if "=" not in spec:
        raise ValueError(
            f"--character spec '{spec}' must look like 'name=path[,path...][:description]'"
        )
    name, rest = spec.split("=", 1)
    name = name.strip()
    if not name:
        raise ValueError(f"--character spec '{spec}' is missing a name")
    rest = rest.strip()
    paths_str = rest
    description = ""
    if ":" in rest:
        candidate_paths, candidate_desc = rest.rsplit(":", 1)
        candidates = [s.strip() for s in candidate_paths.split(",") if s.strip()]
        if candidates and all(Path(s).expanduser().is_file() for s in candidates):
            paths_str = candidate_paths
            description = candidate_desc.strip()
    images = [s.strip() for s in paths_str.split(",") if s.strip()]
    return {"name": name, "images": images, "description": description}


def _build_video_context(args) -> VideoContext | None:
    from .profiles import Character, VideoContext

    intro = (args.video_intro or "").strip()
    if args.video_intro_file:
        intro_path = Path(args.video_intro_file).expanduser()
        intro = intro_path.read_text(encoding="utf-8").strip()

    by_name: dict[str, Character] = {}
    order: list[str] = []

    def _add(ch: Character) -> None:
        existing = by_name.get(ch.name)
        if existing is None:
            by_name[ch.name] = ch
            order.append(ch.name)
            return
        # merge: union of images, prefer non-empty description
        seen = set(existing.images)
        for img in ch.images:
            if img not in seen:
                existing.images.append(img)
                seen.add(img)
        if not existing.description and ch.description:
            existing.description = ch.description

    if args.context_file:
        bundled = VideoContext.load(args.context_file)
        if not intro:
            intro = bundled.intro
        for ch in bundled.characters:
            _add(ch)

    for spec in args.character or []:
        _add(Character.model_validate(_parse_character_spec(spec)))

    ctx = VideoContext(intro=intro, characters=[by_name[n] for n in order])
    return None if ctx.is_empty() else ctx


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

    from ..core.log import Logger
    from .pipeline import ExtractConfig, run

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

    try:
        context = _build_video_context(args)
    except (ValueError, OSError) as e:
        log.error(f"invalid context: {e}")
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
        analyze_concurrency=args.analyze_concurrency,
        context=context,
        subtitles=args.subtitles,
        no_subtitles=args.no_subtitles,
        no_summary=args.no_summary,
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
