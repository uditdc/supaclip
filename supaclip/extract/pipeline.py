from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..core.cache import Cache, fingerprint_file
from ..core.ffmpeg import (
    VideoInfo,
    cut_clip,
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
    now_iso,
    save_manifest,
)
from .analyze import PROMPT_VERSION, AnalyzerBackend, blend_score, build_backend
from .audio import audio_factor_for_range, detect_peaks, peak_loudness_db
from .dedupe import merge_overlapping
from .profiles import GameProfile, load_profile
from .segment import (
    auto_segments_from_peaks,
    clamp_ranges,
    file_segments,
    interval_segments,
    manual_segments,
    scene_segments,
)


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
        if cached is not None:
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

    log.stage(f"Segment ({cfg.segmenter})")
    seg_key = (fingerprint, cfg.segmenter, cfg.interval, cfg.min_clip, cfg.max_clip)
    cached_ranges = cache.get("segments", seg_key)
    if cached_ranges is not None:
        ranges = [(float(a), float(b)) for a, b in cached_ranges]
        log.detail(f"segment cache hit ({len(ranges)} ranges)")
    else:
        ranges = _segment(video_path, cfg, info, peaks)
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
    analyses: list[list[dict]] = []
    for i, (start, end) in enumerate(ranges, 1):
        ana_key = (
            fingerprint, round(start, 3), round(end, 3),
            cfg.analyzer, cfg.llm, profile.name, PROMPT_VERSION,
        )
        cached_ana = cache.get("analysis", ana_key)
        if cached_ana is not None:
            log.detail(f"[{i}/{len(ranges)}] cache hit {start:.1f}-{end:.1f}s "
                       f"({len(cached_ana)} events)")
            analyses.append(cached_ana)
            continue
        log.info(f"[{i}/{len(ranges)}] analyzing {start:.1f}-{end:.1f}s ({end - start:.1f}s)")
        analysis = backend.analyze_segment(video_path, start, end, profile)
        payload = [
            {
                "start": ev.start,
                "end": ev.end,
                "description": ev.description,
                "categories": ev.categories,
                "base_interest": ev.base_interest,
                "game_signals": ev.game_signals,
                "audio_cues": ev.audio_cues,
            }
            for ev in analysis.events
        ]
        cache.set("analysis", ana_key, payload)
        analyses.append(payload)
    total_events = sum(len(a) for a in analyses)
    log.success(f"analyzed {len(analyses)} segments → {total_events} events")

    log.stage("Catalog")
    output_dir.mkdir(parents=True, exist_ok=True)
    clips: list[Clip] = []
    clip_idx = 0
    for seg_idx, ((seg_start, seg_end), events) in enumerate(zip(ranges, analyses), 1):
        cut_path: Path | None = None
        if cfg.segmenter != "file":
            cut_id = f"seg_{seg_idx:02d}"
            cut_path = output_dir / f"{cut_id}.mp4"
            cut_clip(video_path, seg_start, seg_end, cut_path)

        for ev in events:
            clip_idx += 1
            clip_id = f"clip_{clip_idx:02d}"

            ev_start_offset = float(ev.get("start", 0.0))
            ev_end_offset = float(ev.get("end", seg_end - seg_start))
            ev_start_orig = seg_start + ev_start_offset
            ev_end_orig = seg_start + ev_end_offset
            ev_dur = max(0.0, ev_end_orig - ev_start_orig)

            if cut_path is not None:
                clip_file_path = cut_path
                source_in = ev_start_offset
                source_out = ev_end_offset
            else:
                clip_file_path = Path(video_path)
                source_in = ev_start_orig
                source_out = ev_end_orig

            kf_pattern = str(output_dir / f"{clip_id}.kf{{i:02d}}.jpg")
            kf_paths = extract_keyframes(
                video_path, ev_start_orig, ev_end_orig, cfg.keyframes, kf_pattern,
            )

            audio_factor = audio_factor_for_range(samples, ev_start_orig, ev_end_orig)
            score = blend_score(int(ev.get("base_interest", 0)), audio_factor)
            peak_db = peak_loudness_db(samples, ev_start_orig, ev_end_orig)

            if cut_path is not None and _is_inside(clip_file_path, output_dir.parent):
                file_str = str(clip_file_path.relative_to(output_dir.parent))
            else:
                file_str = str(clip_file_path)

            clips.append(Clip(
                id=clip_id,
                file=file_str,
                source_in=round(source_in, 3),
                source_out=round(source_out, 3),
                duration=round(ev_dur, 3),
                resolution=info.resolution,
                fps=info.fps,
                description=str(ev.get("description") or ""),
                categories=list(ev.get("categories") or []),
                score=score,
                game_signals=dict(ev.get("game_signals") or {}),
                audio=AudioInfo(peak_loudness_db=peak_db, cues=list(ev.get("audio_cues") or [])),
                keyframes=[str(Path(k)) for k in kf_paths],
                segment_source=cfg.segmenter,
            ))

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
    )
    manifest_path = output_dir / "manifest.json"
    save_manifest(manifest, manifest_path)
    log.success(f"wrote {len(clips)} clips and {manifest_path}")
    return manifest


def _resolve_output_dir(video_path: str, cfg: ExtractConfig) -> Path:
    out = Path(cfg.output_dir)
    if len(cfg.videos) > 1:
        return out / Path(video_path).stem
    return out


def _segment(
    video_path: str,
    cfg: ExtractConfig,
    info: VideoInfo,
    peaks,
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
            info.duration, peaks, cfg.min_clip, cfg.max_clip,
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
