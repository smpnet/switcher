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


def _setup(runner: CliRunner, *args: str) -> None:
    """Run a CLI command as setup for another test and assert it succeeded.

    Setup invokes (init, use vanilla, scaffold gemini, ...) that silently
    error would otherwise let the next assertion fail in a misleading way --
    e.g., a broken init would surface as `delete vanilla` returning a path-
    not-found error, hiding the actual init regression.
    """
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, (
        f"setup `{' '.join(args)}` failed (exit {result.exit_code}): {result.stderr}"
    )


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
    _setup(runner, "init")
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "claude" in result.stdout


def test_use_vanilla_after_init(runner: CliRunner, tmp_home: Path, tmp_state: Path) -> None:
    _setup(runner, "init")
    result = runner.invoke(app, ["use", "vanilla"])
    assert result.exit_code == 0
    assert "vanilla" in result.stdout


def test_use_only_filter(runner: CliRunner, tmp_home: Path, tmp_state: Path) -> None:
    _setup(runner, "init")
    result = runner.invoke(app, ["use", "vanilla", "--only", "claude"])
    assert result.exit_code == 0
    assert "claude" in result.stdout


def test_use_only_strips_empty_entries(runner: CliRunner, tmp_home: Path, tmp_state: Path) -> None:
    """Trailing-comma / whitespace shouldn't surface as `unknown tool ''`."""
    _setup(runner, "init")
    result = runner.invoke(app, ["use", "vanilla", "--only", "claude, "])
    assert result.exit_code == 0
    # Output must not contain the empty entry that the naive split would produce
    assert ", ," not in result.stdout
    assert result.stdout.rstrip().endswith("claude")


@pytest.mark.parametrize("arg", ["", ","])
def test_use_only_rejects_empty_list(
    runner: CliRunner, tmp_home: Path, tmp_state: Path, arg: str
) -> None:
    """Both an empty string and a comma-only value must fail with a usage error."""
    _setup(runner, "init")
    result = runner.invoke(app, ["use", "vanilla", "--only", arg])
    assert result.exit_code == 2
    assert "at least one tool id" in result.stderr


def test_create_then_list(runner: CliRunner, tmp_home: Path, tmp_state: Path) -> None:
    _setup(runner, "init")
    result = runner.invoke(app, ["create", "experiment"])
    assert result.exit_code == 0
    listed = runner.invoke(app, ["list"])
    assert "experiment" in listed.stdout


def test_save_command(runner: CliRunner, tmp_home: Path, tmp_state: Path) -> None:
    _setup(runner, "init")
    result = runner.invoke(app, ["save", "snap"])
    assert result.exit_code == 0


def test_rename_command(runner: CliRunner, tmp_home: Path, tmp_state: Path) -> None:
    _setup(runner, "init")
    result = runner.invoke(app, ["rename", "vanilla", "fresh"])
    assert result.exit_code == 0


def _dated_profile_name(state_dir: Path) -> str:
    """Read the dated profile name from disk (init creates two: dated + vanilla).

    Bypasses both init's and `list`'s stdout formats, so changes to the
    naming convention or the table renderer don't break these tests.
    """
    return next(p.name for p in FileProfileStore(state_dir).list() if p.name != "vanilla")


def test_delete_with_force(runner: CliRunner, tmp_home: Path, tmp_state: Path) -> None:
    _setup(runner, "init")
    _setup(runner, "use", "vanilla")  # switch off the dated profile
    dated = _dated_profile_name(tmp_state)
    result = runner.invoke(app, ["delete", dated, "--force"])
    assert result.exit_code == 0


def test_delete_active_refused(runner: CliRunner, tmp_home: Path, tmp_state: Path) -> None:
    _setup(runner, "init")
    dated = _dated_profile_name(tmp_state)
    blocked = runner.invoke(app, ["delete", dated, "--force"])
    assert blocked.exit_code == 1
    assert "active for" in blocked.stderr


def test_which_command(runner: CliRunner, tmp_home: Path, tmp_state: Path) -> None:
    _setup(runner, "init")
    expected = _dated_profile_name(tmp_state)
    result = runner.invoke(app, ["which", "claude"])
    assert result.exit_code == 0
    assert expected in result.stdout


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


def test_tools_scaffold_expands_tilde(runner: CliRunner, tmp_state: Path, tmp_home: Path) -> None:
    """`--out ~/foo.toml` must resolve via expanduser, not be taken literally."""
    result = runner.invoke(app, ["tools", "scaffold", "x", "--out", "~/x.toml"])
    assert result.exit_code == 0
    assert (tmp_home / "x.toml").exists()


def test_tools_scaffold_refuses_overwrite(runner: CliRunner, tmp_state: Path) -> None:
    _setup(runner, "tools", "scaffold", "gemini")
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
