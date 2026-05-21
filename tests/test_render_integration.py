from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from supaclip.core.edl import (
    EDL,
    EDLAudioCue,
    EDLOSTCue,
    EDLOutput,
    EDLVideoCue,
    EDLVoiceover,
    save_edl,
)
from supaclip.core.ffmpeg import probe


def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _make_synthetic(path: Path, color: str, duration: float, freq: int) -> None:
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c={color}:size=640x360:rate=30:duration={duration}",
        "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    ], check=True, capture_output=True)


def _make_voiceover_wav(path: Path, duration: float) -> None:
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
        "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(path),
    ], check=True, capture_output=True)


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg not installed")
def test_render_end_to_end_with_catalog(tmp_path: Path):
    from supaclip.catalog import add_manifest, connect
    from supaclip.core.manifest import (
        Clip,
        ExtractInfo,
        Manifest,
        SourceInfo,
        now_iso,
        save_manifest,
    )
    from supaclip.stitch.render import RenderConfig, render

    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    clip_a = clips_dir / "clip_01.mp4"
    clip_b = clips_dir / "clip_02.mp4"
    _make_synthetic(clip_a, "red", 3.0, 440)
    _make_synthetic(clip_b, "blue", 3.0, 880)

    manifest = Manifest(
        source=SourceInfo(file=str(tmp_path / "src.mp4"), duration=6.0,
                          resolution="640x360", fps=30.0),
        extract=ExtractInfo(segmenter="manual", analyzer="mock",
                            game_profile="gta", created_at=now_iso()),
        taxonomy=["cruising"],
        clips=[
            Clip(id="clip_01", file="clip_01.mp4", source_in=0.0, source_out=3.0,
                 duration=3.0, resolution="640x360", fps=30.0,
                 description="red", categories=["cruising"], score=50,
                 segment_source="manual"),
            Clip(id="clip_02", file="clip_02.mp4", source_in=0.0, source_out=3.0,
                 duration=3.0, resolution="640x360", fps=30.0,
                 description="blue", categories=["cruising"], score=60,
                 segment_source="manual"),
        ],
    )
    manifest_path = clips_dir / "manifest.json"
    save_manifest(manifest, manifest_path)

    catalog_path = tmp_path / "catalog.db"
    conn = connect(catalog_path)
    add_manifest(conn, manifest_path)
    rows = conn.execute(
        "SELECT id FROM clips ORDER BY clip_local_id"
    ).fetchall()
    clip_ids = [r[0] for r in rows]
    assert len(clip_ids) == 2
    conn.close()

    edl = EDL(
        title="smoke",
        output=EDLOutput(width=480, height=854, fps=30, duration=4.0),
        voiceover=EDLVoiceover(voice_id="mock", settings={"stability": 50},
                                script="ignored under mock"),
        video=[
            EDLVideoCue(start=0.0, end=2.0, clip_id=clip_ids[0], source_in=0.0),
            EDLVideoCue(start=2.0, end=4.0, clip_id=clip_ids[1], source_in=0.0),
        ],
        audio=[EDLAudioCue(start=0.0, end=4.0, kind="voiceover")],
        ost=[EDLOSTCue(start=0.5, end=2.0, text="HELLO", style="white_pop")],
    )
    edl_path = tmp_path / "edl.json"
    save_edl(edl, edl_path)

    vo_wav = tmp_path / "vo_cache" / "voice.wav"
    vo_wav.parent.mkdir(parents=True)
    _make_voiceover_wav(vo_wav, 4.0)

    output_path = tmp_path / "out.mp4"
    cfg = RenderConfig(
        edl_path=str(edl_path),
        output_path=str(output_path),
        catalog_path=str(catalog_path),
        cache_dir=str(tmp_path / "cache"),
        use_cache=True,
    )

    with patch("supaclip.stitch.render._synthesize", return_value=vo_wav):
        result = render(cfg)

    assert Path(result.output).exists()
    assert Path(result.sidecar).exists()
    info = probe(result.output)
    assert info.width == 480
    assert info.height == 854
    assert abs(info.duration - 4.0) < 0.2
    assert info.has_audio


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg not installed")
def test_render_v11_features_end_to_end(tmp_path: Path):
    """Render an EDL exercising freeze_first + crossfade + slow_mo + ken_burns
    + an annotation; probe the output."""
    from supaclip.catalog import add_manifest, connect
    from supaclip.core.edl import EDLAnnotation, EDLVideoCue
    from supaclip.core.manifest import (
        Clip,
        ExtractInfo,
        Manifest,
        SourceInfo,
        now_iso,
        save_manifest,
    )
    from supaclip.stitch.render import RenderConfig, render

    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    a = clips_dir / "clip_01.mp4"
    b = clips_dir / "clip_02.mp4"
    _make_synthetic(a, "red", 6.0, 440)
    _make_synthetic(b, "blue", 6.0, 880)

    manifest = Manifest(
        source=SourceInfo(file=str(tmp_path / "src.mp4"), duration=12.0,
                          resolution="640x360", fps=30.0),
        extract=ExtractInfo(segmenter="manual", analyzer="mock",
                            game_profile="gta", created_at=now_iso()),
        taxonomy=["cruising"],
        clips=[
            Clip(id="clip_01", file="clip_01.mp4", source_in=0.0, source_out=6.0,
                 duration=6.0, resolution="640x360", fps=30.0,
                 description="red", categories=["cruising"], score=50,
                 segment_source="manual"),
            Clip(id="clip_02", file="clip_02.mp4", source_in=0.0, source_out=6.0,
                 duration=6.0, resolution="640x360", fps=30.0,
                 description="blue", categories=["cruising"], score=60,
                 segment_source="manual"),
        ],
    )
    save_manifest(manifest, clips_dir / "manifest.json")
    catalog_path = tmp_path / "catalog.db"
    conn = connect(catalog_path)
    add_manifest(conn, clips_dir / "manifest.json")
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM clips ORDER BY clip_local_id").fetchall()]
    conn.close()

    edl = EDL(
        title="v11 smoke",
        output=EDLOutput(width=480, height=854, fps=30, duration=8.0),
        voiceover=EDLVoiceover(voice_id="mock", settings={"stability": 50},
                                script="ignored"),
        video=[
            EDLVideoCue(start=0.0, end=2.0, clip_id=ids[0], effect="freeze_first"),
            EDLVideoCue(start=2.0, end=5.0, clip_id=ids[0],
                       transition_in="crossfade", transition_duration=0.3,
                       effect="ken_burns_in"),
            EDLVideoCue(start=5.0, end=8.0, clip_id=ids[1],
                       effect="slow_mo", effect_params={"speed": 0.5}),
        ],
        audio=[EDLAudioCue(start=0.0, end=8.0, kind="voiceover")],
        ost=[EDLOSTCue(start=0.5, end=2.0, text="FREEZE", style="bold_yellow")],
        annotations=[EDLAnnotation(start=0.5, end=1.8, shape="circle",
                                     x=240, y=400, radius=80)],
    )
    edl_path = tmp_path / "edl.json"
    save_edl(edl, edl_path)

    vo_wav = tmp_path / "vo.wav"
    _make_voiceover_wav(vo_wav, 8.0)

    output_path = tmp_path / "v11.mp4"
    cfg = RenderConfig(
        edl_path=str(edl_path),
        output_path=str(output_path),
        catalog_path=str(catalog_path),
        cache_dir=str(tmp_path / "cache"),
        use_cache=True,
    )
    with patch("supaclip.stitch.render._synthesize", return_value=vo_wav):
        result = render(cfg)

    info = probe(result.output)
    assert info.width == 480
    assert info.height == 854
    assert abs(info.duration - 8.0) < 0.3
    assert info.has_audio


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg not installed")
def test_render_print_ffmpeg_skips_execution(tmp_path: Path, capsys):
    """--print-ffmpeg should write the command to stdout without running."""
    from supaclip.catalog import add_manifest, connect
    from supaclip.core.manifest import (
        Clip, ExtractInfo, Manifest, SourceInfo, now_iso, save_manifest,
    )
    from supaclip.stitch.render import RenderConfig, render

    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    c = clips_dir / "clip_01.mp4"
    _make_synthetic(c, "red", 3.0, 440)

    manifest = Manifest(
        source=SourceInfo(file=str(tmp_path / "src.mp4"), duration=3.0,
                          resolution="640x360", fps=30.0),
        extract=ExtractInfo(segmenter="manual", analyzer="mock",
                            game_profile="gta", created_at=now_iso()),
        taxonomy=["cruising"],
        clips=[Clip(id="clip_01", file="clip_01.mp4", source_in=0.0,
                    source_out=3.0, duration=3.0, resolution="640x360",
                    fps=30.0, description="x", categories=["cruising"],
                    score=50, segment_source="manual")],
    )
    save_manifest(manifest, clips_dir / "manifest.json")
    catalog_path = tmp_path / "catalog.db"
    conn = connect(catalog_path)
    add_manifest(conn, clips_dir / "manifest.json")
    cid = conn.execute("SELECT id FROM clips").fetchone()[0]
    conn.close()

    edl = EDL(
        title="t",
        output=EDLOutput(width=480, height=854, fps=30, duration=2.0),
        video=[EDLVideoCue(start=0.0, end=2.0, clip_id=cid)],
        audio=[], ost=[],
    )
    edl_path = tmp_path / "edl.json"
    save_edl(edl, edl_path)
    output_path = tmp_path / "out.mp4"

    cfg = RenderConfig(
        edl_path=str(edl_path),
        output_path=str(output_path),
        catalog_path=str(catalog_path),
        print_only=True,
    )
    render(cfg)

    captured = capsys.readouterr()
    assert "ffmpeg" in captured.out
    assert "-filter_complex" in captured.out
    assert not output_path.exists()
