"""Smoke tests for control-plane health + auth.

NOTE: this module deliberately omits `from __future__ import annotations`.
FastAPI's introspection of `Annotated[...]` dependency types via
`get_type_hints()` needs the referenced classes resolvable from module
globals; PEP 563 stringified annotations don't reliably round-trip with
locally-imported types.
"""

from typing import Annotated

import pytest
from conftest import DEV_DEVELOPER_KEY
from control_plane.auth import Principal, require_principal
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_healthz_ok(client: AsyncClient) -> None:
    """When DB is reachable, healthz returns 200 with db: ok."""
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert "version" in body
    assert "checked_at" in body


@pytest.mark.asyncio
async def test_auth_paths() -> None:
    """Auth dependency returns 401 on missing/bad keys, 200 on dev key."""
    test_app = FastAPI()

    @test_app.get("/whoami")
    async def whoami(
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> dict[str, str]:
        return {"role": principal.role.value}

    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://t") as c:
        r = await c.get("/whoami")
        assert r.status_code == 401, f"no header: {r.text}"

        r = await c.get("/whoami", headers={"Authorization": "Basic foo"})
        assert r.status_code == 401, f"bad scheme: {r.text}"

        r = await c.get("/whoami", headers={"Authorization": "Bearer sk_not_a_real_key"})
        assert r.status_code == 401, f"bogus token: {r.text}"

        r = await c.get("/whoami", headers={"Authorization": f"Bearer {DEV_DEVELOPER_KEY}"})
        assert r.status_code == 200, f"dev key rejected: {r.text}"
        assert r.json() == {"role": "developer"}
