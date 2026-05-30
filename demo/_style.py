"""Tiny ANSI styling + console prep shared by the demo entrypoints.

No dependency on rich/colorama — just enough to make the terminal narrative
readable and the GIF look good, with graceful degradation when stdout isn't a
TTY or NO_COLOR is set.
"""

from __future__ import annotations

import contextlib
import os
import sys

_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

RULE = "─" * 60


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def dim(t: str) -> str:
    return _c("2", t)


def bold(t: str) -> str:
    return _c("1", t)


def red(t: str) -> str:
    return _c("31", t)


def green(t: str) -> str:
    return _c("32", t)


def yellow(t: str) -> str:
    return _c("33", t)


def cyan(t: str) -> str:
    return _c("36", t)


def prepare_console() -> None:
    """Make stdout UTF-8 (for box-drawing glyphs) and enable ANSI on Windows.

    Windows consoles default to a legacy code page (cp1252) that can't encode
    the demo's Unicode framing, and legacy consoles need ANSI processing turned
    on explicitly. Both are best-effort — the demo still runs without them.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(Exception):
                reconfigure(encoding="utf-8", errors="replace")
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            import ctypes

            # getattr keeps this type-clean off-Windows (ctypes.windll is
            # Windows-only) without a platform-specific type: ignore.
            kernel32 = getattr(ctypes, "windll").kernel32  # noqa: B009
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
