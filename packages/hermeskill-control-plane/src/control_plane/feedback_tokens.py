"""Feedback-token issuance + hashing (M3).

The same symmetric-hash invariant the API-key code uses
([control_plane.auth.hash_api_key][]) applies here: never store the raw
token. The raw form is what the customer clicks in their feedback URL;
on the server side, both issuance and lookup go through this helper.

`generate_feedback_token()` returns `(raw, hash)`. Callers embed `raw`
in the URL stored on the `death_certificate` JSONB, persist `hash` on
the `feedback_tokens` row. The matching POST /feedback/{token} handler
hashes the URL's raw token before the SELECT (TODO.md #9).
"""

from __future__ import annotations

import secrets
from hashlib import sha256


def hash_feedback_token(raw: str) -> str:
    """SHA-256 hex digest of a raw feedback token.

    Same algorithm as [control_plane.auth.hash_api_key][] — keep them in
    sync if either ever changes (and write a migration to re-derive).
    """
    return sha256(raw.encode("utf-8")).hexdigest()


def generate_feedback_token() -> tuple[str, str]:
    """Mint a fresh `(raw, hash)` pair.

    `raw` is URL-safe (`secrets.token_urlsafe(32)` → ~43 chars, 256 bits
    of entropy). Caller embeds raw in the feedback URL, stores hash on
    the `feedback_tokens` row.
    """
    raw = secrets.token_urlsafe(32)
    return raw, hash_feedback_token(raw)


def build_feedback_url(base_url: str, raw_token: str) -> str:
    """Compose the feedback URL the customer clicks.

    Centralized so the URL shape stays consistent between issuance (in
    the kill_events POST) and the POST /feedback/{token} route.
    """
    return f"{base_url.rstrip('/')}/feedback/{raw_token}"
