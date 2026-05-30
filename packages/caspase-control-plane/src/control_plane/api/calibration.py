"""Policy calibration endpoint (Phase 4 — feedback-driven threshold suggestions).

`GET /policies/{policy_name}/calibration` turns the operator feedback labels
collected on death certificates into an advisory calibration report. It is
read-only and customer-scoped: it only ever reads `kill_events` rows for agents
owned by the caller's customer, and it never mutates a policy (policies are
SDK-defined constants — the report tells a human what to edit).

The actual aggregation/heuristic lives in `caspase.calibration` so it can be
unit-tested without a database. This module is the thin I/O shell: pull the
labeled kills, extract each one's terminal symptom from the stored death
certificate, hand them to the pure aggregator.

Why extract the symptom in Python and not SQL: the terminal symptom lives
inside `death_certificate["symptoms_log"]` (the entry with
`severity == "terminal"`), not in the scalar `trigger_type` column (that's
auto/manual). The cert column is JSONB on Postgres but an affinity-patched JSON
string on the SQLite test/demo path, so a JSON-path query wouldn't be portable.
We filter on the scalar columns in SQL and dig the symptom out in Python.
"""

from __future__ import annotations

from typing import Annotated, Any

from caspase.calibration import LabeledKill, build_calibration_report
from caspase.policies import UnknownPolicyError, resolve_policy
from caspase.types import CalibrationReport, FeedbackLabel, SymptomType
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth import Principal, require_principal
from control_plane.db.models import Agent, KillEvent
from control_plane.db.session import get_session

router = APIRouter(prefix="/policies", tags=["calibration"])


def _terminal_symptom(death_certificate: dict[str, Any] | None) -> SymptomType | None:
    """Pull the symptom that triggered the kill out of a stored death cert.

    Prefers the `severity == "terminal"` entry in `symptoms_log` (the one that
    actually fired apoptosis); falls back to the last recorded symptom. Returns
    None for certs with no usable symptom (e.g. a bare manual kill) so the
    caller can skip the row rather than mis-attribute it.
    """
    if not death_certificate:
        return None
    log = death_certificate.get("symptoms_log") or []
    terminal = next(
        (e for e in reversed(log) if e.get("severity") == "terminal"),
        log[-1] if log else None,
    )
    if not terminal:
        return None
    try:
        return SymptomType(terminal.get("symptom"))
    except ValueError:
        return None


def _parse_label(raw: str | None) -> FeedbackLabel | None:
    if raw is None:
        return None
    try:
        return FeedbackLabel(raw)
    except ValueError:
        return None


@router.get("/{policy_name}/calibration", response_model=CalibrationReport)
async def get_policy_calibration(
    policy_name: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> CalibrationReport:
    """Advisory calibration report for a policy, from this customer's feedback.

    404 if the policy name isn't one the SDK ships. An empty report (no
    suggestions, total 0) is a valid 200 — it just means no labeled kills yet.
    """
    try:
        policy = resolve_policy(policy_name)
    except UnknownPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown policy: {policy_name!r}",
        ) from exc

    stmt = (
        select(KillEvent.death_certificate, KillEvent.feedback_label)
        .join(Agent, Agent.id == KillEvent.agent_id)
        .where(
            Agent.customer_id == principal.customer_id,
            Agent.policy_name == policy_name,
            KillEvent.feedback_label.is_not(None),
        )
    )
    rows = (await session.execute(stmt)).all()

    labeled: list[LabeledKill] = []
    for cert, raw_label in rows:
        symptom = _terminal_symptom(cert)
        label = _parse_label(raw_label)
        if symptom is None or label is None:
            continue
        labeled.append(LabeledKill(symptom=symptom, label=label))

    return build_calibration_report(policy, labeled)
