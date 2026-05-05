"""Unit-level CLI tests using typer's CliRunner."""

from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from switcher.cli import app, handle_errors
from switcher.errors import SwitcherError


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


def test_handle_errors_renders_switcher_error_to_stderr(runner: CliRunner) -> None:
    """SwitcherError must surface as exit-code 1 + a rendered message on stderr.

    handle_errors is the CLI-wide error boundary; without coverage, a
    refactor that breaks the decorator (e.g., re-raising the wrong type
    or swallowing the message) would silently regress every command.
    """
    isolated = typer.Typer(pretty_exceptions_enable=False)

    @isolated.command()
    @handle_errors
    def explode() -> None:  # pyright: ignore[reportUnusedFunction]
        raise SwitcherError("simulated failure")

    # Single-command Typer app: invoke with no args runs the only command.
    result = runner.invoke(isolated, [])
    assert result.exit_code == 1
    assert "error:" in result.stderr
    assert "simulated failure" in result.stderr
