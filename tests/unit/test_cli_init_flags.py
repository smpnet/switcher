# pyright: reportPrivateUsage=none
"""Spec §2.1 — init --only / --skip / --interactive (+ InitReport surface)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from switcher.cli import app

IS_WINDOWS = sys.platform == "win32"

runner = CliRunner()


@pytest.fixture
def tmp_home_no_copilot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Like `tmp_home` but with no Copilot live dirs seeded.

    Used by tests that need --only/--skip to surface "requested but not
    detected" for copilot. The post-T4 builtin only references
    ~/.copilot (or %USERPROFILE%\\.copilot on Windows); not creating
    that dir is sufficient to make detect_installed() miss copilot.
    """
    home = tmp_path / "home"
    home.mkdir()
    if IS_WINDOWS:
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
        (home / ".claude").mkdir()
    else:
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        (home / ".claude").mkdir()
    return home


def _combined(result: object) -> str:
    """Concatenate stdout + stderr for substring assertions.

    Click 8.3 separates stdout and stderr by default; error messages from
    typer.BadParameter / handle_errors go to stderr. Tests that don't
    care which stream the message lands on use this helper.
    """
    stdout = getattr(result, "stdout", "") or ""
    stderr = getattr(result, "stderr", "") or ""
    return stdout + stderr


# --- T13: --only / --skip ----------------------------------------------------


def test_init_only_captures_only_listed_tools(tmp_home: Path, tmp_state: Path) -> None:
    result = runner.invoke(app, ["init", "--only", "copilot"])
    assert result.exit_code == 0, _combined(result)
    from switcher.cli import get_deps

    deps = get_deps()
    active = deps.store.get_active()
    assert set(active.keys()) == {"copilot"}


def test_init_skip_excludes_listed_tools(tmp_home: Path, tmp_state: Path) -> None:
    result = runner.invoke(app, ["init", "--skip", "claude"])
    assert result.exit_code == 0, _combined(result)
    from switcher.cli import get_deps

    deps = get_deps()
    assert "claude" not in deps.store.get_active()
    assert "copilot" in deps.store.get_active()


def test_init_only_and_skip_mutually_exclusive(tmp_home: Path, tmp_state: Path) -> None:
    result = runner.invoke(app, ["init", "--only", "copilot", "--skip", "claude"])
    assert result.exit_code != 0
    assert "mutually exclusive" in _combined(result).lower()


def test_init_only_empty_value_rejected(tmp_home: Path, tmp_state: Path) -> None:
    result = runner.invoke(app, ["init", "--only", ""])
    assert result.exit_code != 0
    assert "at least one tool" in _combined(result).lower()


def test_init_skip_empty_value_rejected(tmp_home: Path, tmp_state: Path) -> None:
    result = runner.invoke(app, ["init", "--skip", ""])
    assert result.exit_code != 0
    assert "at least one tool" in _combined(result).lower()


def test_init_only_unknown_tool_hard_errors(tmp_home: Path, tmp_state: Path) -> None:
    result = runner.invoke(app, ["init", "--only", "clause"])
    assert result.exit_code != 0
    out = _combined(result).lower()
    assert "did you mean" in out
    assert "claude" in out


def test_init_only_requested_not_installed_raises(
    tmp_home_no_copilot: Path, tmp_state: Path
) -> None:
    """When --only X but X is registered yet not installed, raise
    NothingToInitializeError."""
    result = runner.invoke(app, ["init", "--only", "copilot"])
    assert result.exit_code != 0
    out = _combined(result).lower()
    assert "not installed" in out or "nothing to initialize" in out


def test_init_only_partial_match_surfaces_missing_tool(
    tmp_home_no_copilot: Path, tmp_state: Path
) -> None:
    """--only claude,copilot when only claude installed: capture claude,
    surface copilot as requested-but-not-detected."""
    result = runner.invoke(app, ["init", "--only", "claude,copilot"])
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "Captured: claude" in out
    assert "Requested but not detected: copilot" in out
    assert "switcher rescan --only copilot" in out


def test_init_skip_surfaces_skipped_tool(tmp_home: Path, tmp_state: Path) -> None:
    result = runner.invoke(app, ["init", "--skip", "claude"])
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "Skipped: claude" in out
    assert "switcher rescan --only claude" in out


def test_init_returns_init_report(tmp_home: Path, tmp_state: Path) -> None:
    """service.init() must return an InitReport with the expected fields."""
    from switcher.cli import get_deps
    from switcher.service import InitReport

    deps = get_deps()
    report = deps.service.init()
    assert isinstance(report, InitReport)
    assert report.profile_name.endswith("-current")
    assert set(report.captured) == {"claude", "copilot"}
    assert report.requested_but_not_installed == []
    assert report.skipped_via_skip_flag == []
    assert report.skipped_via_interactive == []


def test_init_bare_still_succeeds_with_no_filter(tmp_home: Path, tmp_state: Path) -> None:
    """Regression: bare `init` (no flags) preserves the v0.1.3 happy path."""
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, _combined(result)
    assert "Initialized profile" in result.stdout
