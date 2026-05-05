"""Unit-level CLI tests using typer's CliRunner."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from switcher.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_version_command(runner: CliRunner) -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "switcher" in result.stdout


def test_tools_lists_builtins(runner: CliRunner, tmp_state: Path) -> None:
    result = runner.invoke(app, ["tools"])
    assert result.exit_code == 0
    assert "claude" in result.stdout
    assert "copilot" in result.stdout


def test_list_empty_when_uninitialized(runner: CliRunner, tmp_state: Path) -> None:
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "No profiles" in result.stdout


def test_status_no_active_when_uninitialized(runner: CliRunner, tmp_state: Path) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "no active profiles" in result.stdout
