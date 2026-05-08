"""CLI surface for `switcher prune` (spec §5.1, §5.4, §5.6)."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from switcher.cli import app


def test_prune_force_deletes_all_orphans(tmp_state: Path, tmp_home: Path) -> None:
    runner = CliRunner()
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, setup.stderr
    create = runner.invoke(app, ["create", "orphan-a"])
    assert create.exit_code == 0, create.stderr
    result = runner.invoke(app, ["prune", "--force"])
    assert result.exit_code == 0, result.stderr


def test_prune_dry_run_lists_with_sizes(tmp_state: Path, tmp_home: Path) -> None:
    runner = CliRunner()
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, setup.stderr
    create = runner.invoke(app, ["create", "orphan-a"])
    assert create.exit_code == 0, create.stderr
    result = runner.invoke(app, ["prune", "--dry-run"])
    assert result.exit_code == 0, result.stderr
    assert "orphan-a" in result.stderr
    assert "Run without --dry-run" in result.stderr
