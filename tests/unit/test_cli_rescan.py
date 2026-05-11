"""CLI surface for `switcher rescan` (spec §4.1, §4.5)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from typer.testing import CliRunner

from switcher.cli import app
from switcher.links import remove_link
from switcher.paths import IS_WINDOWS

# Copilot's two config dirs differ between platforms — see
# src/switcher/builtins/copilot.toml. Tests that suppress + recreate copilot
# need the platform-appropriate first dir so service.detect_installed (which
# checks the FIRST config dir only) sees the tool.
COPILOT_FIRST_DIR = (
    Path("AppData/Local/github-copilot") if IS_WINDOWS else Path(".config/github-copilot")
)
COPILOT_SECOND_DIR = Path(".copilot")


def _is_link(p: Path) -> bool:
    return p.is_symlink() or (IS_WINDOWS and os.path.isjunction(p))


def _remove_path(p: Path) -> None:
    """Link-aware removal helper (mirrors test_rescan.py)."""
    if not p.exists() and not p.is_symlink():
        return
    if _is_link(p):
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


def _suppress_copilot(tmp_home: Path) -> None:
    """Remove copilot's live dirs across BOTH platforms so init won't capture
    copilot via the platform-appropriate first config dir. The not-applicable
    path on each platform is a no-op."""
    for sub in [".copilot", ".config/github-copilot", "AppData/Local/github-copilot"]:
        _remove_path(tmp_home / sub)


def test_rescan_dry_run_makes_no_changes(tmp_state: Path, tmp_home: Path) -> None:
    runner = CliRunner()
    _suppress_copilot(tmp_home)
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, setup.stderr
    # Recreate copilot's first config dir per platform — service.rescan
    # detects the tool via its FIRST config dir only.
    (tmp_home / COPILOT_FIRST_DIR).mkdir(parents=True)
    (tmp_home / COPILOT_SECOND_DIR).mkdir()
    result = runner.invoke(app, ["rescan", "--dry-run"])
    assert result.exit_code == 0, result.stderr
    assert "would" in result.stderr
    # Live dirs untouched (dry run): not a symlink/junction.
    assert not _is_link(tmp_home / COPILOT_SECOND_DIR)
    assert not _is_link(tmp_home / COPILOT_FIRST_DIR)


def test_rescan_default_captures_new_tool(tmp_state: Path, tmp_home: Path) -> None:
    runner = CliRunner()
    _suppress_copilot(tmp_home)
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, setup.stderr
    (tmp_home / COPILOT_FIRST_DIR).mkdir(parents=True)
    (tmp_home / COPILOT_SECOND_DIR).mkdir()
    result = runner.invoke(app, ["rescan"])
    assert result.exit_code == 0, result.stderr
    assert "captured copilot" in result.stderr


def test_rescan_only_empty_string_is_rejected(tmp_state: Path, tmp_home: Path) -> None:
    """Mirror `use --only`: explicitly empty `--only ""` must error rather
    than silently behaving like bare `rescan` (Hermes review)."""
    runner = CliRunner()
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, setup.stderr
    result = runner.invoke(app, ["rescan", "--only", ""])
    assert result.exit_code != 0
    assert "at least one tool id" in result.stderr or "at least one tool id" in result.output
