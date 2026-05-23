"""HTTP client to the Stasis control plane.

Async-first (`httpx.AsyncClient` under the hood). The CLI and the Watcher both
talk to the control plane exclusively through this class — no raw `httpx`
calls outside this module. That gives us one place to handle auth, retries,
and error mapping.

Errors are mapped to typed exceptions: `AuthError` (401), `NotFoundError`
(404), `ConflictError` (409 — used in M4 for already-dying agents),
`ServerError` (5xx). Network/timeout failures raise `TransportError`. Callers
should treat anything else as a bug.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import httpx

from stasis_agent.config import SDKConfig
from stasis_agent.exceptions import StasisError
from stasis_agent.types import (
    AgentRegistrationIn,
    AgentRegistrationOut,
    AgentSummary,
    EventBatchIn,
    EventIn,
    EventPage,
    HeartbeatIn,
    HeartbeatOut,
    KillEventIn,
    KillEventOut,
    PendingKillOut,
    TerminateAgentIn,
)

DEFAULT_TIMEOUT_SECONDS = 30.0


# --- exceptions -----------------------------------------------------------


class TransportError(StasisError):
    """Network/timeout failure talking to the control plane."""


class AuthError(StasisError):
    """401 from the control plane — bad/missing/revoked API key."""


class NotFoundError(StasisError):
    """404 from the control plane — agent_id unknown or owned by another customer."""


class ConflictError(StasisError):
    """409 from the control plane — e.g. agent already has a kill in flight (M4)."""


class ServerError(StasisError):
    """5xx from the control plane."""


# --- client ---------------------------------------------------------------


class StasisClient:
    """Async HTTP client for the Stasis control plane.

    Two ways to use it:

        # Long-lived, share across an agent's lifetime
        client = StasisClient.from_config()
        try:
            ...
        finally:
            await client.aclose()

        # Or as an async context manager
        async with StasisClient.from_config() as client:
            await client.register_agent(...)
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    @classmethod
    def from_config(
        cls,
        config: SDKConfig | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> StasisClient:
        cfg = config or SDKConfig.load()
        if not cfg.api_key:
            raise AuthError(
                "STASIS_API_KEY not set. Put it in your environment or ~/.stasis/config.toml."
            )
        return cls(base_url=cfg.base_url, api_key=cfg.api_key, transport=transport)

    async def __aenter__(self) -> StasisClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- agents -----------------------------------------------------------

    async def register_agent(
        self,
        name: str,
        policy_name: str,
        metadata: dict[str, Any] | None = None,
    ) -> AgentRegistrationOut:
        body = AgentRegistrationIn(
            name=name, policy_name=policy_name, metadata=metadata or {}
        ).model_dump()
        data = await self._request("POST", "/agents", json=body)
        return AgentRegistrationOut.model_validate(data)

    async def list_agents(self) -> list[AgentSummary]:
        data = await self._request("GET", "/agents")
        return [AgentSummary.model_validate(a) for a in data]

    async def get_agent(self, agent_id: UUID | str) -> AgentSummary:
        data = await self._request("GET", f"/agents/{agent_id}")
        return AgentSummary.model_validate(data)

    # --- heartbeats -------------------------------------------------------

    async def heartbeat(self, agent_id: UUID | str, uptime_seconds: float) -> HeartbeatOut:
        body = HeartbeatIn(uptime_seconds=uptime_seconds).model_dump()
        data = await self._request("POST", f"/agents/{agent_id}/heartbeat", json=body)
        return HeartbeatOut.model_validate(data)

    # --- events -----------------------------------------------------------

    async def post_events(
        self,
        agent_id: UUID | str,
        events: list[EventIn],
    ) -> int:
        """Returns the number of events accepted by the server."""
        body = EventBatchIn(events=events).model_dump(mode="json")
        data = await self._request("POST", f"/agents/{agent_id}/events", json=body)
        return int(data["accepted"])

    # --- kill events (M2.5) -----------------------------------------------

    async def post_kill_event(
        self,
        agent_id: UUID | str,
        kill_event: KillEventIn,
    ) -> KillEventOut | int:
        """Post a death certificate for the given agent.

        Returns the assigned `KillEventOut` on 201. On 409 (an active
        kill_event already exists for this agent — racing symptom-vs-
        manual kill), returns the existing kill_event id as a plain
        int. Callers in the auto-kill path should treat 409 as 'already
        dying, fine' and not retry.

        Other errors (5xx, network) raise via `_request` — the wrapper
        in `_watch.py` catches these and logs them so a forensic-post
        failure never swallows the underlying StasisTerminated.
        """
        body = kill_event.model_dump(mode="json")
        try:
            data = await self._request(
                "POST", f"/agents/{agent_id}/kill_events", json=body
            )
        except ConflictError as exc:
            # The server returned 409 with `existing_kill_event_id` in
            # the body — extract it so the caller doesn't lose the
            # winner's id.
            return _extract_existing_kill_event_id(exc) or -1
        return KillEventOut.model_validate(data)

    async def get_kill_event(self, kill_event_id: int) -> KillEventOut:
        data = await self._request("GET", f"/kill_events/{kill_event_id}")
        return KillEventOut.model_validate(data)

    async def list_kill_events(self, agent_id: UUID | str) -> list[KillEventOut]:
        data = await self._request("GET", f"/agents/{agent_id}/kill_events")
        return [KillEventOut.model_validate(k) for k in data]

    # --- manual kill (M4) -------------------------------------------------

    async def terminate_agent(
        self,
        agent_id: UUID | str,
        reason: str,
    ) -> KillEventOut | int:
        """Operator-issued kill. Inserts a kill_events row with
        `status=INITIATED, trigger_type=MANUAL`; the SDK's poller picks
        it up and starts cooperative shutdown.

        Returns the new `KillEventOut` on 201, or an existing
        kill_event id (int) on 409 (someone else already killed this
        agent, or it's already terminated).

        Requires an operator-role API key. 403 → bubbles up as the
        catch-all `StasisError` rather than `AuthError`; operators
        should fix their key, not retry.
        """
        body = TerminateAgentIn(reason=reason).model_dump()
        try:
            data = await self._request(
                "POST", f"/agents/{agent_id}/terminate", json=body
            )
        except ConflictError as exc:
            return _extract_existing_kill_event_id(exc) or -1
        return KillEventOut.model_validate(data)

    async def list_pending_kills(self) -> list[PendingKillOut]:
        """Poll for manual kills awaiting cooperative action.

        One batch round-trip across all the caller's agents — keeps the
        per-process invariant in TODO #8 (one poll task per Python
        process, regardless of watcher count).
        """
        data = await self._request("GET", "/kills/pending")
        return [PendingKillOut.model_validate(p) for p in data]

    # --- events (continued) -----------------------------------------------

    async def list_events(
        self,
        agent_id: UUID | str,
        *,
        limit: int = 100,
        before_id: int | None = None,
        after_id: int | None = None,
    ) -> EventPage:
        params: dict[str, str | int] = {"limit": limit}
        if before_id is not None:
            params["before_id"] = before_id
        if after_id is not None:
            params["after_id"] = after_id
        data = await self._request("GET", f"/agents/{agent_id}/events", params=params)
        return EventPage.model_validate(data)

    # --- internal ---------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = await self._http.request(method, path, json=json, params=params)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise TransportError(f"{method} {path}: {type(exc).__name__}: {exc}") from exc

        if 200 <= response.status_code < 300:
            if response.status_code == 204 or not response.content:
                return None
            return response.json()

        # Error mapping
        detail = _safe_detail(response)
        match response.status_code:
            case 401:
                raise AuthError(detail)
            case 403:
                # Fold 403 into AuthError so the CLI's existing handler
                # surfaces a clean "auth error" message. Promote to a
                # dedicated ForbiddenError if precision starts to matter
                # (e.g. developer keys hitting operator-only routes
                # often enough to deserve distinct messaging).
                raise AuthError(detail)
            case 404:
                raise NotFoundError(detail)
            case 409:
                raise ConflictError(detail)
            case code if 500 <= code < 600:
                raise ServerError(f"{code}: {detail}")
            case _:
                raise StasisError(f"{response.status_code}: {detail}")


def _safe_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict) and "detail" in body:
            return str(body["detail"])
        return str(body)
    except (ValueError, httpx.DecodingError):
        return response.text or response.reason_phrase


def _extract_existing_kill_event_id(exc: ConflictError) -> int | None:
    """Pull `existing_kill_event_id` out of a 409 body.

    The server returns 409 with a nested dict body:
        {"detail": {"detail": "...", "existing_kill_event_id": 42}}

    We rendered the whole nested dict into the exception's message via
    `_safe_detail`. Best-effort parse — if the shape ever changes the
    caller still gets None and treats the kill_event as unknown.
    """
    msg = str(exc)
    # Cheapest possible extraction; the body is a small dict. Avoid eval.
    try:
        import ast

        parsed = ast.literal_eval(msg)
        if isinstance(parsed, dict):
            v = parsed.get("existing_kill_event_id")
            if isinstance(v, int):
                return v
    except (ValueError, SyntaxError):
        pass
    return None


@asynccontextmanager
async def client_from_env() -> AsyncIterator[StasisClient]:
    """Sugar for one-off scripts: `async with client_from_env() as c: ...`."""
    client = StasisClient.from_config()
    try:
        yield client
    finally:
        await client.aclose()
