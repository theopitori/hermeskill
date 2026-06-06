"""Local death-certificate rendering + archival (no control plane)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from hermeskill.apoptosis import build_death_certificate, build_kill_event_payload
from hermeskill.certificate import render_certificate, save_certificate
from hermeskill.policies import resolve_policy
from hermeskill.types import (
    DeathCertificate,
    KillEventIn,
    ShutdownLogEntry,
    SymptomType,
    TriggerType,
)
from hermeskill.watcher import WatcherState


def _cert() -> DeathCertificate:
    now = datetime.now(UTC)
    return DeathCertificate(
        agent_id=uuid4(),
        triggered_at=now,
        terminated_at=now,
        trigger_type=TriggerType.AUTO,
        trigger_reason="read_file repeated 5x in last 5 actions (cap 5)",
        symptoms_log=[
            {
                "symptom": "loop",
                "severity": "terminal",
                "reason": "read_file repeated 5x",
                "detail": {},
            }
        ],
        shutdown_log=[ShutdownLogEntry(step="apoptosis_requested", at=now)],
    )


def test_steered_then_killed_cert_serializes_across_the_wire() -> None:
    """Regression: a `severity="steer"` symptom must survive the *online*
    serialization path. `symptoms_log` carries steer entries, and an online
    steered-then-killed agent POSTs them inside the death cert. Severity is
    free-form `str` end-to-end (no Literal/enum on DeathCertificate/KillEventIn),
    so this must construct cleanly — if anyone tightens severity to an enum
    without adding "steer", this fails instead of 500ing in production."""
    state = WatcherState(
        agent_id=uuid4(),
        name="t",
        policy=resolve_policy("coding-default"),
    )
    # Two steer nudges, then the kill — the real shape of a steered-then-killed
    # agent's symptoms_log.
    state.record_symptom(SymptomType.LOOP, "steer", "repeated 3x", {"count": 3})
    state.record_symptom(SymptomType.LOOP, "steer", "repeated 4x", {"count": 4})
    state.record_symptom(SymptomType.LOOP, "terminal", "repeated 5x", {"count": 5})
    state.request_termination("loop: repeated 5x")

    # Builds (and pydantic-validates) the full POST body — the online path.
    payload = build_kill_event_payload(state)
    assert isinstance(payload, KillEventIn)
    severities = [s["severity"] for s in payload.death_certificate.symptoms_log]
    assert severities == ["steer", "steer", "terminal"]

    # And it round-trips through JSON like the client would send it.
    KillEventIn.model_validate_json(payload.model_dump_json())

    # The renderer tolerates the steer severity too (cosmetic, but check it).
    cert = build_death_certificate(state)
    rendered = render_certificate(cert)
    assert "loop (steer)" in rendered
    assert "loop (terminal)" in rendered


def test_render_certificate_includes_key_fields() -> None:
    out = render_certificate(_cert(), cost_line="$0.42  ·  18.2k in / 2.1k out")
    assert "DEATH CERTIFICATE" in out
    assert "loop (terminal)" in out
    assert "apoptosis_requested" in out
    assert "$0.42" in out
    # Box is closed.
    assert out.rstrip().endswith("─")


def test_render_certificate_without_cost_line() -> None:
    out = render_certificate(_cert())
    assert "cost" not in out  # no cost row when none supplied
    assert "elapsed" in out


def test_save_certificate_writes_txt_and_json(tmp_path: Path) -> None:
    cert = _cert()
    txt = save_certificate(cert, directory=tmp_path, cost_line="$1.00  ·  1.0k in / 0 out")
    assert txt.exists() and txt.suffix == ".txt"
    json_path = txt.with_suffix(".json")
    assert json_path.exists()
    # The JSON round-trips back into a DeathCertificate.
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert DeathCertificate.model_validate(data).agent_id == cert.agent_id
    assert str(cert.agent_id) in txt.name
