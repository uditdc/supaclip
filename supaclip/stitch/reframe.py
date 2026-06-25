from __future__ import annotations

from supaclip.core.edl import ReframeMode

DEFAULT_WIDTH = 1080
DEFAULT_HEIGHT = 1920
DEFAULT_FPS = 60


def build_reframe_filter(
    mode: ReframeMode = "crop_center",
    dst_w: int = DEFAULT_WIDTH,
    dst_h: int = DEFAULT_HEIGHT,
    fps: int = DEFAULT_FPS,
    offset: int = 0,
) -> str:
    """Return an ffmpeg filter-chain string that maps any source resolution to
    (dst_w, dst_h) at `fps` frames/s. Used per-input inside a filter_complex.

    `offset` pans the crop window horizontally by that many source pixels
    (positive = right), clamped to the valid crop range. Ignored for letterbox.
    """
    if mode == "letterbox":
        return (
            f"scale={dst_w}:{dst_h}:force_original_aspect_ratio=decrease,"
            f"pad={dst_w}:{dst_h}:(ow-iw)/2:(oh-ih)/2:black,"
            f"setsar=1,fps={fps},format=yuv420p"
        )

    if mode == "crop_left":
        base_x, y_expr = "0", "(in_h-out_h)/2"
    elif mode == "crop_right":
        base_x, y_expr = "in_w-out_w", "(in_h-out_h)/2"
    else:
        base_x, y_expr = "(in_w-out_w)/2", "(in_h-out_h)/2"

    x_expr = base_x if offset == 0 else f"clip({base_x}+({offset})\\,0\\,in_w-out_w)"

    crop_w = f"min(in_w\\,in_h*{dst_w}/{dst_h})"
    crop_h = f"min(in_h\\,in_w*{dst_h}/{dst_w})"
    return (
        f"crop={crop_w}:{crop_h}:{x_expr}:{y_expr},"
        f"scale={dst_w}:{dst_h},"
        f"setsar=1,fps={fps},format=yuv420p"
    )
