"""Module-level supervised targets for ProcessSupervisor tests.

These MUST live in an importable module (not inside a test function or a
pytest-renamed module) because the ``spawn`` start method re-imports the
target's module by name in the child interpreter. Keeping them here — a plain
top-level module on the tests sys.path, like ``_keys`` — guarantees they pickle
and re-import cleanly.
"""

from __future__ import annotations

import signal
import sys
import time
from typing import Any


def returns_quickly(heartbeat: Any) -> None:
    """Beats once and exits — the clean-completion path."""
    heartbeat.beat()


def wedged_ignores_sigterm(heartbeat: Any) -> None:
    """Beats once, ignores SIGTERM (POSIX), then spins forever CPU-bound.

    This is the case L1/L2 provably cannot kill: no awaits, and SIGTERM is
    swallowed — only SIGKILL ends it. On Windows there is no catchable SIGTERM,
    so the SIG_IGN install is skipped and ``terminate()`` already hard-kills.
    """
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    heartbeat.beat()
    while True:
        pass


def cooperative_on_sigterm(heartbeat: Any) -> None:
    """Beats once, then exits cleanly on SIGTERM — the no-escalation path.

    POSIX only (Windows has no catchable SIGTERM). The supervisor's
    ``terminate()`` should suffice; no SIGKILL needed.
    """

    def _bye(signum: int, frame: Any) -> None:
        sys.exit(0)

    signal.signal(signal.SIGTERM, _bye)
    heartbeat.beat()
    while True:
        time.sleep(0.01)


def wedged_no_heartbeat(heartbeat: Any) -> None:
    """Never beats and spins forever — trips heartbeat-loss immediately."""
    while True:
        pass
