"""Tests for the `caspase` CLI commands (fleet, logs).

Patches CaspaseClient.from_config to return a mock-transport-backed client so
no live server is needed.
"""

import json as _json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import caspase.cli as cli_mod
import httpx
import pytest
from caspase.cli import app
from caspase.client import CaspaseClient
from rich.console import Console
from typer.testing import CliRunner

runner = CliRunner()


def _client_with(handler: Any) -> CaspaseClient:
    return CaspaseClient(
        base_url="http://test",
        api_key="sk_test",
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture(autouse=True)
def _patch_from_config(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Each test installs its own handler by setting `_handler` on the module.

    `_handler` is a test-only injection point that does not exist on the real
    module; the # type: ignore[attr-defined] suppressions below are intentional.

    Also forces a wide Rich console so table cells don't get truncated by
    CliRunner's narrow default terminal width — otherwise assertions on cell
    contents like "coding-default" fail because Rich renders "cod…".
    """
    cli_mod._handler = None  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_mod, "console", Console(width=200))

    def fake(*args: Any, **kwargs: Any) -> CaspaseClient:
        handler = cli_mod._handler  # type: ignore[attr-defined]
        if handler is None:

            def default(_req: httpx.Request) -> httpx.Response:
                return httpx.Response(500, json={"detail": "no handler set"})

            handler = default
        return _client_with(handler)

    monkeypatch.setattr(cli_mod.CaspaseClient, "from_config", staticmethod(fake))
    yield
    cli_mod._handler = None  # type: ignore[attr-defined]


def _set_handler(handler: Any) -> None:
    cli_mod._handler = handler  # type: ignore[attr-defined]


# --- fleet --------------------------------------------------------------


def test_fleet_renders_table_of_agents() -> None:
    now = datetime.now(UTC).isoformat()
    agents = [
        {
            "id": str(uuid4()),
            "name": "alpha",
            "policy_name": "coding-default",
            "status": "running",
            "registered_at": now,
            "last_heartbeat_at": now,
            "terminated_at": None,
        },
        {
            "id": str(uuid4()),
            "name": "beta",
            "policy_name": "strict",
            "status": "terminated",
            "registered_at": now,
            "last_heartbeat_at": None,
            "terminated_at": now,
        },
    ]

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=agents)

    _set_handler(handler)
    # --all so the terminated "beta" shows (default fleet view hides terminal).
    result = runner.invoke(app, ["fleet", "--all"])
    assert result.exit_code == 0, result.stdout
    assert "alpha" in result.stdout
    assert "beta" in result.stdout
    assert "coding-default" in result.stdout
    assert "running" in result.stdout
    assert "terminated" in result.stdout


def test_fleet_empty_message() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    _set_handler(handler)
    result = runner.invoke(app, ["fleet"])
    assert result.exit_code == 0
    assert "no agents" in result.stdout


def test_fleet_auth_error_exits_2() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "bad key"})

    _set_handler(handler)
    result = runner.invoke(app, ["fleet"])
    assert result.exit_code == 2
    # auth-error message goes to stderr; CliRunner captures both via .output
    assert "auth error" in (result.stderr or "") + result.output


def test_fleet_transport_error_exits_5() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _set_handler(handler)
    result = runner.invoke(app, ["fleet"])
    assert result.exit_code == 5


# --- logs ---------------------------------------------------------------


def test_logs_renders_recent_events_oldest_first() -> None:
    aid = str(uuid4())
    now = datetime.now(UTC).isoformat()
    events_page = {
        # Descending order from the server (newest first)
        "events": [
            {
                "id": 3,
                "agent_id": aid,
                "type": "tool_call",
                "payload": {"tool": "write_file"},
                "created_at": now,
            },
            {
                "id": 2,
                "agent_id": aid,
                "type": "tool_call",
                "payload": {"tool": "read_file"},
                "created_at": now,
            },
            {
                "id": 1,
                "agent_id": aid,
                "type": "lifecycle",
                "payload": {"phase": "registered"},
                "created_at": now,
            },
        ],
        "next_before_id": None,
        "last_id": 3,
    }

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=events_page)

    _set_handler(handler)
    result = runner.invoke(app, ["logs", aid])
    assert result.exit_code == 0, result.stdout

    # Verify oldest-first ordering in the printed output
    out = result.stdout
    idx_registered = out.find("registered")
    idx_read = out.find("read_file")
    idx_write = out.find("write_file")
    assert idx_registered < idx_read < idx_write, f"order wrong:\n{out}"


def test_logs_unknown_agent_exits_4() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "agent not found"})

    _set_handler(handler)
    result = runner.invoke(app, ["logs", str(uuid4())])
    assert result.exit_code == 4


def test_logs_llm_event_shows_cost() -> None:
    aid = str(uuid4())
    now = datetime.now(UTC).isoformat()
    page = {
        "events": [
            {
                "id": 1,
                "agent_id": aid,
                "type": "llm_call",
                "payload": {
                    "model": "claude-haiku-4-5",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cost_usd": 0.000350,
                },
                "created_at": now,
            }
        ],
        "next_before_id": None,
        "last_id": 1,
    }

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page)

    _set_handler(handler)
    result = runner.invoke(app, ["logs", aid])
    assert result.exit_code == 0
    assert "claude-haiku-4-5" in result.stdout
    assert "$0.0003" in result.stdout  # 4-decimal cost format


def test_logs_follow_polls_until_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    """--follow polls with after_id; we simulate one poll then KeyboardInterrupt."""
    aid = str(uuid4())
    now = datetime.now(UTC).isoformat()
    call_count = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        params = dict(req.url.params)
        # First call: initial page, returns one event.
        if "after_id" not in params:
            return httpx.Response(
                200,
                json={
                    "events": [
                        {
                            "id": 1,
                            "agent_id": aid,
                            "type": "lifecycle",
                            "payload": {"phase": "registered"},
                            "created_at": now,
                        }
                    ],
                    "next_before_id": None,
                    "last_id": 1,
                },
            )
        # Second call (poll): one new event, then we'll signal stop.
        return httpx.Response(
            200,
            json={
                "events": [
                    {
                        "id": 2,
                        "agent_id": aid,
                        "type": "tool_call",
                        "payload": {"tool": "ping"},
                        "created_at": now,
                    }
                ],
                "next_before_id": None,
                "last_id": 2,
            },
        )

    _set_handler(handler)

    # Force the sleep to raise KeyboardInterrupt on the second iteration
    # (first iteration of the while loop, after the initial page).
    real_sleep = __import__("asyncio").sleep

    async def fake_sleep(_seconds: float) -> None:
        # Allow one poll cycle, then bail.
        if call_count["n"] >= 2:
            raise KeyboardInterrupt
        await real_sleep(0)

    monkeypatch.setattr("caspase.cli.asyncio.sleep", fake_sleep)

    result = runner.invoke(app, ["logs", aid, "--follow", "--interval", "0.001"])
    assert result.exit_code == 0, result.stdout
    assert "registered" in result.stdout
    assert "ping" in result.stdout


# --- version + placeholders ---------------------------------------------


def test_version_flag_prints_version_only() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    # Output should be a single line with the version
    assert result.stdout.strip().count("\n") == 0


def _kill_handlers(aid: str) -> Any:
    """Return a stateful handler that walks an agent through the
    manual-kill lifecycle: running → dying → terminated.

    First GET /agents/{id} returns running; POST /terminate returns the
    new kill_event (201); subsequent GETs return dying once, then
    terminated. The CLI should print the staged progress and exit 0.
    """
    now = datetime.now(UTC).isoformat()
    states = ["running", "dying", "terminated"]
    cursor = {"i": 0}
    kill_event = {
        "id": 7,
        "agent_id": aid,
        "trigger_type": "manual",
        "trigger_reason": "manual kill",
        "status": "initiated",
        "triggered_at": now,
        "terminated_at": None,
        "death_certificate": None,
        "shutdown_log": [],
        "operator_reason": "deploy rollback",
        "created_at": now,
    }

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == f"/agents/{aid}":
            i = min(cursor["i"], len(states) - 1)
            cursor["i"] += 1
            return httpx.Response(
                200,
                json={
                    "id": aid,
                    "name": "alpha",
                    "policy_name": "coding-default",
                    "status": states[i],
                    "registered_at": now,
                    "last_heartbeat_at": now,
                    "terminated_at": now if states[i] == "terminated" else None,
                },
            )
        if req.method == "POST" and path == f"/agents/{aid}/terminate":
            return httpx.Response(201, json=kill_event)
        return httpx.Response(500, json={"detail": f"unhandled {req.method} {path}"})

    return handler


def test_kill_command_walks_staged_progress_to_terminated() -> None:
    aid = str(uuid4())
    _set_handler(_kill_handlers(aid))
    result = runner.invoke(
        app,
        ["kill", aid, "--reason", "deploy rollback", "--poll-interval", "0.1"],
    )
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    out = result.stdout
    # Worst-case latency banner
    assert "worst-case" in out
    # Issue confirmation
    assert "kill issued" in out
    # Cooperative announcement (fires when status hits DYING)
    assert "cooperative shutdown" in out
    # Final confirmation
    assert "confirmed dead" in out


def test_kill_command_409_treated_as_already_dying() -> None:
    aid = str(uuid4())
    now = datetime.now(UTC).isoformat()
    states = ["dying", "terminated"]
    cursor = {"i": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == f"/agents/{aid}":
            i = min(cursor["i"], len(states) - 1)
            cursor["i"] += 1
            return httpx.Response(
                200,
                json={
                    "id": aid,
                    "name": "alpha",
                    "policy_name": "coding-default",
                    "status": states[i],
                    "registered_at": now,
                    "last_heartbeat_at": now,
                    "terminated_at": now if states[i] == "terminated" else None,
                },
            )
        if req.method == "POST" and path == f"/agents/{aid}/terminate":
            return httpx.Response(
                409,
                json={
                    "detail": {
                        "detail": "agent already has an active kill_event",
                        "existing_kill_event_id": 42,
                    }
                },
            )
        return httpx.Response(500, json={"detail": "unhandled"})

    _set_handler(handler)
    result = runner.invoke(
        app, ["kill", aid, "--reason", "x", "--poll-interval", "0.1"]
    )
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    out = result.stdout
    assert "already in flight" in out
    assert "42" in out


def test_kill_command_unknown_agent_exits_4() -> None:
    aid = str(uuid4())

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "agent not found"})

    _set_handler(handler)
    result = runner.invoke(app, ["kill", aid, "--reason", "x"])
    assert result.exit_code == 4


def test_kill_command_developer_key_403_treated_as_auth_error() -> None:
    aid = str(uuid4())
    now = datetime.now(UTC).isoformat()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": aid,
                    "name": "alpha",
                    "policy_name": "coding-default",
                    "status": "running",
                    "registered_at": now,
                    "last_heartbeat_at": now,
                    "terminated_at": None,
                },
            )
        return httpx.Response(403, json={"detail": "operator role required"})

    _set_handler(handler)
    result = runner.invoke(app, ["kill", aid, "--reason", "x"])
    # _request maps 403 → AuthError, which the CLI handles → exit 2.
    assert result.exit_code == 2


def test_worst_case_latency_known_policy() -> None:
    from caspase.cli import _worst_case_latency

    # coding-default: grace=10, verification=30, plus DEFAULT_KILL_POLL_INTERVAL=3
    assert _worst_case_latency("coding-default") == pytest.approx(43.0)


def test_worst_case_latency_unknown_policy_falls_back() -> None:
    from caspase.cli import _worst_case_latency

    # Conservative ~43s fallback for unknown policies.
    assert _worst_case_latency("custom-not-shipped") == pytest.approx(43.0)


def test_watch_kill_times_out_with_exit_6(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zombie path unit-tested directly against `_watch_kill` to avoid
    waiting on the CLI's 60s minimum timeout floor."""
    import asyncio as _asyncio

    import typer as _typer
    from caspase.cli import _watch_kill

    aid = str(uuid4())
    now = datetime.now(UTC).isoformat()

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": aid,
                "name": "alpha",
                "policy_name": "coding-default",
                "status": "running",
                "registered_at": now,
                "last_heartbeat_at": now,
                "terminated_at": None,
            },
        )

    client = _client_with(handler)

    async def go() -> None:
        await _watch_kill(
            client, aid, kill_event_id=7, poll_interval=0.01, timeout_seconds=0.1
        )

    with pytest.raises(_typer.Exit) as exc:
        _asyncio.run(go())
    assert exc.value.exit_code == 6
    _asyncio.run(client.aclose())


def test_grant_command_rejects_unknown_symptom() -> None:
    """`grant --symptoms <bogus>` validates before any network call."""
    result = runner.invoke(
        app,
        [
            "grant",
            str(uuid4()),
            "--symptoms",
            "totally-fake-symptom",
            "--duration",
            "1h",
            "--reason",
            "y",
        ],
    )
    assert result.exit_code != 0
    # The error message names the bad symptom.
    out = (result.stdout or "") + (result.stderr or "")
    assert "totally-fake-symptom" in out


def test_grant_command_rejects_bad_duration() -> None:
    result = runner.invoke(
        app,
        [
            "grant",
            str(uuid4()),
            "--symptoms",
            "loop",
            "--duration",
            "notatime",
            "--reason",
            "y",
        ],
    )
    assert result.exit_code != 0


# --- helper sanity ------------------------------------------------------


def test_set_handler_isolates_between_tests() -> None:
    """Regression guard: after a test, _handler must reset to None (autouse fixture)."""
    assert cli_mod._handler is None  # type: ignore[attr-defined]
    _set_handler(lambda req: httpx.Response(418))
    assert cli_mod._handler is not None  # type: ignore[attr-defined]
    # When this test exits, the fixture will reset _handler to None.
    # Silence unused-import lint
    _ = _json


# --- fleet filtering -----------------------------------------------------


def _two_agents() -> list[dict[str, Any]]:
    now = datetime.now(UTC).isoformat()
    return [
        {
            "id": str(uuid4()),
            "name": "live-one",
            "policy_name": "coding-default",
            "status": "running",
            "registered_at": now,
            "last_heartbeat_at": now,
            "terminated_at": None,
        },
        {
            "id": str(uuid4()),
            "name": "dead-one",
            "policy_name": "strict",
            "status": "terminated",
            "registered_at": now,
            "last_heartbeat_at": None,
            "terminated_at": now,
        },
    ]


def test_fleet_default_hides_terminated() -> None:
    _set_handler(lambda _req: httpx.Response(200, json=_two_agents()))
    result = runner.invoke(app, ["fleet"])
    assert result.exit_code == 0, result.stdout
    assert "live-one" in result.stdout
    assert "dead-one" not in result.stdout


def test_fleet_all_shows_terminated() -> None:
    _set_handler(lambda _req: httpx.Response(200, json=_two_agents()))
    result = runner.invoke(app, ["fleet", "--all"])
    assert result.exit_code == 0, result.stdout
    assert "live-one" in result.stdout
    assert "dead-one" in result.stdout


def test_fleet_status_passes_query_param() -> None:
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["status"] = req.url.params.get("status")
        return httpx.Response(200, json=[_two_agents()[1]])

    _set_handler(handler)
    result = runner.invoke(app, ["fleet", "--status", "terminated"])
    assert result.exit_code == 0, result.stdout
    assert captured["status"] == "terminated"
    # --status bypasses the client-side active-only filter.
    assert "dead-one" in result.stdout


# --- rm ------------------------------------------------------------------


def test_rm_deletes_with_confirmation() -> None:
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["path"] = req.url.path
        return httpx.Response(204)

    _set_handler(handler)
    aid = str(uuid4())
    result = runner.invoke(app, ["rm", aid], input="y\n")
    assert result.exit_code == 0, result.stdout
    assert captured["method"] == "DELETE"
    assert captured["path"] == f"/agents/{aid}"
    assert "deleted" in result.stdout


def test_rm_aborts_on_no_confirmation() -> None:
    called = {"hit": False}

    def handler(_req: httpx.Request) -> httpx.Response:
        called["hit"] = True
        return httpx.Response(204)

    _set_handler(handler)
    result = runner.invoke(app, ["rm", str(uuid4())], input="n\n")
    assert result.exit_code != 0
    assert called["hit"] is False  # no request issued when aborted


def test_rm_yes_skips_prompt() -> None:
    _set_handler(lambda _req: httpx.Response(204))
    result = runner.invoke(app, ["rm", str(uuid4()), "--yes"])
    assert result.exit_code == 0, result.stdout


def test_rm_403_treated_as_auth_error() -> None:
    """Developer key on operator-only rm → 403 → AuthError → exit 2."""
    _set_handler(lambda _req: httpx.Response(403, json={"detail": "operator role required"}))
    result = runner.invoke(app, ["rm", str(uuid4()), "--yes"])
    assert result.exit_code == 2


# --- prune ---------------------------------------------------------------


def test_prune_posts_and_reports_count() -> None:
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["path"] = req.url.path
        captured["body"] = _json.loads(req.content)
        return httpx.Response(200, json={"deleted": 3})

    _set_handler(handler)
    result = runner.invoke(app, ["prune", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert captured["method"] == "POST"
    assert captured["path"] == "/agents/prune"
    assert captured["body"] == {"status": "terminated"}
    assert "pruned 3" in result.stdout


def test_prune_aborts_on_no_confirmation() -> None:
    called = {"hit": False}

    def handler(_req: httpx.Request) -> httpx.Response:
        called["hit"] = True
        return httpx.Response(200, json={"deleted": 0})

    _set_handler(handler)
    result = runner.invoke(app, ["prune"], input="n\n")
    assert result.exit_code != 0
    assert called["hit"] is False


# --- init ----------------------------------------------------------------


def test_init_writes_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    import caspase.config as config_mod

    cfg = tmp_path / "config.toml"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)
    result = runner.invoke(
        app,
        ["init", "--api-key", "sk_op_123", "--policy", "strict"],
    )
    assert result.exit_code == 0, result.stdout
    assert cfg.exists()
    text = cfg.read_text(encoding="utf-8")
    assert 'api_key = "sk_op_123"' in text
    assert 'policy = "strict"' in text


def test_init_refuses_existing_without_force(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import caspase.config as config_mod

    cfg = tmp_path / "config.toml"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)
    r1 = runner.invoke(app, ["init", "--api-key", "sk_first"])
    assert r1.exit_code == 0, r1.stdout
    r2 = runner.invoke(app, ["init", "--api-key", "sk_second"])
    assert r2.exit_code == 3
    # Unchanged.
    assert 'api_key = "sk_first"' in cfg.read_text(encoding="utf-8")


def test_init_force_overwrites(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    import caspase.config as config_mod

    cfg = tmp_path / "config.toml"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)
    runner.invoke(app, ["init", "--api-key", "sk_first"])
    r2 = runner.invoke(app, ["init", "--api-key", "sk_second", "--force"])
    assert r2.exit_code == 0, r2.stdout
    assert 'api_key = "sk_second"' in cfg.read_text(encoding="utf-8")
