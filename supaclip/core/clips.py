from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class ClipMetadata:
    """The slice of clip data the render path needs to place a cue.

    `file` must be a resolvable path ffmpeg can open — an absolute path, or one
    relative to the current working directory. Backends that store paths
    relative to a manifest are responsible for resolving them before returning.
    """

    clip_id: int
    file: str
    clip_local_id: str
    duration: float
    source_in: float


class ClipSource(Protocol):
    """Backend that resolves an EDL's integer clip_ids to playable clips.

    The EDL format references clips only by integer id, so stitch is agnostic
    to where clips come from. The default backend is the SQLite catalog; any
    object implementing this method can be passed to `render()` instead.
    """

    def get_clip(self, clip_id: int) -> ClipMetadata | None:
        ...
