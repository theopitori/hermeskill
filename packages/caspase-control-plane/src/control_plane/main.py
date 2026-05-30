"""FastAPI app entrypoint.

`/healthz` exercises the DB pool (TODO #11) so customers / load balancers see
a real ready signal, not a static 200.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import uvicorn
from fastapi import FastAPI, Response, status
from sqlalchemy import text

from control_plane import __version__
from control_plane.api import agents as agents_router
from control_plane.api import calibration as calibration_router
from control_plane.api import events as events_router
from control_plane.api import feedback as feedback_router
from control_plane.api import grants as grants_router
from control_plane.api import heartbeats as heartbeats_router
from control_plane.api import kill_events as kill_events_router
from control_plane.api import manual_kill as manual_kill_router
from control_plane.db.session import engine
from control_plane.settings import settings


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Background tasks (heartbeat-stale sweeper, grant-expiry sweeper) get
    # registered here from M2 onward.
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Caspase Control Plane",
        version=__version__,
        lifespan=lifespan,
    )

    app.include_router(agents_router.router)
    app.include_router(heartbeats_router.router)
    app.include_router(events_router.router)
    # M2.5 — kill_events has nested (POST/list under /agents/{id}) and
    # top-level (GET single by id). M4 manual-kill routes live in manual_kill.
    app.include_router(kill_events_router.router)
    app.include_router(kill_events_router.top_router)
    app.include_router(manual_kill_router.router)
    app.include_router(manual_kill_router.kills_router)
    # M3 — unauthenticated one-click feedback. Mounted last so its routes
    # are unambiguous even though the prefix doesn't collide.
    app.include_router(feedback_router.router)
    # M5 — apoptosis-proofing grants. Nested under /agents for create+list
    # plus a top-level /grants/* for revoke.
    app.include_router(grants_router.router)
    app.include_router(grants_router.top_router)
    # Phase 4 — advisory, read-only policy calibration from feedback labels.
    app.include_router(calibration_router.router)

    @app.get("/healthz")
    async def healthz(response: Response) -> dict[str, str]:
        """Probe DB connectivity with SELECT 1.

        Returns 200 + `db: ok` on success, 503 + `db: error` if the engine
        can't connect. Static `version` field for compat with the M0 contract.
        """
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            db_status = "ok"
        except Exception as exc:  # health probe must catch everything
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {
                "status": "degraded",
                "version": __version__,
                "db": "error",
                "db_error": type(exc).__name__,
                "checked_at": datetime.now(UTC).isoformat(),
            }
        return {
            "status": "ok",
            "version": __version__,
            "db": db_status,
            "checked_at": datetime.now(UTC).isoformat(),
        }

    return app


app = create_app()


def run() -> None:
    """Console-script entry: `caspase-control-plane`."""
    uvicorn.run(
        "control_plane.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.debug,
    )


if __name__ == "__main__":
    run()
