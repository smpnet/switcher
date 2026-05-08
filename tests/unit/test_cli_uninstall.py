"""CLI surface for `switcher uninstall` (spec §3.1, §3.6)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from switcher.cli import app


def test_uninstall_default_runs_and_clears_state(tmp_state: Path, tmp_home: Path) -> None:
    runner = CliRunner()
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, setup.stderr

    result = runner.invoke(app, ["uninstall"])

    assert result.exit_code == 0, result.stderr
    # Per spec §3.6: per-mapping line uses "unlink <path> ..."
    assert "unlink" in result.stderr
    # Footer: spec §3.6 — non-purge default keeps state, clears active map.
    assert "cleared active map" in result.stderr


def test_uninstall_dry_run_makes_no_changes(tmp_state: Path, tmp_home: Path) -> None:
    runner = CliRunner()
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, setup.stderr

    result = runner.invoke(app, ["uninstall", "--dry-run"])

    assert result.exit_code == 0, result.stderr
    assert "would unlink" in result.stderr
    # State dir untouched.
    assert (tmp_state / "config.json").exists()


def test_uninstall_purge_in_ci_without_yes_errors(
    tmp_state: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    runner = CliRunner()
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, setup.stderr

    result = runner.invoke(app, ["uninstall", "--purge"])

    assert result.exit_code != 0
    assert "non-interactive" in result.stderr
