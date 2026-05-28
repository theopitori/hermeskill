"""Run the in-process SQLite control plane until Ctrl+C.

Used during the Stage-2 Hermes integration demo. Boots
``demo.coding_agent._bootstrap.start_control_plane()`` on
``http://localhost:8000`` (SQLite-backed, no Postgres required) and then
blocks so the death certificate URL printed by the killed Hermes session
stays reachable while you click it in a browser.

Usage::

    uv run python -m demo.coding_agent._run_control_plane

Stop with Ctrl+C. The on-disk SQLite file is recreated next run, so
data is ephemeral by design — this is a demo harness, not a real server.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

from demo.coding_agent._bootstrap import start_control_plane

logger = logging.getLogger("caspase.demo.control_plane")


async def _main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    logger.info("booting in-process control plane on http://localhost:8000")
    server, serve_task = await start_control_plane()
    logger.info("control plane up; press Ctrl+C to stop")

    # Wait for either Ctrl+C (SIGINT) or the uvicorn task ending on its own.
    stop_event = asyncio.Event()

    def _on_signal() -> None:
        logger.info("stop requested; shutting down")
        stop_event.set()

    loop = asyncio.get_running_loop()
    # add_signal_handler is POSIX-only; on Windows we fall back to a KeyboardInterrupt
    # catch in the outer asyncio.run() call.
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, _on_signal)

    try:
        # Whichever happens first: a stop signal, or the server task ending.
        done, _pending = await asyncio.wait(
            {asyncio.create_task(stop_event.wait()), serve_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in done:
            exc = t.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                logger.exception("control plane task failed", exc_info=exc)
                return 1
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(serve_task, timeout=5.0)
        logger.info("control plane stopped")

    return 0


def main() -> None:
    try:
        sys.exit(asyncio.run(_main()))
    except KeyboardInterrupt:
        # Windows reaches here; the asyncio loop bubbles the interrupt up.
        sys.exit(0)


if __name__ == "__main__":
    main()
