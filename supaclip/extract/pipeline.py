from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from ..core.cache import Cache, fingerprint_file
from ..core.ffmpeg import (
    VideoInfo,
    ensure_ffmpeg,
    extract_keyframes,
    extract_loudness_curve,
    probe,
)
from ..core.log import Logger
from ..core.manifest import (
    AudioInfo,
    Clip,
    ExtractInfo,
    Manifest,
    SourceInfo,
    SourceSummary,
    now_iso,
    save_manifest,
)
from .aggregate import (
    PROMPT_VERSION as AGG_PROMPT_VERSION,
)
from .aggregate import (
    AggregateConfig,
    aggregate_events,
)
from .analyze import PROMPT_VERSION, AnalyzerBackend, blend_score, build_backend
from .audio import audio_factor_for_range, detect_peaks, peak_loudness_db
from .backends._shared import _context_fingerprint
from .chunking import chunk_segment
from .dedupe import merge_overlapping
from .profiles import GameProfile, VideoContext, load_profile
from .segment import (
    auto_segments_from_peaks,
    clamp_ranges,
    file_segments,
    interval_segments,
    manual_segments,
    scene_segments,
)
from .subtitles import SubtitleCue, dialogue_for_range, load_for_video
from .summarize import PROMPT_VERSION as SUM_PROMPT_VERSION
from .summarize import summarize_source


@dataclass
class ExtractConfig:
    videos: list[str]
    output_dir: str
    segmenter: str
    timestamps_file: str | None
    interval: float
    game_profile: str
    analyzer: str
    llm: str
    base_url: str
    api_key: str | None
    keyframes: int
    dedup_iou: float
    no_dedup: bool
    min_clip: float
    max_clip: float
    max_duration: float
    cache_dir: str
    no_cache: bool
    keep_temp: bool
    verbose: bool
    no_chunk: bool = False
    analyze_concurrency: int = 4
    context: VideoContext | None = None
    subtitles: str | None = None
    no_subtitles: bool = False
    no_summary: bool = False


def run(cfg: ExtractConfig, log: Logger) -> list[Manifest]:
    ensure_ffmpeg()
    profile = load_profile(cfg.game_profile)
    backend = build_backend(cfg.analyzer, cfg.llm, cfg.base_url, cfg.api_key)
    cache = Cache(cfg.cache_dir, enabled=not cfg.no_cache)

    manifests: list[Manifest] = []
    for video in cfg.videos:
        manifests.append(_run_one(video, cfg, profile, backend, cache, log))
    return manifests


def _run_one(
    video_path: str,
    cfg: ExtractConfig,
    profile: GameProfile,
    backend: AnalyzerBackend,
    cache: Cache,
    log: Logger,
) -> Manifest:
    log.stage(f"Ingest: {video_path}")
    info = probe(video_path)
    if info.duration > cfg.max_duration:
        raise SystemExit(
            f"Input is {info.duration:.1f}s long, exceeds --max-duration "
            f"{cfg.max_duration:.0f}s. Pass --max-duration to override."
        )
    log.success(f"{info.resolution} @ {info.fps:.2f} fps, {info.duration:.1f}s, "
                f"{'audio' if info.has_audio else 'no audio'}")

    fingerprint = fingerprint_file(video_path)
    output_dir = _resolve_output_dir(video_path, cfg)

    log.stage("Audio energy")
    samples: list[tuple[float, float]] = []
    if info.has_audio:
        cached = cache.get("audio", (fingerprint,))
        if cached:
            samples = [(float(t), float(db)) for t, db in cached]
            log.detail(f"audio cache hit ({len(samples)} samples)")
        else:
            samples = extract_loudness_curve(video_path)
            cache.set("audio", (fingerprint,), samples)
        peaks = detect_peaks(samples)
        log.success(f"{len(peaks)} peaks from {len(samples)} samples")
    else:
        log.warn("source has no audio stream; skipping pre-pass")
        peaks = []

    sub_cues: list[SubtitleCue] = []
    if not cfg.no_subtitles:
        log.stage("Subtitles")
        sub_cues, sub_source = load_for_video(video_path, cfg.subtitles)
        if sub_cues:
            log.success(f"{len(sub_cues)} dialogue cues from {sub_source}")
        else:
            log.detail("no subtitles found; descriptions will be vision-only")

    log.stage(f"Segment ({cfg.segmenter})")
    seg_key = (fingerprint, cfg.segmenter, cfg.interval, cfg.min_clip, cfg.max_clip, "v2-trough")
    cached_ranges = cache.get("segments", seg_key)
    if cached_ranges is not None:
        ranges = [(float(a), float(b)) for a, b in cached_ranges]
        log.detail(f"segment cache hit ({len(ranges)} ranges)")
    else:
        ranges = _segment(video_path, cfg, info, samples)
        cache.set("segments", seg_key, ranges)
    if cfg.segmenter != "file":
        ranges = clamp_ranges(ranges, cfg.min_clip, cfg.max_clip, info.duration)
    log.success(f"{len(ranges)} candidate segments")

    if cfg.segmenter != "file" and not cfg.no_dedup and ranges:
        log.stage("Dedupe")
        before = len(ranges)
        ranges = merge_overlapping(ranges, cfg.dedup_iou)
        log.success(f"{before} → {len(ranges)} after temporal merge (IoU ≥ {cfg.dedup_iou})")

    log.stage(f"Analyze ({cfg.analyzer}/{cfg.llm})")
    ctx_fp = _context_fingerprint(cfg.context)
    tasks = _plan_chunks(ranges, samples, cfg, profile, fingerprint, ctx_fp, log)

    payloads: list[list[dict] | None] = [None] * len(tasks)
    pending: list[_ChunkTask] = []
    for task in tasks:
        cached_chunk = cache.get("analysis", task.key)
        if cached_chunk is not None:
            payloads[task.index] = cached_chunk
            log.detail(f"  chunk {task.cs:.1f}-{task.ce:.1f}s cache hit "
                       f"({len(cached_chunk)} events)")
        else:
            pending.append(task)

    if pending:
        def _analyze(task: _ChunkTask) -> list[dict]:
            analysis = backend.analyze_segment(
                video_path, task.cs, task.ce, profile, context=cfg.context,
            )
            return [
                {
                    "start": ev.start,
                    "end": ev.end,
                    "description": ev.description,
                    "categories": ev.categories,
                    "base_interest": ev.base_interest,
                    "game_signals": ev.game_signals,
                }
                for ev in analysis.events
            ]

        workers = max(1, min(cfg.analyze_concurrency, len(pending)))
        log.detail(f"analyzing {len(pending)} chunks (concurrency {workers})")
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_analyze, task): task for task in pending}
            for future in as_completed(futures):
                task = futures[future]
                done += 1
                try:
                    payload = future.result()
                except Exception as e:  # noqa: BLE001
                    log.warn(f"  [{done}/{len(pending)}] {task.cs:.1f}-{task.ce:.1f}s "
                             f"failed ({type(e).__name__}); skipping, will retry on re-run")
                    payloads[task.index] = []
                    continue
                cache.set("analysis", task.key, payload)
                payloads[task.index] = payload
                log.detail(f"  [{done}/{len(pending)}] {task.cs:.1f}-{task.ce:.1f}s "
                           f"→ {len(payload)} events")

    flat_events: list[dict] = []
    for task in tasks:
        for ev in payloads[task.index] or []:
            flat_events.append({
                **ev,
                "start": float(ev.get("start", 0.0)) + task.cs,
                "end": float(ev.get("end", 0.0)) + task.cs,
            })

    log.success(f"analyzed {len(ranges)} segments → {len(flat_events)} raw events")

    if len(flat_events) >= 2:
        log.stage("Aggregate (final pass)")
        agg_cfg = _build_agg_config(cfg)
        agg_signature = tuple(
            (round(float(e.get("start", 0.0)), 1), round(float(e.get("end", 0.0)), 1))
            for e in flat_events
        )
        agg_key = (
            fingerprint, cfg.analyzer, cfg.llm, profile.name, AGG_PROMPT_VERSION,
            len(flat_events), agg_signature, ctx_fp,
        )
        cached_agg = cache.get("aggregate", agg_key)
        if cached_agg is not None:
            log.detail(f"aggregator cache hit ({len(cached_agg)} events)")
            final_events = cached_agg
        else:
            log.detail(f"aggregating {len(flat_events)} raw events")
            final_events = aggregate_events(
                flat_events,
                source_duration=info.duration,
                profile=profile,
                cfg=agg_cfg,
            )
            cache.set("aggregate", agg_key, final_events)
        log.success(f"{len(flat_events)} → {len(final_events)} after merge/dedupe")
    else:
        final_events = flat_events

    log.stage("Catalog")
    output_dir.mkdir(parents=True, exist_ok=True)
    source_path = Path(video_path).resolve()
    if _is_inside(source_path, output_dir.resolve()):
        file_str = str(source_path.relative_to(output_dir.resolve()))
    else:
        file_str = str(source_path)
    clips: list[Clip] = []
    for clip_idx, ev in enumerate(final_events, 1):
        clip_id = f"clip_{clip_idx:02d}"

        ev_start_orig = max(0.0, min(float(ev.get("start", 0.0)), info.duration))
        ev_end_orig = max(0.0, min(float(ev.get("end", info.duration)), info.duration))
        if ev_end_orig <= ev_start_orig:
            continue
        ev_dur = ev_end_orig - ev_start_orig

        kf_pattern = str(output_dir / f"{clip_id}.kf{{i:02d}}.jpg")
        kf_paths = extract_keyframes(
            video_path, ev_start_orig, ev_end_orig, cfg.keyframes, kf_pattern,
        )

        audio_factor = audio_factor_for_range(samples, ev_start_orig, ev_end_orig)
        score = blend_score(int(ev.get("base_interest", 0)), audio_factor)
        peak_db = peak_loudness_db(samples, ev_start_orig, ev_end_orig)

        clips.append(Clip(
            id=clip_id,
            file=file_str,
            source_in=round(ev_start_orig, 3),
            source_out=round(ev_end_orig, 3),
            duration=round(ev_dur, 3),
            resolution=info.resolution,
            fps=info.fps,
            description=str(ev.get("description") or ""),
            dialogue=dialogue_for_range(sub_cues, ev_start_orig, ev_end_orig),
            categories=list(ev.get("categories") or []),
            score=score,
            game_signals=dict(ev.get("game_signals") or {}),
            audio=AudioInfo(peak_loudness_db=peak_db, cues=[]),
            keyframes=[
                str(Path(k).relative_to(output_dir)) if _is_inside(Path(k), output_dir)
                else str(Path(k))
                for k in kf_paths
            ],
            segment_source=cfg.segmenter,
        ))

    summary = _summarize(clips, cfg, profile, info, fingerprint, ctx_fp, cache, log)

    manifest = Manifest(
        source=SourceInfo(
            file=info.path,
            duration=round(info.duration, 3),
            resolution=info.resolution,
            fps=info.fps,
        ),
        extract=ExtractInfo(
            segmenter=cfg.segmenter,
            analyzer=cfg.llm,
            game_profile=profile.name,
            created_at=now_iso(),
        ),
        taxonomy=list(profile.taxonomy),
        clips=clips,
        summary=summary,
    )
    manifest_path = output_dir / "manifest.json"
    save_manifest(manifest, manifest_path)
    log.success(f"wrote {len(clips)} clips and {manifest_path}")
    return manifest


@dataclass
class _ChunkTask:
    index: int
    cs: float
    ce: float
    key: tuple


def _plan_chunks(
    ranges: list[tuple[float, float]],
    samples: list[tuple[float, float]],
    cfg: ExtractConfig,
    profile: GameProfile,
    fingerprint: str,
    ctx_fp: str,
    log: Logger,
) -> list[_ChunkTask]:
    """Expand candidate ranges into the flat, ordered list of analysis chunks.

    Order is preserved (segment, then chunk-within-segment) so reassembled
    events stay deterministic regardless of completion order under concurrency.
    """
    tasks: list[_ChunkTask] = []
    for seg_no, (start, end) in enumerate(ranges, 1):
        chunks = [(start, end)] if cfg.no_chunk else chunk_segment(start, end, samples)
        if len(chunks) > 1:
            log.info(f"[{seg_no}/{len(ranges)}] {start:.1f}-{end:.1f}s "
                     f"({end - start:.1f}s) → {len(chunks)} chunks")
        else:
            log.info(f"[{seg_no}/{len(ranges)}] {start:.1f}-{end:.1f}s ({end - start:.1f}s)")
        for cs, ce in chunks:
            key = (
                fingerprint, round(cs, 3), round(ce, 3),
                cfg.analyzer, cfg.llm, profile.name, PROMPT_VERSION, ctx_fp,
            )
            tasks.append(_ChunkTask(index=len(tasks), cs=cs, ce=ce, key=key))
    return tasks


def _resolve_output_dir(video_path: str, cfg: ExtractConfig) -> Path:
    out = Path(cfg.output_dir)
    if len(cfg.videos) > 1:
        return out / Path(video_path).stem
    return out


def _segment(
    video_path: str,
    cfg: ExtractConfig,
    info: VideoInfo,
    samples: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    if cfg.segmenter == "manual":
        if not cfg.timestamps_file:
            raise SystemExit("--segmenter manual requires --timestamps")
        return manual_segments(cfg.timestamps_file)
    if cfg.segmenter == "interval":
        return interval_segments(info.duration, cfg.interval)
    if cfg.segmenter == "scene":
        return scene_segments(video_path, cfg.min_clip, cfg.max_clip)
    if cfg.segmenter == "auto":
        return auto_segments_from_peaks(
            info.duration, samples, cfg.min_clip, cfg.max_clip,
        )
    if cfg.segmenter == "file":
        return file_segments(info.duration)
    raise SystemExit(f"unknown --segmenter: {cfg.segmenter}")


def _is_inside(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _summarize(
    clips: list[Clip],
    cfg: ExtractConfig,
    profile: GameProfile,
    info: VideoInfo,
    fingerprint: str,
    ctx_fp: str,
    cache: Cache,
    log: Logger,
) -> SourceSummary | None:
    if cfg.no_summary or len(clips) < 2:
        return None

    log.stage("Summarize")
    events = [
        {
            "start": c.source_in,
            "end": c.source_out,
            "description": c.description,
            "dialogue": c.dialogue,
        }
        for c in clips
    ]
    signature = tuple((round(c.source_in, 1), round(c.source_out, 1)) for c in clips)
    key = (
        fingerprint, cfg.analyzer, cfg.llm, profile.name,
        SUM_PROMPT_VERSION, ctx_fp, len(clips), signature,
    )
    cached = cache.get("summary", key)
    if cached is not None:
        log.detail("summary cache hit")
        return SourceSummary.model_validate(cached)

    summary = summarize_source(
        events,
        source_duration=info.duration,
        profile=profile,
        cfg=_build_agg_config(cfg),
        context=cfg.context,
    )
    if summary is None:
        log.warn("summary pass produced nothing; manifest will have no summary")
        return None
    cache.set("summary", key, summary.model_dump(mode="json"))
    log.success(f"synopsis + {len(summary.beats)} beats, {len(summary.characters)} characters")
    return summary


def _build_agg_config(cfg: ExtractConfig) -> AggregateConfig:
    """Pick the right transport for the aggregator based on the analyzer.

    frames → OpenAI-compatible (same endpoint as the vision call).
    video → Google AI Studio (same key the analyzer is already using).
    """
    if cfg.analyzer == "video":
        import os
        key = (
            os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
            or os.environ.get("GOOGLE_AI_STUDIO_API_KEY")
            or cfg.api_key
        )
        return AggregateConfig(
            model=cfg.llm,
            base_url=cfg.base_url,
            api_key=key,
            provider="google",
        )
    return AggregateConfig(
        model=cfg.llm,
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        provider="openai",
    )
