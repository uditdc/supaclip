from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load_dotenv_if_present() -> None:
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:
        return
    path = find_dotenv(usecwd=True)
    if path:
        load_dotenv(path, override=False)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="stitch",
        description="Render short-form videos from a Claude-authored EDL.",
    )
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    pr = sub.add_parser("render", help="render an EDL to an mp4")
    pr.add_argument("edl", help="path to edl.json")
    pr.add_argument("-o", "--output", default=None, help="output mp4 (default: <edl>.mp4)")
    pr.add_argument("--catalog", default=None, help="catalog DB path (default: env/default)")
    pr.add_argument(
        "--cache-dir",
        default=str(Path("~/.cache/supaclip").expanduser()),
    )
    pr.add_argument("--no-cache", action="store_true")
    pr.add_argument("--fontfile", default=None, help="TTF/OTF font for OST overlays")
    pr.add_argument("--api-key", default=None,
                    help="TTS API key (default: env, per the EDL's voiceover.backend)")
    pr.add_argument("-v", "--verbose", action="store_true")
    pr.add_argument("--json", dest="emit_json", action="store_true")
    pr.add_argument("--print-ffmpeg", dest="print_ffmpeg", action="store_true",
                    help="print the ffmpeg command and exit without rendering")
    pr.add_argument("--preview-cue", dest="preview_cue", type=int, default=None,
                    metavar="N",
                    help="render only cue index N (0-based); fast iteration")

    pv = sub.add_parser("validate", help="validate an EDL against the catalog")
    pv.add_argument("edl")
    pv.add_argument("--catalog", default=None)
    pv.add_argument("--json", dest="emit_json", action="store_true")

    pp = sub.add_parser("voice-preview", help="synthesize a one-off TTS sample")
    pp.add_argument("--text", required=True)
    pp.add_argument("--voice-id", required=True)
    pp.add_argument("--backend", default="elevenlabs", choices=["elevenlabs", "google"])
    pp.add_argument("--stability", type=float, default=50.0)
    pp.add_argument("--similarity", type=float, default=75.0)
    pp.add_argument("--style", type=float, default=0.0)
    pp.add_argument("--api-key", default=None)
    pp.add_argument("--cache-dir",
                    default=str(Path("~/.cache/supaclip").expanduser()))
    pp.add_argument("--no-cache", action="store_true")
    pp.add_argument("-o", "--output", default="preview.wav")

    pl = sub.add_parser("voices", help="list available TTS voices")
    pl.add_argument("--backend", default="elevenlabs", choices=["elevenlabs", "google"])
    pl.add_argument("--api-key", default=None)
    pl.add_argument("--json", dest="emit_json", action="store_true")

    return p


def main(argv: list[str] | None = None) -> int:
    _load_dotenv_if_present()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd is None:
        parser.print_help()
        return 2

    if args.cmd == "render":
        return _cmd_render(args)
    if args.cmd == "validate":
        return _cmd_validate(args)
    if args.cmd == "voice-preview":
        return _cmd_voice_preview(args)
    if args.cmd == "voices":
        return _cmd_voices(args)

    parser.error(f"unknown command: {args.cmd}")
    return 2


def _cmd_render(args) -> int:
    from supaclip.core.log import Logger
    from supaclip.stitch.render import RenderConfig, RenderError, render

    log = Logger(verbose=args.verbose)
    edl_path = Path(args.edl)
    if not edl_path.is_file():
        log.error(f"EDL not found: {edl_path}")
        return 2

    output = args.output or str(edl_path.with_suffix(".mp4"))
    api_key = args.api_key

    cfg = RenderConfig(
        edl_path=str(edl_path),
        output_path=output,
        catalog_path=args.catalog,
        cache_dir=args.cache_dir,
        use_cache=not args.no_cache,
        fontfile=args.fontfile,
        api_key=api_key,
        verbose=args.verbose,
        print_only=args.print_ffmpeg,
        preview_cue=args.preview_cue,
    )

    try:
        result = render(cfg, log)
    except RenderError as e:
        log.error(str(e))
        return 1
    except KeyboardInterrupt:
        log.error("interrupted")
        return 130
    except FileNotFoundError as e:
        log.error(str(e))
        return 2
    except Exception as e:  # noqa: BLE001
        log.error(f"{type(e).__name__}: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc(file=sys.stderr)
        return 1

    if args.emit_json:
        json.dump({"output": result.output, "sidecar": result.sidecar,
                   "duration": result.duration}, sys.stdout, indent=2)
        sys.stdout.write("\n")
    return 0


def _cmd_validate(args) -> int:
    from supaclip.catalog.source import SqliteClipSource
    from supaclip.core.edl import load_edl, validate_edl
    from supaclip.core.log import Logger

    log = Logger()
    try:
        edl = load_edl(args.edl)
    except FileNotFoundError as e:
        log.error(str(e))
        return 2

    source = SqliteClipSource.open(args.catalog)
    issues = validate_edl(edl, resolver=source.get_clip)

    if args.emit_json:
        json.dump({
            "ok": not any(i.severity == "error" for i in issues),
            "issues": [i.to_dict() for i in issues],
        }, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        for i in issues:
            (log.error if i.severity == "error" else log.warn)(f"{i.path}: {i.message}")
        if not issues:
            log.success("EDL is valid")

    return 1 if any(i.severity == "error" for i in issues) else 0


def _cmd_voice_preview(args) -> int:
    from supaclip.core.log import Logger
    from supaclip.stitch.tts import get_backend
    from supaclip.stitch.tts.cache import TTSCache

    log = Logger()
    api_key = args.api_key
    settings = {"stability": args.stability,
                "similarity": args.similarity,
                "style": args.style}
    cache = TTSCache(args.cache_dir, enabled=not args.no_cache)
    key = TTSCache.key(args.backend, args.voice_id, settings, args.text)
    cached = cache.get(key)
    if cached is not None:
        log.success(f"cache hit: {cached}")
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        from shutil import copyfile
        copyfile(cached, args.output)
        log.success(f"wrote {args.output}")
        return 0

    backend = get_backend(args.backend, api_key=api_key)
    try:
        backend.synthesize(args.text, args.voice_id, settings, args.output)
    except Exception as e:  # noqa: BLE001
        log.error(f"{type(e).__name__}: {e}")
        return 1
    cache.put(key, args.output) if not args.no_cache else None
    log.success(f"wrote {args.output}")
    return 0


def _cmd_voices(args) -> int:
    from supaclip.core.log import Logger
    from supaclip.stitch.tts import get_backend

    log = Logger()
    api_key = args.api_key
    backend = get_backend(args.backend, api_key=api_key)
    try:
        voices = backend.list_voices()
    except Exception as e:  # noqa: BLE001
        log.error(f"{type(e).__name__}: {e}")
        return 1

    if args.emit_json:
        json.dump([{"voice_id": v.voice_id, "name": v.name,
                    "description": v.description} for v in voices],
                  sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        for v in voices:
            sys.stdout.write(f"{v.voice_id}\t{v.name}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
