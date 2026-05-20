from __future__ import annotations

from dataclasses import dataclass

from clipper.core.edl import EDLOSTCue, OSTStyle


@dataclass(frozen=True)
class OSTStylePreset:
    fontsize: int
    fontcolor: str
    borderw: int = 0
    bordercolor: str = "black"
    box: bool = False
    boxcolor: str = "black@0.6"
    boxborderw: int = 24
    shadowx: int = 0
    shadowy: int = 0
    shadowcolor: str = "black@0.8"
    x_expr: str = "(w-text_w)/2"
    y_expr: str = "h*0.18"


STYLE_PRESETS: dict[OSTStyle, OSTStylePreset] = {
    "bold_yellow": OSTStylePreset(
        fontsize=96, fontcolor="yellow",
        borderw=6, bordercolor="black",
        shadowx=4, shadowy=4,
        y_expr="h*0.15",
    ),
    "red_strike": OSTStylePreset(
        fontsize=84, fontcolor="#ff3b30",
        borderw=4, bordercolor="black",
        shadowx=3, shadowy=3,
        y_expr="h*0.30",
    ),
    "neon_pink": OSTStylePreset(
        fontsize=88, fontcolor="#ff2bd6",
        borderw=4, bordercolor="white",
        shadowx=4, shadowy=4, shadowcolor="#7a0058",
        y_expr="h*0.30",
    ),
    "white_pop": OSTStylePreset(
        fontsize=110, fontcolor="white",
        borderw=6, bordercolor="black",
        shadowx=5, shadowy=5,
        y_expr="(h-text_h)/2",
    ),
    "comment_trap": OSTStylePreset(
        fontsize=72, fontcolor="white",
        borderw=4, bordercolor="black",
        box=True, boxcolor="black@0.55",
        y_expr="h*0.80",
    ),
}


def _escape_drawtext(text: str) -> str:
    """Escape characters that have special meaning to ffmpeg's drawtext filter."""
    out = text.replace("\\", "\\\\")
    out = out.replace(":", r"\:")
    out = out.replace("'", r"\'")
    out = out.replace("%", r"\%")
    return out


def build_drawtext(cue: EDLOSTCue, fontfile: str | None = None) -> str:
    preset = STYLE_PRESETS[cue.style]
    parts: list[str] = [
        "drawtext=",
        f"text='{_escape_drawtext(cue.text)}'",
        f":fontsize={preset.fontsize}",
        f":fontcolor={preset.fontcolor}",
        f":x={preset.x_expr}",
        f":y={preset.y_expr}",
        f":enable='between(t,{cue.start:.3f},{cue.end:.3f})'",
    ]
    if fontfile:
        parts.append(f":fontfile={fontfile}")
    if preset.borderw:
        parts.append(f":borderw={preset.borderw}")
        parts.append(f":bordercolor={preset.bordercolor}")
    if preset.shadowx or preset.shadowy:
        parts.append(f":shadowx={preset.shadowx}")
        parts.append(f":shadowy={preset.shadowy}")
        parts.append(f":shadowcolor={preset.shadowcolor}")
    if preset.box:
        parts.append(":box=1")
        parts.append(f":boxcolor={preset.boxcolor}")
        parts.append(f":boxborderw={preset.boxborderw}")
    return "".join(parts)


def build_ost_chain(cues: list[EDLOSTCue], fontfile: str | None = None) -> str:
    """Chain multiple drawtexts with commas. Returns empty string if no cues."""
    return ",".join(build_drawtext(c, fontfile=fontfile) for c in cues)
