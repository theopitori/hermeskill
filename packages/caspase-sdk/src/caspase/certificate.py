"""Local death-certificate rendering + on-disk archival.

The control plane archives kill events and makes them queryable via
``caspase logs``. But the certificate itself is built entirely in-process by
:func:`caspase.apoptosis.build_death_certificate` — it needs no server. This
module renders that certificate to a human-readable box and saves it under
``~/.caspase/kills/`` so the autopsy is delivered on *every* kill, even with no
control plane configured (the zero-config path).

Plain text by design: the output goes to a log/stderr and to a file, where ANSI
escapes would be noise. The box-drawing glyphs match the offline demo's layout.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from caspase.types import DeathCertificate

KILLS_DIR = Path.home() / ".caspase" / "kills"

_WIDTH = 58


def render_certificate(
    cert: DeathCertificate,
    *,
    cost_line: str | None = None,
) -> str:
    """Render a death certificate as a plain-text box.

    ``cost_line`` is an optional pre-formatted summary (e.g.
    ``"$0.42  ·  18.2k in / 2.1k out"``) the adapter builds from the watcher's
    token/cost counters — those live on ``WatcherState``, not the cert, so the
    caller passes them in.
    """
    lines: list[str] = []
    lines.append("┌─ DEATH CERTIFICATE " + "─" * (_WIDTH - 19))
    lines.append(f"│ {'agent':<10} {cert.agent_id}")
    lines.append(f"│ {'trigger':<10} {cert.trigger_type.value}")
    lines.append(f"│ {'reason':<10} {cert.trigger_reason}")
    if cert.operator:
        lines.append(f"│ {'operator':<10} {cert.operator}")
    lines.append(f"│ {'symptoms':<10} {len(cert.symptoms_log)} logged")
    for s in cert.symptoms_log:
        symptom = s.get("symptom", "?")
        severity = s.get("severity", "?")
        reason = s.get("reason", "")
        lines.append(f"│   • {symptom} ({severity})  {reason}")
    lines.append(f"│ {'shutdown':<10} {len(cert.shutdown_log)} step(s)")
    for st in cert.shutdown_log:
        lines.append(f"│   • {st.step}")
    if cost_line:
        lines.append(f"│ {'cost':<10} {cost_line}")
    elapsed = (cert.terminated_at - cert.triggered_at).total_seconds()
    lines.append(f"│ {'elapsed':<10} {elapsed:.1f}s")
    lines.append("└" + "─" * (_WIDTH + 1))
    return "\n".join(lines)


def save_certificate(
    cert: DeathCertificate,
    *,
    directory: Path | None = None,
    cost_line: str | None = None,
) -> Path:
    """Write the rendered cert (``.txt``) and raw cert (``.json``) to disk.

    Returns the path to the ``.txt`` file. Best-effort: creates the directory
    if missing. Filename is ``<agent_id>-<UTC timestamp>`` so repeated kills in
    the same process don't clobber each other.
    """
    target_dir = directory or KILLS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    stem = f"{cert.agent_id}-{stamp}"
    txt_path = target_dir / f"{stem}.txt"
    json_path = target_dir / f"{stem}.json"
    txt_path.write_text(
        render_certificate(cert, cost_line=cost_line) + "\n", encoding="utf-8"
    )
    json_path.write_text(
        json.dumps(cert.model_dump(mode="json"), indent=2), encoding="utf-8"
    )
    return txt_path
