from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..core.log import Logger
from . import search as search_mod
from .db import connect
from .ingest import add_directory, add_manifest, remove_manifest
from .paths import resolve_catalog_path
from .search import parse_signal_filter


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="clipper catalog",
        description="Store and search clips across many extract runs.",
    )
    p.add_argument(
        "--catalog",
        default=None,
        help="Path to the catalog DB (default: $CLIPPER_CATALOG or "
        "~/.local/share/supaclip/catalog.db)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="ingest one or more manifest.json files")
    p_add.add_argument("paths", nargs="+", help="manifest.json files or directories")

    p_rm = sub.add_parser("remove", help="remove a manifest's clips from the catalog")
    p_rm.add_argument("manifest", help="path to manifest.json")

    p_list = sub.add_parser("list", help="list sources or extracts")
    g = p_list.add_mutually_exclusive_group()
    g.add_argument("--sources", action="store_true")
    g.add_argument("--extracts", action="store_true")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.add_argument("--json", dest="emit_json", action="store_true")

    p_stats = sub.add_parser("stats", help="catalog row counts")
    p_stats.add_argument("--json", dest="emit_json", action="store_true")

    p_search = sub.add_parser("search", help="search clips")
    p_search.add_argument("query", nargs="?", default=None,
                          help="FTS5 query over description/audio_cues/tags")
    p_search.add_argument("--category", action="append", default=[],
                          help="filter by category (repeatable)")
    p_search.add_argument("--all-categories", action="store_true",
                          help="require all --category values (AND mode)")
    p_search.add_argument("--min-score", type=int)
    p_search.add_argument("--max-score", type=int)
    p_search.add_argument("--min-duration", type=float)
    p_search.add_argument("--max-duration", type=float)
    p_search.add_argument("--segmenter")
    p_search.add_argument("--game-profile")
    p_search.add_argument("--source", help="source file path or fingerprint")
    p_search.add_argument("--signal", action="append", default=[],
                          help="game-signal filter; key=value (exact) or key~=value (substring)")
    p_search.add_argument("--order-by",
                          choices=("score", "duration", "created_at"),
                          default="score")
    p_search.add_argument("--limit", type=int, default=50)
    p_search.add_argument("--json", dest="emit_json", action="store_true")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    log = Logger(verbose=False)

    catalog_path = resolve_catalog_path(args.catalog)
    conn = connect(catalog_path)
    try:
        if args.cmd == "add":
            return _cmd_add(args, conn, log)
        if args.cmd == "remove":
            return _cmd_remove(args, conn, log)
        if args.cmd == "list":
            return _cmd_list(args, conn)
        if args.cmd == "stats":
            return _cmd_stats(args, conn, catalog_path)
        if args.cmd == "search":
            return _cmd_search(args, conn)
    finally:
        conn.close()
    return 2


def _cmd_add(args, conn, log: Logger) -> int:
    total = 0
    for raw in args.paths:
        path = Path(raw).expanduser().resolve()
        if path.is_dir():
            log.stage(f"Walking {path}")
            results = add_directory(conn, path)
            for r in results:
                _log_ingest(log, r)
            total += len(results)
        elif path.is_file():
            r = add_manifest(conn, path)
            _log_ingest(log, r)
            total += 1
        else:
            log.error(f"not found: {path}")
            return 2
    log.success(f"ingested {total} manifest(s)")
    return 0


def _log_ingest(log: Logger, r) -> None:
    verb = "added" if r.created else "updated"
    log.info(f"{verb} {r.manifest_path} (source={r.source_id} "
             f"extract={r.extract_id} clips={r.clip_count})")


def _cmd_remove(args, conn, log: Logger) -> int:
    path = Path(args.manifest).expanduser().resolve()
    removed = remove_manifest(conn, path)
    if removed == 0:
        log.warn(f"no matching extracts for {path}")
        return 1
    log.success(f"removed {removed} extract row(s) from {path}")
    return 0


def _cmd_list(args, conn) -> int:
    if args.extracts:
        rows = conn.execute(
            """SELECT e.id, e.segmenter, e.analyzer, e.game_profile,
                      e.created_at, e.manifest_path,
                      s.file_path AS source_file,
                      (SELECT COUNT(*) FROM clips c WHERE c.extract_id = e.id) AS clip_count
               FROM extracts e JOIN sources s ON s.id = e.source_id
               ORDER BY e.id LIMIT ?""",
            (args.limit,),
        ).fetchall()
        items = [dict(r) for r in rows]
    else:
        items = search_mod.list_sources(conn)[: args.limit]

    if args.emit_json:
        json.dump(items, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    if not items:
        print("(empty)")
        return 0

    if args.extracts:
        for r in items:
            print(f"#{r['id']:>4}  {r['created_at']}  {r['segmenter']:<9} "
                  f"{r['analyzer']:<35} clips={r['clip_count']:>3}  "
                  f"{r['source_file']}")
    else:
        for r in items:
            print(f"#{r['id']:>4}  {r['resolution']:<11} {r['duration']:>7.1f}s "
                  f"extracts={r['extract_count']:>2} clips={r['clip_count']:>3}  "
                  f"{r['file_path']}")
    return 0


def _cmd_stats(args, conn, catalog_path: Path) -> int:
    s = search_mod.stats(conn)
    size = catalog_path.stat().st_size if catalog_path.exists() else 0
    payload = {"catalog": str(catalog_path), "size_bytes": size, **s}
    if args.emit_json:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    print(f"catalog : {catalog_path}")
    print(f"size    : {size} bytes")
    for k in ("sources", "extracts", "clips", "clip_categories"):
        print(f"{k:<16}: {payload[k]}")
    return 0


def _cmd_search(args, conn) -> int:
    signals = []
    for tok in args.signal:
        signals.append(parse_signal_filter(tok))

    results = search_mod.search(
        conn,
        query=args.query,
        categories=args.category or None,
        all_categories=args.all_categories,
        min_score=args.min_score,
        max_score=args.max_score,
        min_duration=args.min_duration,
        max_duration=args.max_duration,
        segmenter=args.segmenter,
        game_profile=args.game_profile,
        source=args.source,
        signals=signals or None,
        order_by=args.order_by,
        limit=args.limit,
    )
    if args.emit_json:
        json.dump([r.to_dict() for r in results], sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if not results:
        print("(no matches)")
        return 0
    for r in results:
        cats = ",".join(r.categories) if r.categories else "-"
        print(f"#{r.clip_id:>5}  score={r.score:>3}  {r.duration:>6.1f}s  "
              f"[{cats}]  {_truncate(r.description, 90)}")
        print(f"        file={r.file}")
    return 0


def _truncate(text: str, n: int) -> str:
    text = " ".join(text.split())
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


if __name__ == "__main__":
    raise SystemExit(main())
