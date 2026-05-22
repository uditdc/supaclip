from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from supaclip.core.edl import EDL, EDLVideoCue, ReframeMode
from supaclip.stitch.annotation import build_annotation_chain
from supaclip.stitch.captions import CaptionRender, build_caption_overlay_chain
from supaclip.stitch.effects import plan_effect
from supaclip.stitch.music import MusicPlan
from supaclip.stitch.overlay import OSTRender, build_ost_overlay_chain
from supaclip.stitch.reframe import build_reframe_filter
from supaclip.stitch.transitions import build_join_chain, needs_xfade_chain


@dataclass
class CueInput:
    """Resolved video cue: where the underlying clip lives + its dimensions."""
    file_path: str
    cue: EDLVideoCue
    cue_start: float
    cue_end: float
    source_in: float
    src_w: int
    src_h: int
    reframe: ReframeMode


@dataclass
class RenderInputs:
    edl: EDL
    cues: list[CueInput]
    voiceover_wav: str | None = None
    fontfile: str | None = None
    music_path: str | None = None
    music_plan: MusicPlan | None = None
    ost_renders: list[OSTRender] = field(default_factory=list)
    caption_renders: list[CaptionRender] = field(default_factory=list)
    video_bitrate: str = "8M"
    audio_bitrate: str = "192k"
    preset: str = "medium"
    crf: int = 20


def build_command(inputs: RenderInputs, output_path: str | Path) -> list[str]:
    edl = inputs.edl
    out_w, out_h = edl.output.width, edl.output.height
    fps = edl.output.fps
    duration = edl.output.duration

    if not inputs.cues:
        raise ValueError("at least one video cue is required to render")

    args: list[str] = ["-y", "-hide_banner", "-loglevel", "error"]

    cue_source_durations: list[float] = []
    for cue_input in inputs.cues:
        plan = plan_effect(cue_input.cue, out_w, out_h, fps)
        args += [
            "-ss", f"{cue_input.source_in:.3f}",
            "-t", f"{plan.source_consumed:.3f}",
            "-i", cue_input.file_path,
        ]
        cue_source_durations.append(cue_input.cue_end - cue_input.cue_start)

    voiceover_index: int | None = None
    if inputs.voiceover_wav:
        voiceover_index = len(inputs.cues)
        args += ["-i", str(inputs.voiceover_wav)]

    music_index: int | None = None
    if inputs.music_path:
        music_index = len(inputs.cues) + (1 if voiceover_index is not None else 0)
        args += ["-i", inputs.music_path]

    ost_input_indices: list[int] = []
    next_index = (
        len(inputs.cues)
        + (1 if voiceover_index is not None else 0)
        + (1 if music_index is not None else 0)
    )
    for render in inputs.ost_renders:
        ost_input_indices.append(next_index)
        args += ["-i", str(render.png_path)]
        next_index += 1

    caption_input_indices: list[int] = []
    for render in inputs.caption_renders:
        caption_input_indices.append(next_index)
        args += ["-i", str(render.png_path)]
        next_index += 1

    chains: list[str] = []
    video_labels: list[str] = []
    for i, cue_input in enumerate(inputs.cues):
        reframe = build_reframe_filter(cue_input.reframe, out_w, out_h, fps)
        plan = plan_effect(cue_input.cue, out_w, out_h, fps)
        chain_filters = [f"[{i}:v]", reframe]
        if plan.filter_snippet:
            chain_filters.append("," + plan.filter_snippet)
        chain_filters.append(f"[v{i}]")
        chains.append("".join(chain_filters))
        video_labels.append(f"[v{i}]")

    join = build_join_chain(
        cues=[ci.cue for ci in inputs.cues],
        input_labels=video_labels,
        cue_source_durations=cue_source_durations,
    )
    chains.extend(join.chains)
    vjoined = join.final_label

    ann_chain = build_annotation_chain(edl.annotations)
    after_ann_label = "[vann]" if ann_chain else vjoined
    if ann_chain:
        chains.append(f"{vjoined}{ann_chain}{after_ann_label}")

    captions_present = bool(inputs.caption_renders)
    ost_final_label = "[vost_out]" if captions_present else "[vout]"
    chains.extend(build_ost_overlay_chain(
        renders=inputs.ost_renders,
        input_indices=ost_input_indices,
        base_label=after_ann_label,
        final_label=ost_final_label,
    ))
    if captions_present:
        chains.extend(build_caption_overlay_chain(
            renders=inputs.caption_renders,
            input_indices=caption_input_indices,
            base_label=ost_final_label,
            final_label="[vout]",
        ))

    audio_labels: list[str] = []
    clip_audio_cues = [c for c in edl.audio if c.kind == "clip_audio"]
    voiceover_cues = [c for c in edl.audio if c.kind == "voiceover"]
    voiceover_sidechain_label: str | None = None

    if voiceover_index is not None and voiceover_cues:
        vo_cue = voiceover_cues[0]
        vo_filters: list[str] = []
        if vo_cue.level_db is not None:
            vo_filters.append(f"volume={vo_cue.level_db}dB")
        vo_filters.append(f"apad=whole_dur={duration}")
        vo_filters.append(f"atrim=duration={duration}")
        vo_filters.append("aresample=48000")
        chains.append(f"[{voiceover_index}:a]{','.join(vo_filters)}[avo_pre]")
        if inputs.music_plan is not None and edl.music is not None and edl.music.duck:
            chains.append("[avo_pre]asplit=2[avo][avo_sc]")
            voiceover_sidechain_label = "[avo_sc]"
        else:
            chains.append("[avo_pre]anull[avo]")
        audio_labels.append("[avo]")

    if inputs.music_plan is not None and music_index is not None:
        from supaclip.stitch.music import build_music_plan
        music_plan = build_music_plan(
            music=edl.music,  # type: ignore[arg-type]
            music_input_index=music_index,
            duration=duration,
            voiceover_sidechain_label=voiceover_sidechain_label,
        )
        chains.extend(music_plan.chains)
        audio_labels.append(music_plan.final_label)

    for j, ac in enumerate(clip_audio_cues):
        idx = _find_input_for_time(inputs.cues, ac.start)
        if idx is None:
            continue
        cue = inputs.cues[idx]
        seg_offset = max(0.0, ac.start - cue.cue_start)
        seg_dur = min(ac.end, cue.cue_end) - max(ac.start, cue.cue_start)
        if seg_dur <= 0:
            continue
        timeline_offset = max(ac.start, cue.cue_start)
        level = ac.level_db if ac.level_db is not None else -18.0
        chains.append(
            f"[{idx}:a]"
            f"atrim=start={seg_offset:.3f}:duration={seg_dur:.3f},"
            f"asetpts=PTS-STARTPTS,"
            f"adelay={int(timeline_offset*1000)}|{int(timeline_offset*1000)},"
            f"volume={level}dB,"
            f"aresample=48000"
            f"[abg{j}]"
        )
        audio_labels.append(f"[abg{j}]")

    if not audio_labels:
        chains.append(
            f"anullsrc=channel_layout=stereo:sample_rate=48000:duration={duration}[aout]"
        )
    elif len(audio_labels) == 1:
        chains.append(f"{audio_labels[0]}anull[aout]")
    else:
        chains.append(
            "".join(audio_labels)
            + f"amix=inputs={len(audio_labels)}:duration=longest:normalize=0[aout]"
        )

    filter_complex = ";".join(chains)

    args += [
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-map", "[aout]",
        "-c:v", "libx264", "-preset", inputs.preset, "-crf", str(inputs.crf),
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-c:a", "aac", "-b:a", inputs.audio_bitrate,
        "-movflags", "+faststart",
        "-t", f"{duration:.3f}",
        str(output_path),
    ]
    return args


def _find_input_for_time(cues: list[CueInput], t: float) -> int | None:
    for i, cue in enumerate(cues):
        if cue.cue_start <= t < cue.cue_end:
            return i
    return None
