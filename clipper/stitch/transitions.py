from __future__ import annotations

from dataclasses import dataclass

from clipper.core.edl import EDLVideoCue


@dataclass(frozen=True)
class JoinChain:
    chains: list[str]
    final_label: str


def needs_xfade_chain(cues: list[EDLVideoCue]) -> bool:
    return any(c.transition_in == "crossfade" and c.transition_duration > 0 for c in cues)


def build_join_chain(
    cues: list[EDLVideoCue],
    input_labels: list[str],
    cue_source_durations: list[float],
) -> JoinChain:
    """Join `input_labels` (one per cue, post-reframe + post-effect) into a
    single stream, mixing xfade where requested and concat elsewhere.

    `cue_source_durations[i]` is the visible duration of cues[i] *after*
    effects (e.g. freeze and ken_burns preserve cue duration; slow_mo also
    preserves it because the slow_mo filter expands the source back to the
    cue length).
    """
    assert len(cues) == len(input_labels) == len(cue_source_durations)
    if not cues:
        return JoinChain(chains=[], final_label="")

    chains: list[str] = []
    if len(cues) == 1:
        chains.append(f"{input_labels[0]}null[vjoined]")
        return JoinChain(chains=chains, final_label="[vjoined]")

    out_label = input_labels[0]
    running = cue_source_durations[0]
    for i in range(1, len(cues)):
        cue = cues[i]
        next_in = input_labels[i]
        join_label = f"[vj{i}]"
        if cue.transition_in == "crossfade" and cue.transition_duration > 0:
            d = cue.transition_duration
            offset = max(0.0, running - d)
            chains.append(
                f"{out_label}{next_in}"
                f"xfade=transition=fade:duration={d:.3f}:offset={offset:.3f}"
                f"{join_label}"
            )
            running = running + cue_source_durations[i] - d
        else:
            chains.append(f"{out_label}{next_in}concat=n=2:v=1:a=0{join_label}")
            running = running + cue_source_durations[i]
        out_label = join_label
    return JoinChain(chains=chains, final_label=out_label)
