from __future__ import annotations

import sys


USAGE = """\
clipper <command> [args...]

Commands:
  extract   Split a local video into clips and a manifest.json
  catalog   Add manifests to a global catalog and search across them
  stitch    Render a short-form video from a Claude-authored EDL
  mcp       Run the MCP server (exposes catalog to Claude over stdio)

Run `clipper <command> --help` for command-specific help.
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help"}:
        sys.stdout.write(USAGE)
        return 0

    cmd, rest = argv[0], argv[1:]

    if cmd == "extract":
        from .extract.cli import main as extract_main
        return extract_main(rest)

    if cmd == "catalog":
        from .catalog.cli import main as catalog_main
        return catalog_main(rest)

    if cmd == "stitch":
        from .stitch.cli import main as stitch_main
        return stitch_main(rest)

    if cmd == "mcp":
        try:
            from .catalog.mcp import main as mcp_main
        except ImportError as e:
            sys.stderr.write(
                f"MCP support not installed ({e}). Install with: "
                f"pip install -e \".[mcp]\"\n"
            )
            return 2
        return mcp_main(rest)

    sys.stderr.write(f"unknown command: {cmd}\n\n{USAGE}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
