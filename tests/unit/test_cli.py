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


def test_init_run_succeeds(runner: CliRunner, tmp_home: Path, tmp_state: Path) -> None:
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert "Initialized" in result.stdout


def test_init_then_status_shows_active(runner: CliRunner, tmp_home: Path, tmp_state: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "claude" in result.stdout


def test_use_vanilla_after_init(runner: CliRunner, tmp_home: Path, tmp_state: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["use", "vanilla"])
    assert result.exit_code == 0
    assert "vanilla" in result.stdout


def test_use_only_filter(runner: CliRunner, tmp_home: Path, tmp_state: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["use", "vanilla", "--only", "claude"])
    assert result.exit_code == 0
    assert "claude" in result.stdout


def test_create_then_list(runner: CliRunner, tmp_home: Path, tmp_state: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["create", "experiment"])
    assert result.exit_code == 0
    listed = runner.invoke(app, ["list"])
    assert "experiment" in listed.stdout


def test_save_command(runner: CliRunner, tmp_home: Path, tmp_state: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["save", "snap"])
    assert result.exit_code == 0


def test_rename_command(runner: CliRunner, tmp_home: Path, tmp_state: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["rename", "vanilla", "fresh"])
    assert result.exit_code == 0


def test_delete_with_force(runner: CliRunner, tmp_home: Path, tmp_state: Path) -> None:
    runner.invoke(app, ["init"])
    runner.invoke(app, ["use", "vanilla"])  # switch off the dated profile
    listed = runner.invoke(app, ["list"]).stdout
    dated = next(line.split()[-1] for line in listed.splitlines() if "current" in line)
    result = runner.invoke(app, ["delete", dated, "--force"])
    assert result.exit_code == 0


def test_delete_active_refused(runner: CliRunner, tmp_home: Path, tmp_state: Path) -> None:
    runner.invoke(app, ["init"])
    listed = runner.invoke(app, ["list"]).stdout
    dated = next(line.split()[-1] for line in listed.splitlines() if "current" in line)
    blocked = runner.invoke(app, ["delete", dated, "--force"])
    assert blocked.exit_code == 1
    assert "active for" in blocked.stderr


def test_which_command(runner: CliRunner, tmp_home: Path, tmp_state: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["which", "claude"])
    assert result.exit_code == 0
    assert "current" in result.stdout


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
