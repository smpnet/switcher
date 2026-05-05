"""Unit-level CLI tests using typer's CliRunner."""

from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from switcher.cli import app, handle_errors
from switcher.errors import SwitcherError
from switcher.store import FileProfileStore


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


def test_use_only_strips_empty_entries(runner: CliRunner, tmp_home: Path, tmp_state: Path) -> None:
    """Trailing-comma / whitespace shouldn't surface as `unknown tool ''`."""
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["use", "vanilla", "--only", "claude, "])
    assert result.exit_code == 0
    # Output must not contain the empty entry that the naive split would produce
    assert ", ," not in result.stdout
    assert result.stdout.rstrip().endswith("claude")


def test_use_only_rejects_empty_list(runner: CliRunner, tmp_home: Path, tmp_state: Path) -> None:
    """`--only ""` and `--only ","` must fail fast with a usage error."""
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["use", "vanilla", "--only", ""])
    assert result.exit_code == 2
    assert "at least one tool id" in result.stderr


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


def _dated_profile_name(state_dir: Path) -> str:
    """Read the dated profile name from disk (init creates two: dated + vanilla).

    Bypasses both init's and `list`'s stdout formats, so changes to the
    naming convention or the table renderer don't break these tests.
    """
    return next(p.name for p in FileProfileStore(state_dir).list() if p.name != "vanilla")


def test_delete_with_force(runner: CliRunner, tmp_home: Path, tmp_state: Path) -> None:
    runner.invoke(app, ["init"])
    runner.invoke(app, ["use", "vanilla"])  # switch off the dated profile
    dated = _dated_profile_name(tmp_state)
    result = runner.invoke(app, ["delete", dated, "--force"])
    assert result.exit_code == 0


def test_delete_active_refused(runner: CliRunner, tmp_home: Path, tmp_state: Path) -> None:
    runner.invoke(app, ["init"])
    dated = _dated_profile_name(tmp_state)
    blocked = runner.invoke(app, ["delete", dated, "--force"])
    assert blocked.exit_code == 1
    assert "active for" in blocked.stderr


def test_which_command(runner: CliRunner, tmp_home: Path, tmp_state: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["which", "claude"])
    assert result.exit_code == 0
    assert "current" in result.stdout


def test_tools_scaffold_writes_default_path(runner: CliRunner, tmp_state: Path) -> None:
    result = runner.invoke(app, ["tools", "scaffold", "gemini"])
    assert result.exit_code == 0
    expected = tmp_state / "registry.d" / "gemini.toml"
    assert expected.exists()
    content = expected.read_text()
    assert 'id = "gemini"' in content


def test_tools_scaffold_with_out(runner: CliRunner, tmp_state: Path, tmp_path: Path) -> None:
    out = tmp_path / "custom.toml"
    result = runner.invoke(app, ["tools", "scaffold", "x", "--out", str(out)])
    assert result.exit_code == 0
    assert out.exists()


def test_tools_scaffold_refuses_overwrite(runner: CliRunner, tmp_state: Path) -> None:
    runner.invoke(app, ["tools", "scaffold", "gemini"])
    result = runner.invoke(app, ["tools", "scaffold", "gemini"])
    assert result.exit_code == 1
    assert "overwrite" in result.stderr


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
