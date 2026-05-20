from __future__ import annotations

from clipper.core.edl import EDLAnnotation


def _hex_to_ffcolor(color: str) -> str:
    """Normalize a hex color to ffmpeg drawbox-friendly form (keeps `#`)."""
    return color if color.startswith("#") or color in _NAMED else f"#{color}"


_NAMED = {"red", "green", "blue", "yellow", "white", "black", "pink", "orange", "cyan"}


def build_annotation(ann: EDLAnnotation) -> str:
    """Return an ffmpeg filter chain string drawing one annotation.

    NOTE for 2.5: shapes are drawn with the native `drawbox` filter, so:
    - `box` is a rectangle outline (true to shape),
    - `circle` is rendered as its square bounding box (placeholder; a
      proper PIL-rendered circle overlay is on the 3.x list),
    - `arrow` is a horizontal bar of length `width` (no arrow head yet).
    All use `enable='between(t,...)'` so they appear only in window.
    """
    color = _hex_to_ffcolor(ann.color)
    t = max(1, ann.stroke_width)
    enable = f"enable='between(t,{ann.start:.3f},{ann.end:.3f})'"

    if ann.shape == "circle":
        r = ann.radius
        x0 = ann.x - r
        y0 = ann.y - r
        side = r * 2
        return (
            f"drawbox=x={x0}:y={y0}:w={side}:h={side}:"
            f"color={color}:t={t}:{enable}"
        )

    if ann.shape == "box":
        x0 = ann.x - ann.width // 2
        y0 = ann.y - ann.height // 2
        return (
            f"drawbox=x={x0}:y={y0}:w={ann.width}:h={ann.height}:"
            f"color={color}:t={t}:{enable}"
        )

    if ann.shape == "arrow":
        x0 = ann.x
        y0 = ann.y - t // 2
        return (
            f"drawbox=x={x0}:y={y0}:w={ann.width}:h={t}:"
            f"color={color}:t=fill:{enable}"
        )

    raise ValueError(f"unknown annotation shape: {ann.shape!r}")


def build_annotation_chain(annotations: list[EDLAnnotation]) -> str:
    """Comma-joined chain of drawbox filters for all annotations. Empty when none."""
    return ",".join(build_annotation(a) for a in annotations)
