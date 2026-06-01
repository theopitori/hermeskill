"""``python -m demo --scenario calibrate`` — feedback turns into a tuning hint.

The other scenarios end at the death certificate. This one shows what Hermeskill
does with the *feedback* on those certificates. It:

  1. drives the real engine into a `loop` kill on several agents under the
     `strict` policy (whose loop cap is a deliberately tight 3),
  2. submits an operator verdict on each via the **real** one-click feedback
     endpoint (the same `POST /feedback/{token}` a human hits from the link in
     the cert) — labelling the majority *false-positive*, as if the cap were
     too aggressive,
  3. asks the control plane for a calibration report and shows the advisory
     suggestion that falls out: "consider raising max_loop_repeats 3→5".

Nothing is auto-applied. The point is the honest mechanism — feedback in,
evidence-backed suggestion out — not a self-mutating limit.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from demo._style import RULE, bold, cyan, dim, green, prepare_console, yellow
from demo.coding_agent._bootstrap import _DEV_DEVELOPER_KEY, start_control_plane
from demo.rogue import new_state, run_scenario

_BASE_URL = "http://localhost:8000"
_POLICY = "strict"
_N_KILLS = 5
#: First three kills labelled false-positive → 60% > the 30% action threshold.
_LABELS = ["false_positive", "false_positive", "false_positive", "good_kill", "good_kill"]


@dataclass(slots=True)
class CalibrateOutcome:
    labeled: int
    loop_suggested_value: float | None


def _token_from_url(feedback_url: str) -> str:
    """The raw token is the last path segment of the cert's feedback URL."""
    return feedback_url.rstrip("/").rsplit("/", 1)[-1]


async def run_calibrate_demo(*, quiet: bool = False) -> CalibrateOutcome:
    """Seed labelled loop-kills, then surface the calibration suggestion."""
    if not quiet:
        prepare_console()

    def say(*args: object) -> None:
        if not quiet:
            print(*args)

    from hermeskill.apoptosis import build_kill_event_payload
    from hermeskill.client import HermeskillClient

    say()
    say(bold(cyan("  HERMESKILL")) + dim("  ·  feedback-driven calibration (Phase 4)"))
    say(dim(f"  policy: {_POLICY}   scenario: calibrate"))
    say(dim("  " + RULE))
    say()
    say(dim("  Every death certificate ships with a one-click feedback link. This"))
    say(dim("  scenario files several loop-kills, has an 'operator' label most of"))
    say(dim("  them false-positive, then asks Hermeskill what that feedback implies."))
    say()

    say(f"{cyan('▸')} booting in-process control plane {dim('(sqlite, no postgres)')} …")
    _demo_db = Path(tempfile.gettempdir()) / "hermeskill-demo.db"
    os.environ["HERMESKILL_DB_URL"] = f"sqlite+aiosqlite:///{_demo_db}"
    server, serve_task = await start_control_plane()
    os.environ["HERMESKILL_API_KEY"] = _DEV_DEVELOPER_KEY
    os.environ["HERMESKILL_BASE_URL"] = _BASE_URL
    say(f"  {green('✓')} control plane up at {dim(_BASE_URL)}")

    client = HermeskillClient.from_config()
    labeled = 0
    try:
        say(f"{cyan('▸')} filing {_N_KILLS} loop-kills and labelling them …")
        async with httpx.AsyncClient(base_url=_BASE_URL) as feedback_http:
            for i in range(_N_KILLS):
                name = f"demo-cal-agent-{i}"
                reg = await client.register_agent(name=name, policy_name=_POLICY)
                agent_id = reg.agent_id

                # One real loop kill per agent (one cert per agent — the server
                # allows only one confirmed kill_event per agent).
                state = new_state(_POLICY, agent_id, name)
                run_scenario("loop", state)
                with contextlib.suppress(Exception):
                    await client.post_events(agent_id, state.drain_events())
                posted = await client.post_kill_event(
                    agent_id, build_kill_event_payload(state)
                )
                cert = None if isinstance(posted, int) else posted.death_certificate
                label = _LABELS[i]
                if cert is not None and cert.feedback_url:
                    resp = await feedback_http.post(
                        f"/feedback/{_token_from_url(cert.feedback_url)}",
                        json={"label": label},
                    )
                    if resp.status_code == 200:
                        labeled += 1
                glyph = yellow("✗ false-positive") if label == "false_positive" \
                    else green("✓ good kill")
                say(f"  {dim(f'kill {i + 1}')}  loop  →  operator: {glyph}")

        say(f"  {green('✓')} {labeled}/{_N_KILLS} verdicts recorded")
        say()

        say(f"{cyan('▸')} asking for the calibration report …")
        report = await client.get_calibration(_POLICY)
        loop_row = next(
            (s for s in report.symptoms if s.symptom.value == "loop"), None
        )

        say()
        say(dim(f"  ┌─ CALIBRATION · {_POLICY} {'─' * 36}"))
        for s in report.symptoms:
            fp = f"{s.false_positive_rate * 100:.0f}%"
            say(dim("  │ ") + f"{s.symptom.value:<14} "
                f"n={s.total_labeled}  fp={fp:<4}  [{s.confidence}]")
            if s.suggested_value is not None and s.threshold_field is not None:
                say(dim("  │   ") + green(
                    f"↑ raise {s.threshold_field} "
                    f"{s.current_value:g} → {s.suggested_value:g}"
                ))
        say(dim(f"  └{'─' * 58}"))
        say()
        loop_suggested = loop_row.suggested_value if loop_row else None
        if loop_suggested is not None:
            say("  " + bold(green(
                f"suggestion: raise the loop cap 3 → {loop_suggested:g} "
                "(60% of loop-kills were labeled false-positive)"
            )))
        say()
        say(dim("  view it yourself:  ") + f"hermeskill calibrate {_POLICY}")
        say()
        say(dim("  note: nothing was auto-applied. hermeskill suggests a looser limit"))
        say(dim("  for a human to set; it never tightens (it can't see kills that"))
        say(dim("  should have fired but didn't) and never edits the policy itself."))
        say()

        return CalibrateOutcome(labeled=labeled, loop_suggested_value=loop_suggested)
    finally:
        await client.aclose()
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(serve_task, timeout=5.0)
