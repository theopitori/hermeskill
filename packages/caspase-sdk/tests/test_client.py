"""Unit tests for caspase.client.CaspaseClient.

Uses httpx.MockTransport so no live control plane is needed. End-to-end
integration against a real server lands in M1.9.

NOTE: no `from __future__ import annotations` — Pydantic v2 model rebuild
under PEP 563 + locally-imported types can lose the metadata FastAPI/Pydantic
need to introspect schemas. We've hit this elsewhere in the project; keep
test modules without the future import.
"""

import json as _json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
from caspase.client import (
    AuthError,
    CaspaseClient,
    ConflictError,
    NotFoundError,
    ServerError,
    TransportError,
)
from caspase.types import EventIn, EventType


def _client(handler: Any) -> CaspaseClient:
    return CaspaseClient(
        base_url="http://test",
        api_key="sk_test_xxx",
        transport=httpx.MockTransport(handler),
    )


def _ok(payload: dict[str, Any], status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=_json.dumps(payload).encode())


# --- happy paths ----------------------------------------------------------


@pytest.mark.asyncio
async def test_register_agent_round_trip() -> None:
    captured: dict[str, Any] = {}
    agent_id = uuid4()
    now = datetime.now(UTC).isoformat()

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["url"] = str(req.url)
        captured["auth"] = req.headers.get("authorization")
        captured["body"] = _json.loads(req.content)
        return _ok(
            {"agent_id": str(agent_id), "policy_name": "coding-default", "registered_at": now},
            status=201,
        )

    async with _client(handler) as c:
        out = await c.register_agent("bot", "coding-default", metadata={"k": "v"})

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/agents")
    assert captured["auth"] == "Bearer sk_test_xxx"
    assert captured["body"]["name"] == "bot"
    assert captured["body"]["policy_name"] == "coding-default"
    assert captured["body"]["metadata"] == {"k": "v"}
    assert out.agent_id == agent_id
    assert out.policy_name == "coding-default"


@pytest.mark.asyncio
async def test_heartbeat_round_trip() -> None:
    agent_id = uuid4()
    now = datetime.now(UTC).isoformat()
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(req.content)
        return _ok({"received_at": now, "active_grants": []})

    async with _client(handler) as c:
        out = await c.heartbeat(agent_id, uptime_seconds=42.5)

    assert captured["body"] == {"uptime_seconds": 42.5}
    assert out.active_grants == []


@pytest.mark.asyncio
async def test_post_events_batches() -> None:
    agent_id = uuid4()
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(req.content)
        return _ok({"accepted": 2}, status=202)

    async with _client(handler) as c:
        n = await c.post_events(
            agent_id,
            [
                EventIn(type=EventType.TOOL_CALL, payload={"tool": "x"}),
                EventIn(type=EventType.LLM_CALL, payload={"model": "y"}),
            ],
        )

    assert n == 2
    assert len(captured["body"]["events"]) == 2
    assert captured["body"]["events"][0]["type"] == "tool_call"


@pytest.mark.asyncio
async def test_list_events_passes_after_id_cursor() -> None:
    agent_id = uuid4()
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["params"] = dict(req.url.params)
        return _ok({"events": [], "next_before_id": None, "last_id": None})

    async with _client(handler) as c:
        await c.list_events(agent_id, limit=50, after_id=99)

    assert captured["params"] == {"limit": "50", "after_id": "99"}


@pytest.mark.asyncio
async def test_list_events_descending_default() -> None:
    agent_id = uuid4()
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["params"] = dict(req.url.params)
        return _ok({"events": [], "next_before_id": None, "last_id": None})

    async with _client(handler) as c:
        await c.list_events(agent_id)

    assert "after_id" not in captured["params"]
    assert "before_id" not in captured["params"]


# --- error mapping --------------------------------------------------------


@pytest.mark.asyncio
async def test_401_raises_auth_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return _ok({"detail": "bad key"}, status=401)

    async with _client(handler) as c:
        with pytest.raises(AuthError, match="bad key"):
            await c.register_agent("x", "y")


@pytest.mark.asyncio
async def test_404_raises_not_found() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return _ok({"detail": "agent not found"}, status=404)

    async with _client(handler) as c:
        with pytest.raises(NotFoundError):
            await c.get_agent(uuid4())


@pytest.mark.asyncio
async def test_409_raises_conflict() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return _ok({"detail": "already dying"}, status=409)

    async with _client(handler) as c:
        with pytest.raises(ConflictError):
            await c.heartbeat(uuid4(), 1.0)


@pytest.mark.asyncio
async def test_500_raises_server_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return _ok({"detail": "boom"}, status=500)

    async with _client(handler) as c:
        with pytest.raises(ServerError, match="500"):
            await c.list_agents()


@pytest.mark.asyncio
async def test_network_error_raises_transport_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with _client(handler) as c:
        with pytest.raises(TransportError, match="ConnectError"):
            await c.list_agents()


# --- config plumbing ------------------------------------------------------


@pytest.mark.asyncio
async def test_from_config_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CASPASE_API_KEY", raising=False)
    monkeypatch.delenv("CASPASE_BASE_URL", raising=False)
    # Point config loader at a non-existent file so the env-only fallback applies.
    monkeypatch.setattr(
        "caspase.config.CONFIG_PATH",
        type("P", (), {"exists": staticmethod(lambda: False)})(),
    )
    # Disable the .env auto-loader; otherwise the test picks up the project's
    # real .env (which has CASPASE_API_KEY set) and the AuthError never fires.
    monkeypatch.setattr(
        "caspase.config._load_dotenv_into_environ", lambda *a, **kw: None
    )
    with pytest.raises(AuthError, match="CASPASE_API_KEY"):
        CaspaseClient.from_config()
