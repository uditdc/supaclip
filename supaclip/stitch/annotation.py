from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

from supaclip.core.edl import EDLAnnotation


def _hex_to_ffcolor(color: str) -> str:
    """Normalize a hex color to ffmpeg drawbox-friendly form (keeps `#`)."""
    return color if color.startswith("#") or color in _NAMED else f"#{color}"


_NAMED = {"red", "green", "blue", "yellow", "white", "black", "pink", "orange", "cyan"}


def build_annotation(ann: EDLAnnotation) -> str:
    """Return an ffmpeg `drawbox` filter chain string for one annotation.

    Handles the rectangular shapes only:
    - `box` is a rectangle outline (true to shape),
    - `arrow` is a horizontal bar of length `width` (no arrow head yet).
    `circle` is rendered separately as a PIL ring overlay (see
    `render_annotation_pngs`) and must be filtered out before calling this.
    """
    color = _hex_to_ffcolor(ann.color)
    t = max(1, ann.stroke_width)
    enable = f"enable='between(t,{ann.start:.3f},{ann.end:.3f})'"

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

    raise ValueError(
        f"build_annotation does not handle shape {ann.shape!r}; "
        "circles are rendered via render_annotation_pngs"
    )


def build_annotation_chain(annotations: list[EDLAnnotation]) -> str:
    """Comma-joined chain of drawbox filters for box/arrow annotations.

    Circle annotations are skipped here; they are composited as PNG overlays.
    Empty string when no drawbox-shaped annotations are present.
    """
    drawn = [a for a in annotations if a.shape != "circle"]
    return ",".join(build_annotation(a) for a in drawn)


@dataclass(frozen=True)
class AnnotationRender:
    """A rendered circle annotation PNG ready to be overlaid by ffmpeg."""
    png_path: Path
    x: int
    y: int
    start: float
    end: float


def _hex_to_rgba(color: str) -> tuple[int, int, int, int]:
    c = color.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if len(c) == 6:
        r, g, b = (int(c[i:i + 2], 16) for i in (0, 2, 4))
        return r, g, b, 255
    if len(c) == 8:
        r, g, b, a = (int(c[i:i + 2], 16) for i in (0, 2, 4, 6))
        return r, g, b, a
    raise ValueError(f"unsupported annotation color: {color!r}")


def _circle_png_filename(ann: EDLAnnotation) -> str:
    key = f"{ann.x}|{ann.y}|{ann.radius}|{ann.stroke_width}|{ann.color}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"circle-{digest}.png"


def render_circle_png(ann: EDLAnnotation, dest: Path) -> tuple[int, int]:
    """Render a circle annotation to a transparent PNG ring. Returns (w, h)."""
    r = ann.radius
    sw = max(1, ann.stroke_width)
    pad = sw + 2
    size = 2 * r + 2 * pad

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse(
        (pad, pad, pad + 2 * r, pad + 2 * r),
        outline=_hex_to_rgba(ann.color),
        width=sw,
    )

    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, format="PNG")
    return size, size


def render_annotation_pngs(
    annotations: list[EDLAnnotation],
    cache_dir: Path,
) -> list[AnnotationRender]:
    """Render every circle annotation to a PNG ring and return placement info.

    Non-circle shapes are ignored (they go through `build_annotation_chain`).
    """
    renders: list[AnnotationRender] = []
    for ann in annotations:
        if ann.shape != "circle":
            continue
        png_path = cache_dir / _circle_png_filename(ann)
        w, h = render_circle_png(ann, png_path)
        renders.append(AnnotationRender(
            png_path=png_path,
            x=ann.x - w // 2,
            y=ann.y - h // 2,
            start=ann.start,
            end=ann.end,
        ))
    return renders


def build_annotation_overlay_chain(
    renders: list[AnnotationRender],
    input_indices: list[int],
    base_label: str,
    final_label: str,
) -> list[str]:
    """Build ffmpeg filter_complex chains overlaying each circle PNG.

    Mirrors `build_ost_overlay_chain`: each PNG is an extra ffmpeg input, and
    input_indices[i] is the `-i` index for renders[i].png_path.
    """
    if not renders:
        return [f"{base_label}null{final_label}"]
    if len(renders) != len(input_indices):
        raise ValueError("renders and input_indices length mismatch")

    chains: list[str] = []
    cur = base_label
    for n, (r, idx) in enumerate(zip(renders, input_indices)):
        is_last = n == len(renders) - 1
        out_label = final_label if is_last else f"[vannot{n}]"
        chains.append(
            f"{cur}[{idx}:v]"
            f"overlay=x={r.x}:y={r.y}:format=auto"
            f":enable='between(t,{r.start:.3f},{r.end:.3f})'"
            f"{out_label}"
        )
        cur = out_label
    return chains
