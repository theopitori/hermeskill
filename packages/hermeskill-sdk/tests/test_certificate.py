"""Local death-certificate rendering + archival (no control plane)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from hermeskill.certificate import render_certificate, save_certificate
from hermeskill.types import DeathCertificate, ShutdownLogEntry, TriggerType


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
