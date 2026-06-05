"""Tests for `hermeskill enable-hermes` — the one-shot plugin enable command."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from hermeskill.cli import app
from typer.testing import CliRunner

runner = CliRunner()


class _FakeHermesConfig:
    """Stand-in for hermes_cli.config backed by an in-memory dict."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.saved: dict[str, Any] | None = None

    def load_config(self) -> dict[str, Any]:
        return self.cfg

    def save_config(self, cfg: dict[str, Any]) -> None:
        self.saved = cfg

    def get_config_path(self) -> Path:
        return Path("/tmp/hermes/config.yaml")


@pytest.fixture
def fake_hermes(monkeypatch: pytest.MonkeyPatch) -> _FakeHermesConfig:
    fake = _FakeHermesConfig({})
    module = type(sys)("hermes_cli.config")
    module.load_config = fake.load_config  # type: ignore[attr-defined]
    module.save_config = fake.save_config  # type: ignore[attr-defined]
    module.get_config_path = fake.get_config_path  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_cli.config", module)
    return fake


def test_enable_adds_hermeskill(fake_hermes: _FakeHermesConfig) -> None:
    result = runner.invoke(app, ["enable-hermes"])
    assert result.exit_code == 0
    assert fake_hermes.saved is not None
    assert fake_hermes.saved["plugins"]["enabled"] == ["hermeskill"]


def test_enable_is_idempotent(fake_hermes: _FakeHermesConfig) -> None:
    fake_hermes.cfg = {"plugins": {"enabled": ["hermeskill"]}}
    result = runner.invoke(app, ["enable-hermes"])
    assert result.exit_code == 0
    assert "already enabled" in result.output
    assert fake_hermes.saved is None  # nothing rewritten


def test_disable_removes_hermeskill(fake_hermes: _FakeHermesConfig) -> None:
    fake_hermes.cfg = {"plugins": {"enabled": ["hermeskill", "other"]}}
    result = runner.invoke(app, ["enable-hermes", "--disable"])
    assert result.exit_code == 0
    assert fake_hermes.saved is not None
    assert fake_hermes.saved["plugins"]["enabled"] == ["other"]


def test_enable_when_hermes_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Make `from hermes_cli.config import ...` raise ImportError.
    monkeypatch.setitem(sys.modules, "hermes_cli.config", None)
    result = runner.invoke(app, ["enable-hermes"])
    assert result.exit_code == 2
    assert "Hermes Agent isn't importable" in result.output
