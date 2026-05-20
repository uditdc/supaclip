from __future__ import annotations

from dataclasses import dataclass

from clipper.core.edl import EDLVideoCue


@dataclass(frozen=True)
class EffectPlan:
    """Resolved effect for a single cue.

    `source_consumed` is how much source footage the input `-t` flag should
    request. `filter_snippet` is a comma-separated ffmpeg filter chain to be
    spliced in *after* the reframe filter (so the effect operates on the
    already-9:16 stream). Empty snippet means no effect.
    """

    source_consumed: float
    filter_snippet: str


def plan_effect(cue: EDLVideoCue, dst_w: int, dst_h: int, fps: int) -> EffectPlan:
    cue_dur = cue.end - cue.start

    if cue.effect == "none":
        return EffectPlan(source_consumed=cue_dur, filter_snippet="")

    if cue.effect == "freeze_first":
        return EffectPlan(
            source_consumed=max(2.0 / fps, 0.05),
            filter_snippet=(
                f"trim=end_frame=1,setpts=PTS-STARTPTS,"
                f"loop=loop=-1:size=1:start=0,"
                f"fps={fps},"
                f"trim=duration={cue_dur:.3f},"
                f"setpts=PTS-STARTPTS"
            ),
        )

    if cue.effect == "slow_mo":
        speed = float(cue.effect_params.get("speed", 0.5))
        speed = max(0.05, min(1.0, speed))
        return EffectPlan(
            source_consumed=max(cue_dur * speed, 1.0 / fps),
            filter_snippet=f"setpts=PTS/{speed}",
        )

    if cue.effect in ("ken_burns_in", "ken_burns_out"):
        if cue.effect == "ken_burns_in":
            zf = float(cue.effect_params.get("zoom_from", 1.0))
            zt = float(cue.effect_params.get("zoom_to", 1.15))
        else:
            zf = float(cue.effect_params.get("zoom_from", 1.15))
            zt = float(cue.effect_params.get("zoom_to", 1.0))
        total_frames = max(int(round(cue_dur * fps)), 2)
        z_expr = (
            f"{zf:.4f}+({zt:.4f}-{zf:.4f})*on/{total_frames - 1}"
        )
        return EffectPlan(
            source_consumed=cue_dur,
            filter_snippet=(
                f"zoompan=z='{z_expr}':d=1:"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                f"s={dst_w}x{dst_h}:fps={fps}"
            ),
        )

    return EffectPlan(source_consumed=cue_dur, filter_snippet="")
