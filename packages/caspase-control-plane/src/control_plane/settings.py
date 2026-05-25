"""Control-plane settings (env-driven).

`CASPASE_DB_URL` must be set in any environment doing real DB work. Defaults to
a local Postgres 18 instance on Windows dev machines.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CASPASE_", env_file=".env", extra="ignore")

    db_url: str = "postgresql+asyncpg://caspase:caspase@localhost:5432/caspase"
    debug: bool = False

    heartbeat_interval_seconds: int = 30
    verification_timeout_seconds: int = 30
    kill_poll_interval_seconds: int = 3

    # Base URL used to compose the one-click feedback URL embedded in
    # each death certificate (M3). Set this to the public origin in prod
    # so the link is clickable from operator email/Slack.
    feedback_base_url: str = "http://localhost:8000"
    # How long an issued feedback token stays valid before lookup 404s.
    feedback_token_ttl_days: int = 30


settings = Settings()
