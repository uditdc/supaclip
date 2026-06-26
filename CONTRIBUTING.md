# Contributing to supaclip

Thanks for your interest in improving supaclip! This guide covers local setup,
the conventions the codebase follows, and how to get a change merged.

## Development setup

```bash
git clone https://github.com/uditdc/supaclip.git
cd supaclip
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,mcp]"      # add ,align if you touch forced alignment
```

You also need `ffmpeg` and `ffprobe` on your `PATH`:

```bash
sudo apt install ffmpeg      # Debian/Ubuntu
brew install ffmpeg          # macOS
```

## Running tests

```bash
pytest -q            # or: make test
```

The suite is fast and runs without any API keys — TTS and model calls are
stubbed. Integration tests that shell out to `ffmpeg` skip themselves when it
isn't installed. **Every change should keep the suite green and add tests for
new behavior** (renderer changes in particular: prefer asserting on the
generated ffmpeg filter graph, as the existing `tests/test_assembly.py` does,
over a full render).

## Code style

- **Lint with [Ruff](https://github.com/astral-sh/ruff):** `ruff check .`
- **Self-documenting code over comments.** Don't add comments that restate what
  the code does or narrate obvious steps; reach for a clearer name instead.
  Reserve comments for *why* something non-obvious is done. Never commit
  commented-out code.
- Match the surrounding style — type hints, `from __future__ import annotations`,
  small pure functions, and dataclasses for structured values are the norm.
- Keep modules focused: `core/` is shared plumbing, `extract/` /`catalog/` /
  `stitch/` are the three stages. New ffmpeg filter logic belongs in a pure
  builder function under `stitch/` with a unit test on its output string.

## Submitting changes

1. Branch off `main` (`git checkout -b feat/your-thing`).
2. Make the change with accompanying tests and doc updates
   (`docs/`, `README.md`, and the `stitch-director` skill if behavior changes).
3. Run `pytest -q` and `ruff check .`.
4. Open a PR with a clear description of the what and why. CI runs the test
   suite on Python 3.10–3.12 and builds the wheel.

## Reporting bugs & ideas

Open a [GitHub issue](https://github.com/uditdc/supaclip/issues). For bugs,
include the command you ran, what you expected, what happened, and your
`ffmpeg -version` and OS. For a render problem, the EDL (with any keys removed)
and the `--print-ffmpeg` output are gold.

By contributing, you agree your contributions are licensed under the project's
[MIT License](LICENSE).
