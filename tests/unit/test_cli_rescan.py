"""CLI surface for `switcher rescan` (spec §4.1, §4.5)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from typer.testing import CliRunner

from switcher.cli import app
from switcher.links import remove_link
from switcher.paths import IS_WINDOWS


def _remove_path(p: Path) -> None:
    """Link-aware removal helper (mirrors test_rescan.py)."""
    if not p.exists() and not p.is_symlink():
        return
    if p.is_symlink() or (IS_WINDOWS and os.path.isjunction(p)):
        remove_link(p)
    elif p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()


def test_rescan_no_new_tools_exits_zero(tmp_state: Path, tmp_home: Path) -> None:
    runner = CliRunner()
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, setup.stderr
    result = runner.invoke(app, ["rescan"])
    assert result.exit_code == 0, result.stderr
    assert "no new tools detected" in result.stderr


def test_rescan_dry_run_makes_no_changes(tmp_state: Path, tmp_home: Path) -> None:
    runner = CliRunner()
    # Suppress copilot from init by removing its config dirs first.
    for sub in [".copilot", ".config/github-copilot"]:
        _remove_path(tmp_home / sub)
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, setup.stderr
    (tmp_home / ".copilot").mkdir()
    (tmp_home / ".config" / "github-copilot").mkdir()
    result = runner.invoke(app, ["rescan", "--dry-run"])
    assert result.exit_code == 0, result.stderr
    assert "would" in result.stderr
    # Live dirs untouched (dry run).
    assert not (tmp_home / ".copilot").is_symlink()
    assert not (tmp_home / ".config" / "github-copilot").is_symlink()


def test_rescan_default_captures_new_tool(tmp_state: Path, tmp_home: Path) -> None:
    runner = CliRunner()
    for sub in [".copilot", ".config/github-copilot"]:
        _remove_path(tmp_home / sub)
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, setup.stderr
    (tmp_home / ".copilot").mkdir()
    (tmp_home / ".config" / "github-copilot").mkdir()
    result = runner.invoke(app, ["rescan"])
    assert result.exit_code == 0, result.stderr
    assert "captured copilot" in result.stderr
