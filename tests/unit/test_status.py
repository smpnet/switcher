"""CLI surface for `switcher status` (spec §6.5, §7.2)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from typer.testing import CliRunner

from switcher.cli import app
from switcher.paths import IS_WINDOWS


def test_status_shows_ok_indicator_when_cache_present(tmp_state: Path, tmp_home: Path) -> None:
    runner = CliRunner()
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, setup.stderr
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    # `[ok]` must render literally (not be eaten by Rich markup).
    assert "[ok]" in result.output


def test_status_verbose_shows_cached_paths(tmp_state: Path, tmp_home: Path) -> None:
    runner = CliRunner()
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, setup.stderr
    result = runner.invoke(app, ["status", "-v"])
    assert result.exit_code == 0
    # Use os.sep so the assertion holds on Windows (`\.claude`) too. The
    # status output must also not wrap the path mid-string under narrow CI
    # terminals — guarded by `soft_wrap=True` in the CLI.
    assert f"{os.sep}.claude" in result.output  # one of the cached live paths


def test_status_shows_dashes_when_active_populated_but_cache_missing(
    tmp_state: Path, tmp_home: Path
) -> None:
    """Spec §6.5: status reads the RAW persisted cache, not the derived view.
    Clearing active_live_paths on disk must produce `[--]` for every active
    tool — even if migration *could* re-derive the cache from live links —
    so operators see the on-disk cache state when diagnosing uninstall."""
    runner = CliRunner()
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, setup.stderr
    cfg = tmp_state / "config.json"
    raw = json.loads(cfg.read_text())
    raw["active_live_paths"] = {}
    cfg.write_text(json.dumps(raw, sort_keys=True))

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    active = json.loads(cfg.read_text())["active"]
    # Every active tool gets `[--]`; raw cache is empty by construction.
    assert result.output.count("[--]") == len(active)
    assert "[ok]" not in result.output


def test_status_shows_dashes_when_live_link_broken_and_cache_empty(
    tmp_state: Path, tmp_home: Path
) -> None:
    """Force [--] by clearing cache AND breaking a live link target so
    migration can't derive (strict-validation fails)."""
    runner = CliRunner()
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, setup.stderr
    cfg = tmp_state / "config.json"
    raw = json.loads(cfg.read_text())
    raw["active_live_paths"] = {}
    cfg.write_text(json.dumps(raw, sort_keys=True))
    bogus = tmp_home / "bogus-target"
    bogus.mkdir()
    claude = tmp_home / ".claude"
    is_junction = IS_WINDOWS and os.path.isjunction(claude)
    if claude.is_symlink() or is_junction:
        if is_junction:
            claude.rmdir()
        else:
            claude.unlink()
        claude.symlink_to(bogus, target_is_directory=True)

    result = runner.invoke(app, ["status"])
    assert "[--]" in result.output


def test_status_no_active_profiles(tmp_state: Path, tmp_home: Path) -> None:
    """After uninstall, active is empty: status reports the empty case."""
    runner = CliRunner()
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, setup.stderr
    uninstall = runner.invoke(app, ["uninstall"])
    assert uninstall.exit_code == 0, uninstall.stderr
    result = runner.invoke(app, ["status"])
    assert "no active profiles" in result.output
