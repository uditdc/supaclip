from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from clipper.catalog.search import get_clip
from clipper.core.edl import EDLMusic


@dataclass
class MusicPlan:
    """Resolved music bed for the render.

    `input_path` is what gets added as a `-i` to ffmpeg. `chains` is the list
    of filter_complex chain strings; `final_label` is the audio label to mix
    with voiceover/clip_audio at the end.
    """

    input_path: str
    chains: list[str]
    final_label: str


CATALOG_PREFIX = "catalog:"


def resolve_music_file(file_str: str, conn: sqlite3.Connection | None) -> str:
    """Accept either a filesystem path or 'catalog:<clip_id>'."""
    if file_str.startswith(CATALOG_PREFIX):
        if conn is None:
            raise FileNotFoundError(
                f"music references catalog clip but no catalog connection provided: {file_str}"
            )
        try:
            clip_id = int(file_str[len(CATALOG_PREFIX):])
        except ValueError as e:
            raise FileNotFoundError(f"invalid catalog ref: {file_str!r}") from e
        clip = get_clip(conn, clip_id)
        if clip is None:
            raise FileNotFoundError(f"music: clip_id={clip_id} not found")
        return clip.file

    p = Path(file_str).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"music file not found: {p}")
    return str(p.resolve())


def build_music_plan(
    music: EDLMusic,
    music_input_index: int,
    duration: float,
    voiceover_sidechain_label: str | None,
) -> MusicPlan:
    """Build the music-bed filter chain.

    `voiceover_sidechain_label` is the label of a *copy* of the voiceover
    suitable for sidechain compression. If None or music.duck is False, no
    ducking is applied.
    """
    base = f"[{music_input_index}:a]"
    parts = [
        f"volume={music.level_db}dB",
        f"apad=whole_dur={duration}",
        f"atrim=duration={duration}",
        "aresample=48000",
    ]
    pre_label = f"[amus_pre_{music_input_index}]"
    chains = [f"{base}{','.join(parts)}{pre_label}"]

    if music.duck and voiceover_sidechain_label is not None:
        out_label = f"[amus_{music_input_index}]"
        chains.append(
            f"{pre_label}{voiceover_sidechain_label}"
            f"sidechaincompress=threshold=0.05:ratio=8:attack=80:release=400"
            f"{out_label}"
        )
        return MusicPlan(input_path="", chains=chains, final_label=out_label)

    return MusicPlan(input_path="", chains=chains, final_label=pre_label)
