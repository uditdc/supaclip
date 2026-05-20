from __future__ import annotations

import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clipper.catalog import connect, resolve_catalog_path
from clipper.catalog.search import ClipRow, get_clip
from clipper.core.edl import EDL, load_edl, save_edl, validate_edl
from clipper.core.ffmpeg import ensure_ffmpeg, probe, run_ffmpeg
from clipper.stitch.progress import ProgressEvent, run_ffmpeg_with_progress
from clipper.core.log import Logger
from clipper.stitch.assembly import CueInput, RenderInputs, build_command
from clipper.stitch.music import build_music_plan, resolve_music_file
from clipper.stitch.tts import get_backend
from clipper.stitch.tts.cache import TTSCache


DEFAULT_CACHE_DIR = "~/.cache/supaclip"


@dataclass
class RenderConfig:
    edl_path: str
    output_path: str
    catalog_path: str | None = None
    cache_dir: str = DEFAULT_CACHE_DIR
    use_cache: bool = True
    fontfile: str | None = None
    api_key: str | None = None
    verbose: bool = False
    print_only: bool = False
    preview_cue: int | None = None      # 0-based index; if set, only that cue is rendered


@dataclass
class RenderResult:
    output: str
    sidecar: str
    duration: float


class RenderError(RuntimeError):
    pass


def render(
    config: RenderConfig,
    log: Logger | None = None,
    progress: callable | None = None,
) -> RenderResult:
    log = log or Logger(verbose=config.verbose)
    ensure_ffmpeg()

    log.stage("load EDL")
    edl = load_edl(config.edl_path)
    log.info(f"title: {edl.title!r}  duration: {edl.output.duration:.1f}s  "
             f"out: {edl.output.width}x{edl.output.height}@{edl.output.fps}")

    if config.preview_cue is not None:
        if not (0 <= config.preview_cue < len(edl.video)):
            raise RenderError(
                f"--preview-cue {config.preview_cue} out of range "
                f"(0..{len(edl.video)-1})"
            )
        edl = _trim_to_single_cue(edl, config.preview_cue)
        log.info(f"preview mode: rendering only cue #{config.preview_cue}")

    log.stage("connect catalog")
    catalog_path = resolve_catalog_path(config.catalog_path)
    log.info(f"catalog: {catalog_path}")
    conn = connect(catalog_path)

    log.stage("validate EDL")
    resolver = _build_resolver(conn)
    issues = validate_edl(edl, resolver=resolver)
    errors = [i for i in issues if i.severity == "error"]
    for i in issues:
        (log.error if i.severity == "error" else log.warn)(f"{i.path}: {i.message}")
    if errors:
        raise RenderError(f"EDL has {len(errors)} validation error(s); refusing to render")

    log.stage("resolve clips")
    cue_inputs = _resolve_cue_inputs(conn, edl, log)

    voiceover_wav: Path | None = None
    if edl.voiceover is not None:
        log.stage("synthesize voiceover")
        voiceover_wav = _synthesize(edl, config, log)

    music_path: str | None = None
    music_plan = None
    if edl.music is not None:
        log.stage("resolve music")
        music_path = resolve_music_file(edl.music.file, conn)
        log.info(f"music: {music_path}")
        music_plan = build_music_plan(
            music=edl.music,
            music_input_index=0,
            duration=edl.output.duration,
            voiceover_sidechain_label=None,
        )

    log.stage("render")
    inputs = RenderInputs(
        edl=edl, cues=cue_inputs,
        voiceover_wav=str(voiceover_wav) if voiceover_wav else None,
        fontfile=config.fontfile,
        music_path=music_path,
        music_plan=music_plan,
    )
    out_path = Path(config.output_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    args = build_command(inputs, out_path)
    if config.print_only:
        import sys
        sys.stdout.write("ffmpeg " + " ".join(_shell_quote(a) for a in args) + "\n")
        sys.stdout.flush()
        log.info("--print-ffmpeg: command emitted; no render run")
        sidecar = out_path.with_suffix(out_path.suffix + ".edl.json")
        save_edl(edl, sidecar)
        return RenderResult(output=str(out_path), sidecar=str(sidecar),
                            duration=edl.output.duration)

    log.detail("ffmpeg " + " ".join(args))
    callback = _default_progress(log) if progress is None else progress
    run_ffmpeg_with_progress(args, total_duration=edl.output.duration,
                              callback=callback)
    log.success(f"wrote {out_path}")

    sidecar = out_path.with_suffix(out_path.suffix + ".edl.json")
    save_edl(edl, sidecar)
    log.success(f"sidecar {sidecar}")

    return RenderResult(
        output=str(out_path),
        sidecar=str(sidecar),
        duration=edl.output.duration,
    )


def _build_resolver(conn: sqlite3.Connection):
    def resolve(clip_id: int) -> ClipRow | None:
        return get_clip(conn, clip_id)
    return resolve


def _resolve_cue_inputs(conn: sqlite3.Connection, edl: EDL, log: Logger) -> list[CueInput]:
    cues: list[CueInput] = []
    probe_cache: dict[str, Any] = {}
    for i, cue in enumerate(edl.video):
        clip = get_clip(conn, cue.clip_id)
        if clip is None:
            raise RenderError(f"video[{i}]: clip_id={cue.clip_id} not found")
        if clip.file not in probe_cache:
            probe_cache[clip.file] = probe(clip.file)
        info = probe_cache[clip.file]
        src_in = cue.source_in if cue.source_in is not None else float(clip.source_in or 0.0)
        log.detail(
            f"video[{i}] -> {clip.clip_local_id} ({Path(clip.file).name}) "
            f"src_in={src_in:.2f} dur={(cue.end - cue.start):.2f}"
        )
        cues.append(CueInput(
            file_path=clip.file,
            cue=cue,
            cue_start=cue.start,
            cue_end=cue.end,
            source_in=src_in,
            src_w=info.width,
            src_h=info.height,
            reframe=cue.reframe,
        ))
    return cues


def _trim_to_single_cue(edl: EDL, idx: int) -> EDL:
    """Return a copy of `edl` containing only video cue `idx`, with the
    timeline collapsed so the cue starts at 0 and output.duration matches.
    OST/audio/annotation cues that overlap the original window are preserved
    with shifted timestamps; others are dropped.
    """
    src = edl.video[idx]
    shift = src.start
    cue_dur = src.end - src.start
    new_video = [src.model_copy(update={"start": 0.0, "end": cue_dur,
                                          "transition_in": "cut",
                                          "transition_duration": 0.0})]

    def _shift_cue(cue):
        if cue.end <= src.start or cue.start >= src.end:
            return None
        ns = max(0.0, cue.start - shift)
        ne = min(cue_dur, cue.end - shift)
        if ne - ns <= 1e-3:
            return None
        return cue.model_copy(update={"start": ns, "end": ne})

    audio = [c for c in (_shift_cue(c) for c in edl.audio) if c is not None]
    ost = [c for c in (_shift_cue(c) for c in edl.ost) if c is not None]
    annotations = [c for c in (_shift_cue(c) for c in edl.annotations) if c is not None]

    return edl.model_copy(update={
        "output": edl.output.model_copy(update={"duration": cue_dur}),
        "video": new_video,
        "audio": audio,
        "ost": ost,
        "annotations": annotations,
    })


def _shell_quote(s: str) -> str:
    """Minimal shell-safe quoting for --print-ffmpeg output."""
    if not s or any(c in s for c in " \"'`$;|&<>()[]{}\\*?#~"):
        return "'" + s.replace("'", "'\\''") + "'"
    return s


def _default_progress(log: Logger):
    last_pct = [-1]
    def cb(evt: ProgressEvent) -> None:
        if evt.pct is None:
            return
        pct = int(evt.pct * 100)
        if pct == last_pct[0]:
            return
        last_pct[0] = pct
        log.detail(f"render {pct:3d}%  speed={evt.speed}  fps={evt.fps}")
    return cb


def _synthesize(edl: EDL, config: RenderConfig, log: Logger) -> Path:
    assert edl.voiceover is not None
    vo = edl.voiceover
    cache = TTSCache(config.cache_dir, enabled=config.use_cache)
    key = TTSCache.key(vo.backend, vo.voice_id, vo.settings, vo.script)
    cached = cache.get(key)
    if cached is not None:
        log.success(f"cache hit: {cached}")
        return cached

    backend = get_backend(vo.backend, api_key=config.api_key)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    backend.synthesize(vo.script, vo.voice_id, vo.settings, tmp_path)
    stored = cache.put(key, tmp_path)
    if stored is None:
        log.warn("cache disabled or unwritable; using temp wav")
        return tmp_path
    log.success(f"voiceover wav: {stored}")
    return stored
