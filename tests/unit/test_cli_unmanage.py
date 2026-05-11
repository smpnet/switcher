# pyright: reportPrivateUsage=none
"""Spec §2.2 — switcher unmanage <tool>."""

from __future__ import annotations

import os
from pathlib import Path

from typer.testing import CliRunner

from switcher.cli import app

runner = CliRunner()


def _combined(result: object) -> str:
    stdout = getattr(result, "stdout", "") or ""
    stderr = getattr(result, "stderr", "") or ""
    return stdout + stderr


def _home() -> Path:
    return Path(os.environ.get("HOME", os.environ.get("USERPROFILE", "")))


def test_unmanage_happy_path(tmp_home: Path, tmp_state: Path) -> None:
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, _combined(setup)

    result = runner.invoke(app, ["unmanage", "claude"])
    assert result.exit_code == 0, _combined(result)

    from switcher.cli import get_deps

    assert "claude" not in get_deps().store.get_active()
    # Live path is a real dir, not a symlink.
    claude_path = _home() / ".claude"
    assert claude_path.is_dir()
    assert not claude_path.is_symlink()


def test_unmanage_drops_cache_entry(tmp_home: Path, tmp_state: Path) -> None:
    """active_live_paths cache must drop the unmanaged tool's entry."""
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, _combined(setup)

    result = runner.invoke(app, ["unmanage", "claude"])
    assert result.exit_code == 0, _combined(result)

    from switcher.cli import get_deps

    cache = get_deps().store.get_active_live_paths()
    assert "claude" not in cache


def test_unmanage_dry_run_no_changes(tmp_home: Path, tmp_state: Path) -> None:
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, _combined(setup)

    result = runner.invoke(app, ["unmanage", "claude", "--dry-run"])
    assert result.exit_code == 0, _combined(result)
    assert "Would unmanage" in result.stdout

    from switcher.cli import get_deps

    # Still managed.
    assert "claude" in get_deps().store.get_active()
    # Symlink still in place.
    assert (_home() / ".claude").is_symlink()


def test_unmanage_unknown_tool_raises(tmp_home: Path, tmp_state: Path) -> None:
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, _combined(setup)

    result = runner.invoke(app, ["unmanage", "nonexistent"])
    assert result.exit_code != 0
    out = _combined(result).lower()
    assert "not managed" in out


def test_unmanage_then_use_does_not_remanage(tmp_home: Path, tmp_state: Path) -> None:
    """Durability check: after unmanage, use(vanilla) must NOT re-activate
    the unmanaged tool (validates T10's use() filter)."""
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, _combined(setup)

    unmanage_result = runner.invoke(app, ["unmanage", "claude"])
    assert unmanage_result.exit_code == 0, _combined(unmanage_result)

    use_result = runner.invoke(app, ["use", "vanilla"])
    assert use_result.exit_code == 0, _combined(use_result)

    from switcher.cli import get_deps

    assert "claude" not in get_deps().store.get_active()
    # Live dir still a real directory — no resurrection.
    assert (_home() / ".claude").is_dir()
    assert not (_home() / ".claude").is_symlink()


def test_unmanage_when_uninitialized_errors(tmp_home: Path, tmp_state: Path) -> None:
    """Before init, unmanage must surface state-not-initialized, not crash."""
    result = runner.invoke(app, ["unmanage", "claude"])
    assert result.exit_code != 0
    out = _combined(result).lower()
    assert "not been initialized" in out or "switcher init" in out
