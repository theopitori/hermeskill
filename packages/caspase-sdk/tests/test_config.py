"""Tests for SDKConfig load/save and the `caspase init` command.

`caspase init` writes ~/.caspase/config.toml so users don't re-export four
env vars every shell. These tests redirect CONFIG_PATH to a tmp dir and chdir
into it so the repo's real .env / config never leak in.
"""

from pathlib import Path
from typing import Any

import caspase.config as config_mod
import pytest
from caspase.config import DEFAULT_BASE_URL, SDKConfig, save_config


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point CONFIG_PATH at a tmp file, chdir into an empty dir (no .env),
    and clear CASPASE_* env so resolution is deterministic."""
    cfg = tmp_path / "config.toml"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    for var in (
        "CASPASE_API_KEY",
        "CASPASE_BASE_URL",
        "CASPASE_POLICY",
        "CASPASE_AGENT_NAME",
    ):
        monkeypatch.delenv(var, raising=False)
    yield cfg


# --- save/load round trip -------------------------------------------------


def test_save_then_load_round_trip(_isolate_config: Path) -> None:
    save_config(
        SDKConfig(
            base_url="https://cp.example.com",
            api_key="sk_operator_abc",
            policy="strict",
            agent_name="hermes-prod",
        )
    )
    assert _isolate_config.exists()

    loaded = SDKConfig.load()
    assert loaded.base_url == "https://cp.example.com"
    assert loaded.api_key == "sk_operator_abc"
    assert loaded.policy == "strict"
    assert loaded.agent_name == "hermes-prod"


def test_load_defaults_when_no_file_no_env() -> None:
    loaded = SDKConfig.load()
    assert loaded.base_url == DEFAULT_BASE_URL
    assert loaded.api_key is None
    assert loaded.policy is None
    assert loaded.agent_name is None


def test_save_omits_empty_optionals(_isolate_config: Path) -> None:
    save_config(SDKConfig(base_url=DEFAULT_BASE_URL, api_key="sk_x"))
    text = _isolate_config.read_text(encoding="utf-8")
    assert "policy" not in text
    assert "agent_name" not in text
    assert 'api_key = "sk_x"' in text


def test_env_overrides_file(_isolate_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    save_config(SDKConfig(base_url="http://from-file", api_key="sk_file", policy="strict"))
    monkeypatch.setenv("CASPASE_API_KEY", "sk_env")
    monkeypatch.setenv("CASPASE_POLICY", "permissive")

    loaded = SDKConfig.load()
    assert loaded.api_key == "sk_env"  # env wins
    assert loaded.policy == "permissive"  # env wins
    assert loaded.base_url == "http://from-file"  # file value kept where no env


def test_save_refuses_to_clobber_without_force(_isolate_config: Path) -> None:
    save_config(SDKConfig(base_url=DEFAULT_BASE_URL, api_key="sk_first"))
    with pytest.raises(FileExistsError):
        save_config(SDKConfig(base_url=DEFAULT_BASE_URL, api_key="sk_second"))
    # Original untouched.
    assert SDKConfig.load().api_key == "sk_first"


def test_save_force_overwrites(_isolate_config: Path) -> None:
    save_config(SDKConfig(base_url=DEFAULT_BASE_URL, api_key="sk_first"))
    save_config(SDKConfig(base_url=DEFAULT_BASE_URL, api_key="sk_second"), force=True)
    assert SDKConfig.load().api_key == "sk_second"


def test_save_quotes_backslashes_and_quotes(_isolate_config: Path) -> None:
    """Windows-style values with backslashes/quotes must round-trip via TOML."""
    save_config(
        SDKConfig(base_url=DEFAULT_BASE_URL, api_key='sk_a\\b"c'),
    )
    assert SDKConfig.load().api_key == 'sk_a\\b"c'
