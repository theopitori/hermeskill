"""Hermeskill SDK exceptions."""


class HermeskillError(Exception):
    """Base class for all Hermeskill SDK errors."""


class HermeskillTerminated(HermeskillError):
    """Raised inside a watched agent when the apoptosis flag has been set.

    Bubbles up through the agent's call stack so `finally` blocks and
    registered cleanup hooks run. Callers of the watched invoke should treat
    this as a normal termination outcome, not an exception to retry.
    """

    def __init__(self, reason: str, *, kill_event_id: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.kill_event_id = kill_event_id
