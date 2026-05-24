"""Per-agent WatcherState dataclass and supporting constants."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from stasis_agent.pricing import cost_for_usage
from stasis_agent.types import EventIn, EventType, Policy, SymptomType

if TYPE_CHECKING:
    from stasis_agent.apoptosis import Watchdog

DEFAULT_HEARTBEAT_INTERVAL = 30  # seconds — overridden per-watcher by policy
DEFAULT_KILL_POLL_INTERVAL = 3  # seconds — M4 manual-kill latency budget
EVENT_BATCH_MAX = 500


@dataclass
class WatcherState:
    """Per-agent supervision state. One per `watch()` call.

    `policy` is the resolved Policy object (from `stasis_agent.policies`),
    not just the name — M2 symptom checks read thresholds and the tool
    allowlist straight off `state.policy.thresholds.*` and
    `state.policy.tool_allowlist`.
    """

    agent_id: UUID
    name: str
    policy: Policy
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_monotonic: float = field(default_factory=time.monotonic)

    # Apoptosis flag — set by M2 symptom checks or by manual kill (M4).
    # The actual `StasisTerminated` raise happens at checkpoint sites
    # (langgraph gate nodes, `stasis.checkpoint()`), not here.
    terminate_requested: bool = False
    terminate_reason: str | None = None
    terminate_kill_event_id: str | None = None
    # Set by `request_termination()` — the moment the kill decision was
    # made. Goes into the death cert's `triggered_at`. Distinct from the
    # cert's `terminated_at` (which is when the agent actually exits).
    terminate_requested_at: datetime | None = None

    # Manual-kill context (M4). Populated by the kill-pending poller
    # *before* it calls `request_termination()` so the cert builder sees
    # both the flag and the operator info atomically from its POV.
    #
    # Shape: {"operator": str | None, "operator_reason": str | None,
    #         "kill_event_id": int}.
    #
    # `None` for auto-kill paths; the cert builder branches on
    # presence/absence rather than on `trigger_type` so a future kill
    # source (programmatic, scheduled) can populate this without a
    # separate enum bit.
    manual_kill: dict[str, Any] | None = None

    # Append-only forensic log — populated by `record_shutdown_step()`
    # during the apoptosis sequence (cert built, hook ran, etc.). Goes
    # into the death cert.
    shutdown_log: list[dict[str, Any]] = field(default_factory=list)

    # Append-only symptoms log — every Terminal/Warning the checks fired.
    # Powered by `record_symptom()` (which also queues the same data as
    # an event for live monitoring). Goes into the death cert.
    symptoms_log: list[dict[str, Any]] = field(default_factory=list)

    # threading.Event paired with `terminate_requested`. The M2.4 watchdog
    # thread sleeps on this so it wakes the *instant* apoptosis is requested,
    # not after a polling interval. All apoptosis-flag flips should go through
    # `request_termination()` so this stays consistent — but the watchdog
    # also polls the flag as a defensive fallback in case a caller writes
    # the flag directly.
    _terminate_event: threading.Event = field(default_factory=threading.Event)

    # L2 watchdog (M2.4). Set by `watch()` after construction so the
    # watcher knows the agent's policy grace seconds. May be None for
    # synthetic test states that don't need a watchdog.
    watchdog: Watchdog | None = None

    # Loop detection ring buffer. Sized from policy in __post_init__ — set
    # to a placeholder here so the dataclass machinery is happy.
    loop_signatures: deque[str] = field(default_factory=deque)

    # Token/cost runaway counters — updated by the LangChain handler on llm_end.
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0

    # Active grants (M5 populates from heartbeat response).
    grants: list[dict[str, Any]] = field(default_factory=list)

    # Pending events queue — appended by callbacks, drained by the worker.
    # Lock protects against concurrent appends from sync LangChain callbacks
    # and the async worker.
    _pending_events: list[EventIn] = field(default_factory=list)
    _events_lock: threading.Lock = field(default_factory=threading.Lock)

    # Cleanup hooks — M2's L1 cooperative termination runs these.
    cleanup_hooks: list[Any] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Resize the ring buffer to match the policy's loop window. Keep any
        # signatures already appended (none, in practice — __post_init__ runs
        # before any record_* call) by re-creating the deque from the iter.
        self.loop_signatures = deque(
            self.loop_signatures,
            maxlen=self.policy.thresholds.loop_window_actions,
        )

    # --- convenience -------------------------------------------------------

    @property
    def policy_name(self) -> str:
        """Backwards-compat alias for callers that just want the name."""
        return self.policy.name

    # --- mutators (thread-safe where needed) ------------------------------

    def record_tool_call(self, tool_name: str, params: Any) -> None:
        """Append a tool_call event + update loop ring buffer."""
        sig = _signature(tool_name, params)
        self.loop_signatures.append(sig)
        self._enqueue(
            EventIn(
                type=EventType.TOOL_CALL,
                payload={"tool": tool_name, "signature": sig},
            )
        )

    def record_llm_call(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """Update token/cost counters + queue an llm_call event."""
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        cost = cost_for_usage(model, input_tokens, output_tokens)
        self.total_cost_usd += cost
        self._enqueue(
            EventIn(
                type=EventType.LLM_CALL,
                payload={
                    "model": model,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cost_usd": cost,
                    "cumulative_cost_usd": self.total_cost_usd,
                },
            )
        )

    def record_lifecycle(self, phase: str, **extra: Any) -> None:
        self._enqueue(
            EventIn(
                type=EventType.LIFECYCLE,
                payload={"phase": phase, **extra},
            )
        )

    def request_termination(
        self,
        reason: str,
        *,
        kill_event_id: str | None = None,
        manual_kill: dict[str, Any] | None = None,
    ) -> None:
        """Flip the apoptosis flag (first-cause wins) and wake the watchdog.

        Idempotent: if already flagged, the reason and `terminate_requested_at`
        are preserved (the death cert needs to show what *first* killed
        the agent, and *when* it was first decided). The watchdog still
        gets re-poked — harmless because it's already running its grace
        timer.

        All apoptosis-flag flips — symptom checks, manual kill (M4),
        external grant revocations — should go through this method so the
        watchdog wakes immediately rather than waiting for its poll, and
        so `terminate_requested_at` is captured for the death cert.

        `manual_kill` carries operator context for M4 kills. It's set
        **inside** this method (rather than by the caller before the
        call) so first-cause-wins covers both fields atomically: an
        auto-kill that lands a microsecond before the poller can't be
        retroactively re-classified as manual.
        """
        if not self.terminate_requested:
            self.terminate_requested = True
            self.terminate_reason = reason
            self.terminate_kill_event_id = kill_event_id
            self.terminate_requested_at = datetime.now(UTC)
            self.manual_kill = manual_kill
            # Record the first shutdown-log step at the moment of decision
            # so the death cert (built later, from this same list) has
            # something to show — even if the kill path is very short
            # (e.g. tool-scope violation that raises immediately from
            # on_tool_start with no other steps in between).
            self.record_shutdown_step(
                "apoptosis_requested",
                reason=reason,
                kill_event_id=kill_event_id,
            )
        # Set the event unconditionally so a watchdog armed *after* the
        # flag was first flipped still gets the signal.
        self._terminate_event.set()

    def record_shutdown_step(
        self,
        step: str,
        *,
        duration_ms: float | None = None,
        **detail: Any,
    ) -> None:
        """Append a structured entry to the shutdown log.

        Each step gets a timestamp + optional duration + free-form detail
        dict. The death cert serializes the list in order — operators
        read it to understand the apoptosis sequence post-mortem.
        """
        self.shutdown_log.append(
            {
                "step": step,
                "at": datetime.now(UTC).isoformat(),
                "duration_ms": duration_ms,
                "detail": detail,
            }
        )

    def record_symptom(
        self,
        symptom: SymptomType,
        severity: str,
        reason: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Queue a SYMPTOM event for `stasis logs` AND append to symptoms_log
        for the death cert.

        `severity` is `"terminal"` (apoptosis fires) or `"warning"`
        (suppressed by an M5 grant). Both are sent so audit shows what
        *would have* killed the agent even when a grant let it live.

        We write to two sinks because they have different access patterns:
          * Event queue → drained by BackgroundWorker, fans out to
            `/agents/{id}/events`. Live monitoring; ephemeral.
          * `symptoms_log` → kept in memory until the death cert is
            built. Captures the symptom history even if the event POST
            failed.
        """
        record = {
            "symptom": symptom.value,
            "severity": severity,
            "reason": reason,
            "detail": detail or {},
            "at": datetime.now(UTC).isoformat(),
        }
        self.symptoms_log.append(record)
        self._enqueue(
            EventIn(
                type=EventType.SYMPTOM,
                payload={
                    "symptom": symptom.value,
                    "severity": severity,
                    "reason": reason,
                    "detail": detail or {},
                },
            )
        )

    def _enqueue(self, event: EventIn) -> None:
        with self._events_lock:
            self._pending_events.append(event)

    def drain_events(self) -> list[EventIn]:
        """Atomically take the pending events and return them."""
        with self._events_lock:
            events = self._pending_events
            self._pending_events = []
        return events

    def requeue_events(self, events: list[EventIn]) -> None:
        """Prepend events back onto the queue (e.g. after a failed POST)."""
        with self._events_lock:
            self._pending_events = events + self._pending_events

    def uptime_seconds(self) -> float:
        return time.monotonic() - self.started_monotonic


def _signature(tool_name: str, params: Any) -> str:
    """Stable signature for loop detection: tool_name + SHA1(canonical-JSON of params)."""
    try:
        canonical = json.dumps(params, sort_keys=True, default=str)
    except (TypeError, ValueError):
        canonical = repr(params)
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{tool_name}|{digest}"
