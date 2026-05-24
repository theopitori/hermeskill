"""Smoke test for the M1.7 demo agent.

Verifies the demo builds a valid graph, the 5-line watch() integration works,
and tool calls fire the StasisCallbackHandler (so tool_call events get queued
in the WatcherState).

Uses httpx.MockTransport so no live control plane is required.
"""

import json as _json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from stasis_agent import watch
from stasis_agent.client import StasisClient
from stasis_agent.watcher import BackgroundWorker, all_watchers

# Make the demo importable as a module (it lives outside any package).
DEMO_PARENT = Path(__file__).resolve().parents[3] / "demo"
sys.path.insert(0, str(DEMO_PARENT.parent))


def _mock_client(calls: list[dict[str, Any]]) -> StasisClient:
    agent_id = str(uuid4())
    now = datetime.now(UTC).isoformat()

    def handler(req: httpx.Request) -> httpx.Response:
        body = _json.loads(req.content) if req.content else None
        calls.append({"path": req.url.path, "body": body})
        if req.method == "POST" and req.url.path == "/agents":
            return httpx.Response(
                201,
                json={
                    "agent_id": agent_id,
                    "policy_name": body["policy_name"],
                    "registered_at": now,
                },
            )
        if req.url.path.endswith("/heartbeat"):
            return httpx.Response(200, json={"received_at": now, "active_grants": []})
        if req.url.path.endswith("/events"):
            return httpx.Response(202, json={"accepted": len(body["events"])})
        return httpx.Response(404, json={"detail": "?"})

    return StasisClient(
        base_url="http://test",
        api_key="sk_test",
        transport=httpx.MockTransport(handler),
    )


def test_build_graph_returns_compiled_graph() -> None:
    from demo.coding_agent.agent import build_graph

    g = build_graph()
    # Compiled graphs are LangChain Runnables — they expose `with_config`.
    assert hasattr(g, "with_config")
    assert hasattr(g, "ainvoke")


@pytest.mark.asyncio
async def test_demo_agent_runs_end_to_end_under_mock() -> None:
    from demo.coding_agent.agent import build_graph

    calls: list[dict[str, Any]] = []
    client = _mock_client(calls)
    graph = build_graph()

    try:
        watched = await watch(graph, name="demo-test", policy="coding-default", client=client)
        result = await watched.ainvoke({"task": "fix it"})

        # Customer-visible result
        assert result.get("edits_made") == 1
        assert result.get("files_read") == ["dummy.py"]

        # Tool events were queued on the WatcherState
        state = all_watchers()[0]
        events = state.drain_events()
        tool_calls = [e for e in events if e.type.value == "tool_call"]
        assert any(e.payload.get("tool") == "read_file" for e in tool_calls)
        assert any(e.payload.get("tool") == "write_file" for e in tool_calls)
    finally:
        await BackgroundWorker.stop()
        await client.aclose()


def test_dotenv_loader_skips_missing_file(tmp_path: Path) -> None:
    """The .env loader must no-op when the file doesn't exist."""
    from demo.coding_agent.agent import _load_dotenv

    _load_dotenv(tmp_path / "definitely-not-there.env")  # must not raise


def test_dotenv_loader_parses_and_skips_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from demo.coding_agent.agent import _load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n"
        "\n"
        "STASIS_TEST_FOO=bar\n"
        "STASIS_TEST_BAZ=qux=with=equals\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("STASIS_TEST_FOO", raising=False)
    monkeypatch.delenv("STASIS_TEST_BAZ", raising=False)

    _load_dotenv(env_file)

    import os

    assert os.environ["STASIS_TEST_FOO"] == "bar"
    # Right-hand side keeps subsequent '=' signs intact
    assert os.environ["STASIS_TEST_BAZ"] == "qux=with=equals"
