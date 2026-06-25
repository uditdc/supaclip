from __future__ import annotations

from dataclasses import dataclass

from supaclip.core.edl import EDL


@dataclass(frozen=True)
class EncoderProfile:
    codec: str
    hardware: bool
    family: str


ENCODER_PROFILES: dict[str, EncoderProfile] = {
    "libx264": EncoderProfile("libx264", False, "h264"),
    "libx265": EncoderProfile("libx265", False, "hevc"),
    "h264_nvenc": EncoderProfile("h264_nvenc", True, "h264"),
    "hevc_nvenc": EncoderProfile("hevc_nvenc", True, "hevc"),
    "h264_videotoolbox": EncoderProfile("h264_videotoolbox", True, "h264"),
    "hevc_videotoolbox": EncoderProfile("hevc_videotoolbox", True, "hevc"),
    "h264_qsv": EncoderProfile("h264_qsv", True, "h264"),
    "hevc_qsv": EncoderProfile("hevc_qsv", True, "hevc"),
}

ENCODER_CHOICES = ("auto", *ENCODER_PROFILES)

_AUTO_PRIORITY = (
    "h264_nvenc",
    "h264_videotoolbox",
    "h264_qsv",
    "libx264",
)

_NVENC_PRESET = {
    "ultrafast": "p1",
    "superfast": "p1",
    "veryfast": "p2",
    "faster": "p3",
    "fast": "p3",
    "medium": "p4",
    "slow": "p5",
    "slower": "p6",
    "veryslow": "p7",
}


def _nvenc_preset(preset: str) -> str:
    return _NVENC_PRESET.get(preset, "p4")


def _crf_to_vt_quality(crf: int) -> int:
    """Map an x264-style CRF (lower = better) to a VideoToolbox -q:v (higher = better)."""
    return max(1, min(100, round(100 * (1 - crf / 51))))


def build_video_encode_args(encoder: str, preset: str, crf: int) -> list[str]:
    """Return the `-c:v ...` rate-control args for the chosen encoder."""
    try:
        profile = ENCODER_PROFILES[encoder]
    except KeyError:
        raise ValueError(f"unknown encoder: {encoder!r}") from None

    codec = profile.codec
    args = ["-c:v", codec]

    if codec in ("libx264", "libx265"):
        args += ["-preset", preset, "-crf", str(crf)]
    elif codec.endswith("nvenc"):
        args += ["-preset", _nvenc_preset(preset), "-rc", "vbr", "-cq", str(crf), "-b:v", "0"]
    elif codec.endswith("videotoolbox"):
        args += ["-q:v", str(_crf_to_vt_quality(crf))]
    elif codec.endswith("qsv"):
        args += ["-preset", preset, "-global_quality", str(crf)]
    return args


def select_encoder(
    preference: str,
    available: set[str] | None = None,
    probe=None,
) -> str:
    """Resolve an encoder preference against the encoders ffmpeg can actually use.

    `auto` picks the first hardware encoder that initializes (in priority order),
    falling back to libx264. Hardware encoders are functionally probed because a
    build can advertise codecs with no matching device present. An explicit
    choice is returned only if usable; otherwise raises with a clear message.
    """
    if available is None:
        from supaclip.core.ffmpeg import list_encoders
        available = list_encoders()
    if probe is None:
        from supaclip.core.ffmpeg import probe_encoder as probe

    def usable(name: str) -> bool:
        if name not in available:
            return False
        profile = ENCODER_PROFILES[name]
        return True if not profile.hardware else probe(profile.codec)

    if preference == "auto":
        for name in _AUTO_PRIORITY:
            if usable(name):
                return name
        return "libx264"

    if preference not in ENCODER_PROFILES:
        raise ValueError(f"unknown encoder: {preference!r}")
    if preference not in available:
        usable_names = sorted(n for n in ENCODER_PROFILES if n in available)
        raise ValueError(
            f"encoder {preference!r} is not available in this ffmpeg build; "
            f"available: {', '.join(usable_names) or 'none'}"
        )
    if not usable(preference):
        raise ValueError(
            f"encoder {preference!r} is present but failed to initialize "
            "(no compatible hardware/driver?); try --encoder auto or libx264"
        )
    return preference


RESOLUTION_PRESETS: dict[str, int] = {
    "720p": 720,
    "1080p": 1080,
    "1440p": 1440,
    "2160p": 2160,
    "4k": 2160,
}

RESOLUTION_CHOICES = tuple(RESOLUTION_PRESETS)


def _round_even(value: float) -> int:
    n = int(round(value))
    if n % 2:
        n += 1
    return max(2, n)


def resolution_scale_factor(width: int, height: int, resolution: str) -> float:
    """Factor that scales (width, height) so its short side hits the preset target."""
    try:
        target = RESOLUTION_PRESETS[resolution]
    except KeyError:
        raise ValueError(f"unknown resolution: {resolution!r}") from None
    return target / min(width, height)


def _scale_int(value: int, factor: float, minimum: int = 0) -> int:
    return max(minimum, int(round(value * factor)))


def scale_edl(edl: EDL, factor: float) -> EDL:
    """Return a copy of `edl` with every pixel-space field scaled by `factor`.

    Used to render an authored composition at a different resolution without
    re-authoring coordinates: output dimensions, reframe offsets, annotation
    geometry, watermark and caption font sizes all scale together.
    """
    if factor == 1.0:
        return edl

    out = edl.output
    new_out = out.model_copy(update={
        "width": _round_even(out.width * factor),
        "height": _round_even(out.height * factor),
    })
    if out.watermark is not None:
        new_out = new_out.model_copy(update={
            "watermark": out.watermark.model_copy(update={
                "font_size": _scale_int(out.watermark.font_size, factor, 1),
            }),
        })

    new_video = [
        cue.model_copy(update={"reframe_offset": _scale_int(cue.reframe_offset, factor)})
        for cue in edl.video
    ]

    new_annotations = [
        ann.model_copy(update={
            "x": _scale_int(ann.x, factor),
            "y": _scale_int(ann.y, factor),
            "radius": _scale_int(ann.radius, factor),
            "width": _scale_int(ann.width, factor),
            "height": _scale_int(ann.height, factor),
            "stroke_width": _scale_int(ann.stroke_width, factor, 1),
        })
        for ann in edl.annotations
    ]

    update: dict = {
        "output": new_out,
        "video": new_video,
        "annotations": new_annotations,
    }
    if edl.captions is not None and edl.captions.font_size is not None:
        update["captions"] = edl.captions.model_copy(update={
            "font_size": _scale_int(edl.captions.font_size, factor, 1),
        })

    return edl.model_copy(update=update)
