from __future__ import annotations

import sys
from typing import TextIO

_RESET = "\033[0m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_BLUE = "\033[34m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_CYAN = "\033[36m"


class Logger:
    def __init__(self, verbose: bool = False, stream: TextIO | None = None) -> None:
        self.verbose = verbose
        self.stream = stream or sys.stderr
        self._color = self.stream.isatty()

    def _wrap(self, code: str, text: str) -> str:
        return f"{code}{text}{_RESET}" if self._color else text

    def _write(self, line: str) -> None:
        self.stream.write(line + "\n")
        self.stream.flush()

    def stage(self, name: str) -> None:
        self._write(self._wrap(_BOLD + _CYAN, f"==> {name}"))

    def info(self, msg: str) -> None:
        self._write(f"  {msg}")

    def detail(self, msg: str) -> None:
        if self.verbose:
            self._write(self._wrap(_DIM, f"    {msg}"))

    def success(self, msg: str) -> None:
        self._write(self._wrap(_GREEN, f"  ✓ {msg}"))

    def warn(self, msg: str) -> None:
        self._write(self._wrap(_YELLOW, f"  ! {msg}"))

    def error(self, msg: str) -> None:
        self._write(self._wrap(_RED, f"  ✗ {msg}"))
