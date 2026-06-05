"""SDK configuration: loads `~/.hermeskill/config.toml` + env-var overrides.

Resolution order (highest wins):
  1. explicit kwargs to `watch()` / `Client(...)`
  2. environment variables (HERMESKILL_API_KEY, HERMESKILL_BASE_URL,
     HERMESKILL_POLICY, HERMESKILL_AGENT_NAME)
  3. `~/.hermeskill/config.toml`
  4. built-in defaults

Write the config file once with `hermeskill init` so the per-session env-var
dance (export four vars every shell) isn't needed — the file is read from the
user's home dir, so it works from any directory, not just the repo.
"""

from __future__ import annotations

import contextlib
import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field

DEFAULT_BASE_URL = "http://localhost:8000"
CONFIG_PATH = Path.home() / ".hermeskill" / "config.toml"


def _load_dotenv_into_environ(path: Path = Path(".env")) -> None:
    """Best-effort .env loader. Only sets keys that aren't already in the env.

    Tiny on purpose — we don't want a python-dotenv dependency just for this.
    """
    if not path.exists():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())
    except OSError:
        return


class SDKConfig(BaseModel):
    base_url: str = Field(default=DEFAULT_BASE_URL)
    api_key: str | None = None
    # Runtime-adapter hints (e.g. hermeskill-hermes). Optional here in the core
    # SDK — the adapter applies its own defaults when these are unset — but
    # surfaced so they can live in config.toml and stop being per-session env
    # vars. `None` means "not configured; let the adapter decide".
    policy: str | None = None
    agent_name: str | None = None
    # When a kill fires, render the death certificate to the terminal and save
    # it under ~/.hermeskill/kills/ — so the autopsy is delivered even with no
    # control plane. On by default; set HERMESKILL_LOCAL_CERT=0 (or local_cert =
    # false in config.toml) to disable.
    local_cert: bool = True
    # Write the live-vitals snapshot for `hermeskill monitor` on each hook tick
    # (~/.hermeskill/live/). On by default; set HERMESKILL_LIVE=0 (or
    # live_vitals = false in config.toml) to skip the per-tick file write
    # entirely for agents that are never monitored.
    live_vitals: bool = True

    @classmethod
    def load(cls) -> SDKConfig:
        # Pull `.env` from the CWD into os.environ first (idempotent, doesn't
        # overwrite real env vars), so `hermeskill fleet` works without the user
        # having to `export` anything in their shell.
        _load_dotenv_into_environ()

        data: dict[str, object] = {}
        if CONFIG_PATH.exists():
            with CONFIG_PATH.open("rb") as f:
                data = dict(tomllib.load(f))
        if env_url := os.environ.get("HERMESKILL_BASE_URL"):
            data["base_url"] = env_url
        if env_key := os.environ.get("HERMESKILL_API_KEY"):
            data["api_key"] = env_key
        if env_policy := os.environ.get("HERMESKILL_POLICY"):
            data["policy"] = env_policy
        if env_name := os.environ.get("HERMESKILL_AGENT_NAME"):
            data["agent_name"] = env_name
        _falsey = {"0", "false", "no", "off", ""}
        if (env_cert := os.environ.get("HERMESKILL_LOCAL_CERT")) is not None:
            data["local_cert"] = env_cert.strip().lower() not in _falsey
        if (env_live := os.environ.get("HERMESKILL_LIVE")) is not None:
            data["live_vitals"] = env_live.strip().lower() not in _falsey
        return cls.model_validate(data)


def save_config(config: SDKConfig, *, force: bool = False) -> Path:
    """Write `config` to `~/.hermeskill/config.toml` and return the path.

    Only non-empty values are persisted. Refuses to clobber an existing file
    unless ``force=True`` (the CLI surfaces this as a clear error + `--force`
    hint). The file holds an API key, so it's created `0600` on POSIX.
    """
    if CONFIG_PATH.exists() and not force:
        raise FileExistsError(CONFIG_PATH)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    def _q(value: str) -> str:
        # Minimal TOML basic-string quoting — escape backslash and quote.
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    lines = [f"base_url = {_q(config.base_url)}"]
    if config.api_key:
        lines.append(f"api_key = {_q(config.api_key)}")
    if config.policy:
        lines.append(f"policy = {_q(config.policy)}")
    if config.agent_name:
        lines.append(f"agent_name = {_q(config.agent_name)}")
    # Only persist when overriding the default (True) — keeps the file minimal.
    if not config.local_cert:
        lines.append("local_cert = false")
    if not config.live_vitals:
        lines.append("live_vitals = false")
    CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        CONFIG_PATH.chmod(0o600)
    return CONFIG_PATH
