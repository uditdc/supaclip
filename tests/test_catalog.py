from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from supaclip.catalog.db import connect
from supaclip.catalog.ingest import add_directory, add_manifest, remove_manifest
from supaclip.catalog.paths import resolve_catalog_path
from supaclip.catalog.schema import SCHEMA_VERSION, migrate
from supaclip.catalog.search import (
    get_clip,
    get_source,
    list_sources,
    parse_signal_filter,
    search,
    stats,
)
from supaclip.core.manifest import (
    AudioInfo,
    Clip,
    ExtractInfo,
    Manifest,
    SourceInfo,
    now_iso,
    save_manifest,
)


def _make_manifest(
    *,
    source_file: Path,
    created_at: str | None = None,
    clips=None,
) -> Manifest:
    if clips is None:
        clips = [
            Clip(
                id="clip_01",
                file="clip_01.mp4",
                source_in=0.0,
                source_out=20.0,
                duration=20.0,
                resolution="1920x1080",
                fps=60.0,
                description="A high-speed police chase on the freeway with sirens blaring.",
                categories=["police_chase", "crash"],
                score=82,
                game_signals={"wanted_level": 4, "vehicles": ["police cruiser", "sports car"]},
                audio=AudioInfo(peak_loudness_db=-8.2, cues=["sirens", "collision"]),
                keyframes=["clip_01.kf01.jpg", "clip_01.kf02.jpg"],
                segment_source="auto",
            ),
            Clip(
                id="clip_02",
                file="clip_02.mp4",
                source_in=25.0,
                source_out=55.0,
                duration=30.0,
                resolution="1920x1080",
                fps=60.0,
                description="Calm cruising through Vinewood at sunset.",
                categories=["cruising"],
                score=40,
                game_signals={"wanted_level": 0, "vehicles": ["sports car"]},
                audio=AudioInfo(peak_loudness_db=-25.0, cues=["engine"]),
                keyframes=["clip_02.kf01.jpg"],
                segment_source="auto",
            ),
        ]
    return Manifest(
        source=SourceInfo(
            file=str(source_file),
            duration=300.0,
            resolution="1920x1080",
            fps=60.0,
        ),
        extract=ExtractInfo(
            segmenter="auto",
            analyzer="gemma4",
            game_profile="gta",
            created_at=created_at or now_iso(),
        ),
        taxonomy=["police_chase", "shootout", "stunt", "crash",
                  "npc_chaos", "cruising", "mission", "fail"],
        clips=clips,
    )


def _write(tmp_path: Path, name: str = "manifest.json", **kwargs) -> Path:
    out_dir = tmp_path / name.replace("/", "_").rsplit(".", 1)[0]
    out_dir.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "fake_source.mp4"
    source.write_bytes(b"FAKE")
    manifest = _make_manifest(source_file=source, **kwargs)
    manifest_path = out_dir / "manifest.json"
    save_manifest(manifest, manifest_path)
    return manifest_path


# ---------------------- schema / migration ----------------------

def test_migrate_fresh_and_idempotent(tmp_path: Path):
    db = tmp_path / "c.db"
    conn = connect(db)
    row = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()
    assert int(row[0]) == SCHEMA_VERSION
    # rerunning migrate is a no-op
    migrate(conn)
    row = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()
    assert int(row[0]) == SCHEMA_VERSION
    conn.close()


# ---------------------- paths ----------------------

def test_resolve_catalog_path_priority(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SUPACLIP_CATALOG", raising=False)
    assert resolve_catalog_path("/tmp/explicit.db") == Path("/tmp/explicit.db").resolve()

    monkeypatch.setenv("SUPACLIP_CATALOG", str(tmp_path / "env.db"))
    assert resolve_catalog_path() == (tmp_path / "env.db").resolve()


# ---------------------- ingest ----------------------

def test_add_manifest_idempotent(tmp_path: Path):
    manifest_path = _write(tmp_path)
    conn = connect(tmp_path / "c.db")

    r1 = add_manifest(conn, manifest_path)
    assert r1.created is True
    assert r1.clip_count == 2

    r2 = add_manifest(conn, manifest_path)
    assert r2.created is False
    assert r2.extract_id == r1.extract_id
    assert r2.source_id == r1.source_id
    assert r2.clip_count == 2

    # Still only one source, one extract
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM extracts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == 2
    conn.close()


def test_add_manifest_moved_updates_path(tmp_path: Path):
    manifest_path = _write(tmp_path)
    conn = connect(tmp_path / "c.db")
    add_manifest(conn, manifest_path)

    new_dir = tmp_path / "moved"
    new_dir.mkdir()
    new_path = new_dir / "manifest.json"
    new_path.write_text(manifest_path.read_text())

    r = add_manifest(conn, new_path)
    assert r.created is False
    assert conn.execute("SELECT COUNT(*) FROM extracts").fetchone()[0] == 1
    stored = conn.execute("SELECT manifest_path FROM extracts").fetchone()[0]
    assert stored == str(new_path)
    conn.close()


def test_add_directory_walks_recursively(tmp_path: Path):
    _write(tmp_path, name="a.json", created_at="2026-05-17T10:00:00+05:30")
    sub = tmp_path / "nested"
    sub.mkdir()
    source = tmp_path / "fake_source.mp4"
    m = _make_manifest(source_file=source, created_at="2026-05-17T11:00:00+05:30")
    save_manifest(m, sub / "manifest.json")

    conn = connect(tmp_path / "c.db")
    results = add_directory(conn, tmp_path)
    assert len(results) == 2
    assert conn.execute("SELECT COUNT(*) FROM extracts").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
    conn.close()


def test_remove_manifest_cascades(tmp_path: Path):
    manifest_path = _write(tmp_path)
    conn = connect(tmp_path / "c.db")
    add_manifest(conn, manifest_path)
    assert conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM clip_categories").fetchone()[0] >= 2
    assert conn.execute("SELECT COUNT(*) FROM clips_fts").fetchone()[0] == 2

    removed = remove_manifest(conn, manifest_path)
    assert removed == 1
    assert conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM clip_categories").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM clips_fts").fetchone()[0] == 0
    conn.close()


# ---------------------- search ----------------------

@pytest.fixture
def populated(tmp_path: Path):
    manifest_path = _write(tmp_path)
    conn = connect(tmp_path / "c.db")
    add_manifest(conn, manifest_path)
    yield conn, manifest_path
    conn.close()


def test_search_fts_query(populated):
    conn, _ = populated
    res = search(conn, query="chase")
    assert len(res) == 1
    assert "chase" in res[0].description.lower()


def test_search_category_filter_or(populated):
    conn, _ = populated
    res = search(conn, categories=["cruising"])
    assert len(res) == 1
    assert "cruising" in res[0].categories


def test_search_category_filter_and(populated):
    conn, _ = populated
    res = search(conn, categories=["police_chase", "crash"], all_categories=True)
    assert len(res) == 1
    res = search(conn, categories=["police_chase", "shootout"], all_categories=True)
    assert len(res) == 0


def test_search_score_range(populated):
    conn, _ = populated
    res = search(conn, min_score=70)
    assert len(res) == 1
    assert res[0].score == 82
    res = search(conn, max_score=50)
    assert len(res) == 1
    assert res[0].score == 40


def test_search_duration_range(populated):
    conn, _ = populated
    res = search(conn, min_duration=25)
    assert len(res) == 1
    assert res[0].duration == pytest.approx(30.0)


def test_search_signal_exact_int(populated):
    conn, _ = populated
    res = search(conn, signals=[("wanted_level", "=", "4")])
    assert len(res) == 1
    assert res[0].game_signals["wanted_level"] == 4


def test_search_signal_substring_list(populated):
    conn, _ = populated
    res = search(conn, signals=[("vehicles", "~=", "police")])
    assert len(res) == 1
    assert "police" in str(res[0].game_signals["vehicles"]).lower()


def test_search_source_filter(populated, tmp_path):
    conn, manifest_path = populated
    row = conn.execute("SELECT file_path, fingerprint FROM sources").fetchone()
    res = search(conn, source=row["file_path"])
    assert len(res) == 2
    res = search(conn, source=row["fingerprint"])
    assert len(res) == 2
    res = search(conn, source="/nonexistent")
    assert res == []


def test_search_ordering(populated):
    conn, _ = populated
    res = search(conn, order_by="score")
    assert res[0].score >= res[1].score
    res = search(conn, order_by="duration")
    assert res[0].duration >= res[1].duration


def test_search_limit(populated):
    conn, _ = populated
    res = search(conn, limit=1)
    assert len(res) == 1


def test_search_resolves_paths(populated, tmp_path):
    conn, manifest_path = populated
    res = search(conn)
    for r in res:
        assert Path(r.file).is_absolute()
        assert Path(r.file).parent == manifest_path.parent
        for kf in r.keyframes:
            assert Path(kf).is_absolute()


def test_get_clip_and_get_source(populated):
    conn, _ = populated
    res = search(conn)
    one = get_clip(conn, res[0].clip_id)
    assert one is not None
    assert one.description == res[0].description
    assert get_clip(conn, 99999) is None

    src = get_source(conn, res[0].source_id)
    assert src is not None
    assert get_source(conn, 99999) is None


def test_list_sources_and_stats(populated):
    conn, _ = populated
    sources = list_sources(conn)
    assert len(sources) == 1
    assert sources[0]["clip_count"] == 2

    s = stats(conn)
    assert s["sources"] == 1
    assert s["extracts"] == 1
    assert s["clips"] == 2


def test_parse_signal_filter():
    assert parse_signal_filter("wanted_level=4") == ("wanted_level", "=", "4")
    assert parse_signal_filter("vehicles~=police") == ("vehicles", "~=", "police")
    with pytest.raises(ValueError):
        parse_signal_filter("nope")


# ---------------------- CLI smoke ----------------------

def test_cli_help_is_fast():
    res = subprocess.run(
        [sys.executable, "-m", "supaclip.cli", "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert res.returncode == 0
    assert "catalog" in res.stdout


def test_cli_catalog_add_search_roundtrip(tmp_path: Path):
    manifest_path = _write(tmp_path)
    catalog_db = tmp_path / "c.db"
    env_args = ["--catalog", str(catalog_db)]

    add = subprocess.run(
        [sys.executable, "-m", "supaclip.cli", "catalog", *env_args,
         "add", str(manifest_path)],
        capture_output=True, text=True, timeout=15,
    )
    assert add.returncode == 0, add.stderr

    srch = subprocess.run(
        [sys.executable, "-m", "supaclip.cli", "catalog", *env_args,
         "search", "chase", "--json"],
        capture_output=True, text=True, timeout=15,
    )
    assert srch.returncode == 0, srch.stderr
    payload = json.loads(srch.stdout)
    assert len(payload) == 1
    assert "chase" in payload[0]["description"].lower()


# ---------------------- MCP (optional) ----------------------

def test_mcp_tools_callable_when_installed(tmp_path: Path, monkeypatch):
    pytest.importorskip("mcp")
    manifest_path = _write(tmp_path)
    catalog_db = tmp_path / "c.db"
    monkeypatch.setenv("SUPACLIP_CATALOG", str(catalog_db))

    conn = connect(catalog_db)
    add_manifest(conn, manifest_path)
    conn.close()

    from supaclip.catalog import mcp as mcp_mod
    server = mcp_mod._build_server()
    assert server is not None  # build succeeded; tool decorators executed
