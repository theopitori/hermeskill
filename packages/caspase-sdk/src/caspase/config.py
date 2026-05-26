"""SDK configuration: loads `~/.caspase/config.toml` + env-var overrides.

Resolution order (highest wins):
  1. explicit kwargs to `watch()` / `Client(...)`
  2. environment variables (CASPASE_API_KEY, CASPASE_BASE_URL)
  3. `~/.caspase/config.toml`
  4. built-in defaults

Filled lightly in M0 so other modules can import a stable type; the
file-loading path is exercised in M1 when the CLI gains real subcommands.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field

DEFAULT_BASE_URL = "http://localhost:8000"
CONFIG_PATH = Path.home() / ".caspase" / "config.toml"


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

    @classmethod
    def load(cls) -> SDKConfig:
        # Pull `.env` from the CWD into os.environ first (idempotent, doesn't
        # overwrite real env vars), so `caspase fleet` works without the user
        # having to `export` anything in their shell.
        _load_dotenv_into_environ()

        data: dict[str, object] = {}
        if CONFIG_PATH.exists():
            with CONFIG_PATH.open("rb") as f:
                data = dict(tomllib.load(f))
        if env_url := os.environ.get("CASPASE_BASE_URL"):
            data["base_url"] = env_url
        if env_key := os.environ.get("CASPASE_API_KEY"):
            data["api_key"] = env_key
        return cls.model_validate(data)
