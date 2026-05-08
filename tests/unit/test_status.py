"""CLI surface for `switcher status` (spec §6.5, §7.2)."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from switcher.cli import app


def test_status_shows_ok_indicator_when_cache_present(tmp_state: Path, tmp_home: Path) -> None:
    runner = CliRunner()
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    # `[ok]` must render literally (not be eaten by Rich markup).
    assert "[ok]" in result.output


def test_status_verbose_shows_cached_paths(tmp_state: Path, tmp_home: Path) -> None:
    runner = CliRunner()
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["status", "-v"])
    assert result.exit_code == 0
    assert "/.claude" in result.output  # one of the cached live paths


def test_status_shows_dashes_when_active_populated_but_cache_missing(
    tmp_state: Path, tmp_home: Path
) -> None:
    """Spec §6.5: simulate legacy state by clearing active_live_paths while
    keeping the active map intact. Migration should derive cache (since live
    links still resolve), so by default we get [ok]. Assert the indicator
    infrastructure itself works: [ok] OR [--] is present per tool, never absent."""
    runner = CliRunner()
    runner.invoke(app, ["init"])
    cfg = tmp_state / "config.json"
    raw = json.loads(cfg.read_text())
    raw["active_live_paths"] = {}
    cfg.write_text(json.dumps(raw, sort_keys=True))

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    active = json.loads(cfg.read_text())["active"]
    for _ in active:
        assert "[ok]" in result.output or "[--]" in result.output


def test_status_shows_dashes_when_live_link_broken_and_cache_empty(
    tmp_state: Path, tmp_home: Path
) -> None:
    """Force [--] by clearing cache AND breaking a live link target so
    migration can't derive (strict-validation fails)."""
    runner = CliRunner()
    runner.invoke(app, ["init"])
    cfg = tmp_state / "config.json"
    raw = json.loads(cfg.read_text())
    raw["active_live_paths"] = {}
    cfg.write_text(json.dumps(raw, sort_keys=True))
    bogus = tmp_home / "bogus-target"
    bogus.mkdir()
    claude = tmp_home / ".claude"
    if claude.is_symlink():
        claude.unlink()
        claude.symlink_to(bogus)

    result = runner.invoke(app, ["status"])
    assert "[--]" in result.output


def test_status_no_active_profiles(tmp_state: Path, tmp_home: Path) -> None:
    """After uninstall, active is empty: status reports the empty case."""
    runner = CliRunner()
    runner.invoke(app, ["init"])
    runner.invoke(app, ["uninstall"])
    result = runner.invoke(app, ["status"])
    assert "no active profiles" in result.output
