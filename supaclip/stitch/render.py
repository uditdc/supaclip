from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from supaclip.core.clips import ClipSource
from supaclip.core.edl import EDL, load_edl, save_edl, validate_edl
from supaclip.core.ffmpeg import ensure_ffmpeg, probe
from supaclip.core.log import Logger
from supaclip.stitch.annotation import render_annotation_pngs
from supaclip.stitch.assembly import CueInput, RenderInputs, build_command
from supaclip.stitch.captions import chunk_alignment, chunks_from_cues, render_caption_pngs
from supaclip.stitch.encode import resolution_scale_factor, scale_edl, select_encoder
from supaclip.stitch.music import build_music_plan, resolve_music_file
from supaclip.stitch.overlay import render_ost_pngs, render_watermark_png
from supaclip.stitch.progress import ProgressEvent, run_ffmpeg_with_progress
from supaclip.stitch.tts import get_backend
from supaclip.stitch.tts.base import Alignment
from supaclip.stitch.tts.cache import TTSCache

DEFAULT_CACHE_DIR = "~/.cache/supaclip"

# Kinetic captions/OST fan out to many small PNG overlays (karaoke × pop × cues).
# Well within ffmpeg's reach for a Short, but warn past this so a runaway EDL is
# visible rather than silently slow.
OVERLAY_INPUT_WARN_THRESHOLD = 300


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
    encoder: str = "libx264"
    resolution: str | None = None        # 720p/1080p/1440p/4k; scales the EDL
    preset: str = "medium"
    crf: int = 20


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
    clip_source: ClipSource | None = None,
) -> RenderResult:
    log = log or Logger(verbose=config.verbose)
    ensure_ffmpeg()

    log.stage("load EDL")
    edl = load_edl(config.edl_path)
    log.info(f"title: {edl.title!r}  duration: {edl.output.duration:.1f}s  "
             f"out: {edl.output.width}x{edl.output.height}@{edl.output.fps}")

    if config.resolution is not None:
        factor = resolution_scale_factor(
            edl.output.width, edl.output.height, config.resolution
        )
        edl = scale_edl(edl, factor)
        log.info(f"resolution {config.resolution}: scaled "
                 f"{factor:.3f}x -> {edl.output.width}x{edl.output.height}")

    encoder = select_encoder(config.encoder)
    if encoder != config.encoder:
        log.info(f"encoder: {config.encoder} -> {encoder}")
    else:
        log.info(f"encoder: {encoder}")

    if config.preview_cue is not None:
        if not (0 <= config.preview_cue < len(edl.video)):
            raise RenderError(
                f"--preview-cue {config.preview_cue} out of range "
                f"(0..{len(edl.video)-1})"
            )
        edl = _trim_to_single_cue(edl, config.preview_cue)
        log.info(f"preview mode: rendering only cue #{config.preview_cue}")

    if clip_source is None:
        log.stage("connect catalog")
        from supaclip.catalog.paths import resolve_catalog_path
        from supaclip.catalog.source import SqliteClipSource
        clip_source = SqliteClipSource.open(config.catalog_path)
        log.info(f"catalog: {resolve_catalog_path(config.catalog_path)}")

    log.stage("validate EDL")
    issues = validate_edl(edl, resolver=clip_source.get_clip)
    errors = [i for i in issues if i.severity == "error"]
    for i in issues:
        (log.error if i.severity == "error" else log.warn)(f"{i.path}: {i.message}")
    if errors:
        raise RenderError(f"EDL has {len(errors)} validation error(s); refusing to render")

    log.stage("resolve clips")
    cue_inputs = _resolve_cue_inputs(clip_source, edl, log)

    voiceover_wav: Path | None = None
    alignment: Alignment | None = None
    if edl.voiceover is not None:
        log.stage("synthesize voiceover")
        voiceover_wav, alignment = _synthesize(
            edl, config, log, want_alignment=edl.captions is not None,
        )

    music_path: str | None = None
    music_plan = None
    if edl.music is not None:
        log.stage("resolve music")
        music_path = resolve_music_file(edl.music.file, clip_source)
        log.info(f"music: {music_path}")
        music_plan = build_music_plan(
            music=edl.music,
            music_input_index=0,
            duration=edl.output.duration,
            voiceover_sidechain_label=None,
        )

    ost_renders = []
    if edl.ost:
        log.stage("render OST captions")
        ost_cache = Path(config.cache_dir).expanduser() / "ost"
        ost_renders = render_ost_pngs(
            cues=edl.ost,
            out_w=edl.output.width,
            out_h=edl.output.height,
            cache_dir=ost_cache,
            fontfile=config.fontfile,
            fps=edl.output.fps,
        )
        log.info(f"rendered {len(ost_renders)} OST png(s) -> {ost_cache}")

    watermark_render = None
    if edl.output.watermark is not None:
        log.stage("render watermark")
        watermark_cache = Path(config.cache_dir).expanduser() / "watermark"
        watermark_render = render_watermark_png(
            edl.output.watermark,
            out_w=edl.output.width,
            out_h=edl.output.height,
            cache_dir=watermark_cache,
            fontfile=config.fontfile,
        )
        log.info(f"rendered watermark png -> {watermark_render.png_path}")

    annotation_renders = []
    if any(a.shape == "circle" for a in edl.annotations):
        log.stage("render circle annotations")
        ann_cache = Path(config.cache_dir).expanduser() / "annotations"
        annotation_renders = render_annotation_pngs(edl.annotations, ann_cache)
        log.info(f"rendered {len(annotation_renders)} circle png(s) -> {ann_cache}")

    caption_renders = []
    if edl.captions is not None and (alignment is not None or edl.captions.cues):
        voiceover_offset = 0.0
        if edl.captions.cues:
            log.stage("render source captions")
            chunks = chunks_from_cues(edl.captions.cues)
        else:
            log.stage("render speech captions")
            voiceover_cues = [c for c in edl.audio if c.kind == "voiceover"]
            voiceover_offset = voiceover_cues[0].start if voiceover_cues else 0.0
            chunks = chunk_alignment(
                alignment,
                max_words=edl.captions.max_words,
                max_chars=edl.captions.max_chars,
                min_chunk_duration=edl.captions.min_chunk_duration,
            )
        caption_cache = Path(config.cache_dir).expanduser() / "captions"
        caption_renders = render_caption_pngs(
            chunks=chunks,
            config=edl.captions,
            out_w=edl.output.width,
            out_h=edl.output.height,
            cache_dir=caption_cache,
            voiceover_offset=voiceover_offset,
            fontfile=config.fontfile,
            fps=edl.output.fps,
        )
        log.info(
            f"rendered {len(caption_renders)} caption png(s) "
            f"from {len(chunks)} phrase(s) -> {caption_cache}"
        )

    overlay_inputs = (
        len(ost_renders) + len(caption_renders) + len(annotation_renders)
        + (1 if watermark_render is not None else 0)
    )
    if overlay_inputs > OVERLAY_INPUT_WARN_THRESHOLD:
        log.warn(
            f"{overlay_inputs} overlay PNG inputs (captions/OST/annotations); "
            f"animation multiplies inputs — keep the PNG cache warm and watch "
            f"ffmpeg's open-file limit"
        )

    log.stage("render")
    inputs = RenderInputs(
        edl=edl, cues=cue_inputs,
        voiceover_wav=str(voiceover_wav) if voiceover_wav else None,
        fontfile=config.fontfile,
        music_path=music_path,
        music_plan=music_plan,
        ost_renders=ost_renders,
        caption_renders=caption_renders,
        annotation_renders=annotation_renders,
        watermark_render=watermark_render,
        encoder=encoder,
        preset=config.preset,
        crf=config.crf,
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


def _resolve_cue_inputs(source: ClipSource, edl: EDL, log: Logger) -> list[CueInput]:
    cues: list[CueInput] = []
    probe_cache: dict[str, Any] = {}
    for i, cue in enumerate(edl.video):
        clip = source.get_clip(cue.clip_id)
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
        "captions": None,
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


def synthesize_voiceover(
    *,
    backend: str,
    voice_id: str,
    settings: dict[str, float],
    script: str,
    api_key: str | None = None,
    cache_dir: str = DEFAULT_CACHE_DIR,
    use_cache: bool = True,
    want_alignment: bool = False,
    log: Logger | None = None,
) -> tuple[Path, Alignment | None]:
    """Synthesize a voiceover wav, reusing the on-disk TTS cache.

    Shared by the render pipeline and callers that need the voiceover (and its
    alignment) ahead of render, so a single synthesis serves both.
    """
    log = log or Logger(verbose=False)
    cache = TTSCache(cache_dir, enabled=use_cache)
    key = TTSCache.key(backend, voice_id, settings, script)

    cached = cache.get(key)
    cached_align: Alignment | None = None
    if cached is not None and want_alignment:
        raw = cache.get_alignment(key)
        if raw is not None:
            cached_align = Alignment.from_dict(raw)

    if cached is not None and (not want_alignment or cached_align is not None):
        log.success(f"cache hit: {cached}")
        return cached, cached_align

    be = get_backend(backend, api_key=api_key)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    alignment: Alignment | None = None
    if want_alignment:
        _, alignment = be.synthesize_with_alignment(script, voice_id, settings, tmp_path)
    else:
        be.synthesize(script, voice_id, settings, tmp_path)

    stored = cache.put(key, tmp_path)
    if stored is None:
        log.warn("cache disabled or unwritable; using temp wav")
        return tmp_path, alignment
    if alignment is not None:
        cache.put_alignment(key, alignment.to_dict())
    log.success(f"voiceover wav: {stored}")
    return stored, alignment


def _synthesize(
    edl: EDL,
    config: RenderConfig,
    log: Logger,
    want_alignment: bool = False,
) -> tuple[Path, Alignment | None]:
    assert edl.voiceover is not None
    vo = edl.voiceover
    return synthesize_voiceover(
        backend=vo.backend,
        voice_id=vo.voice_id,
        settings=vo.settings,
        script=vo.script,
        api_key=config.api_key,
        cache_dir=config.cache_dir,
        use_cache=config.use_cache,
        want_alignment=want_alignment,
        log=log,
    )
