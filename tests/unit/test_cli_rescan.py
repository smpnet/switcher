"""CLI surface for `switcher rescan` (spec §4.1, §4.5)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from typer.testing import CliRunner

from switcher.cli import app
from switcher.links import remove_link
from switcher.paths import IS_WINDOWS

# Copilot's single config dir — see src/switcher/builtins/copilot.toml.
# Same path on POSIX (`~/.copilot`) and on Windows (`%USERPROFILE%\.copilot`,
# which expands to `~/.copilot` under the test home fixture).
COPILOT_DIR = Path(".copilot")


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
    # Recreate copilot's config dir — service.rescan detects the tool via
    # its single config dir.
    (tmp_home / COPILOT_DIR).mkdir(parents=True)
    result = runner.invoke(app, ["rescan", "--dry-run"])
    assert result.exit_code == 0, result.stderr
    # Dry-run uses present-tense "would capture" (not the past-tense
    # "would captured" produced by the prior prefix-then-verb shape).
    assert "would capture" in result.stderr
    assert "would captured" not in result.stderr
    # Live dir untouched (dry run): not a symlink/junction.
    assert not _is_link(tmp_home / COPILOT_DIR)


def test_rescan_default_captures_new_tool(tmp_state: Path, tmp_home: Path) -> None:
    runner = CliRunner()
    _suppress_copilot(tmp_home)
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, setup.stderr
    (tmp_home / COPILOT_DIR).mkdir(parents=True)
    result = runner.invoke(app, ["rescan"])
    assert result.exit_code == 0, result.stderr
    # Real run uses past-tense "captured", never the dry-run "would" prefix.
    # Pins the non-dry-run branch of the same conditional that produced the
    # original "would captured" typo, so the regression can't reappear on
    # either side.
    assert "captured copilot" in result.stderr
    assert "would capture" not in result.stderr


def test_rescan_only_empty_string_is_rejected(tmp_state: Path, tmp_home: Path) -> None:
    """Mirror `use --only`: explicitly empty `--only ""` must error rather
    than silently behaving like bare `rescan` (Hermes review)."""
    runner = CliRunner()
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, setup.stderr
    result = runner.invoke(app, ["rescan", "--only", ""])
    assert result.exit_code != 0
    assert "at least one tool id" in result.stderr or "at least one tool id" in result.output
