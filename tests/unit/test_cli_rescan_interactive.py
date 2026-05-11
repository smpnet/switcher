# pyright: reportPrivateUsage=none
"""Spec §3.3 — rescan auto-interactive on TTY."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from switcher.cli import app, get_deps

runner = CliRunner()


def test_rescan_all_flag_captures_all(tmp_home: Path, tmp_state: Path) -> None:
    runner.invoke(app, ["init", "--only", "copilot"])
    result = runner.invoke(app, ["rescan", "--all"])
    assert result.exit_code == 0
    assert "claude" in get_deps().store.get_active()


def test_rescan_only_unchanged_behavior(tmp_home: Path, tmp_state: Path) -> None:
    runner.invoke(app, ["init", "--only", "copilot"])
    result = runner.invoke(app, ["rescan", "--only", "claude"])
    assert result.exit_code == 0
    assert "claude" in get_deps().store.get_active()


def test_rescan_all_and_only_mutually_exclusive(tmp_home: Path, tmp_state: Path) -> None:
    result = runner.invoke(app, ["rescan", "--all", "--only", "claude"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower()


def test_rescan_tty_prompt_accepts_all(
    tmp_home: Path, tmp_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner.invoke(app, ["init", "--only", "copilot"])
    monkeypatch.setattr("switcher.cli._stdin_is_tty", lambda: True)
    result = runner.invoke(app, ["rescan"], input="\n")  # default-Y for claude
    assert result.exit_code == 0, result.output
    assert "claude" in get_deps().store.get_active()


def test_rescan_tty_prompt_rejects_all(
    tmp_home: Path, tmp_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner.invoke(app, ["init", "--only", "copilot"])
    monkeypatch.setattr("switcher.cli._stdin_is_tty", lambda: True)
    result = runner.invoke(app, ["rescan"], input="n\n")
    assert result.exit_code == 0, result.output
    assert "claude" not in get_deps().store.get_active()


def test_rescan_non_tty_warns_and_captures_all(
    tmp_home: Path, tmp_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner.invoke(app, ["init", "--only", "copilot"])
    monkeypatch.setattr("switcher.cli._stdin_is_tty", lambda: False)
    result = runner.invoke(app, ["rescan"])
    assert result.exit_code == 0, result.output
    combined = (result.output + (result.stderr or "")).lower()
    assert "warning" in combined
    assert "claude" in get_deps().store.get_active()


def test_rescan_dry_run_does_not_prompt_on_tty(
    tmp_home: Path, tmp_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--dry-run must never prompt, regardless of TTY status."""
    runner.invoke(app, ["init", "--only", "copilot"])
    monkeypatch.setattr("switcher.cli._stdin_is_tty", lambda: True)
    # Provide NO input — if dry-run were to prompt, the test would hang
    # or fail on EOF.
    result = runner.invoke(app, ["rescan", "--dry-run"], input="")
    assert result.exit_code == 0, result.output
    # The preview output should mention what would be captured.
    assert "would capture" in result.output.lower() or "claude" in result.output
    # And the state must NOT have changed.
    assert "claude" not in get_deps().store.get_active()


def test_rescan_all_dry_run_equivalent_to_bare_dry_run(
    tmp_home: Path, tmp_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--all --dry-run and bare --dry-run produce identical capture sets
    (both preview-all, no prompts, no mutation)."""
    runner.invoke(app, ["init", "--only", "copilot"])
    monkeypatch.setattr("switcher.cli._stdin_is_tty", lambda: True)
    r1 = runner.invoke(app, ["rescan", "--dry-run"], input="")
    r2 = runner.invoke(app, ["rescan", "--all", "--dry-run"])
    assert r1.exit_code == 0 and r2.exit_code == 0
    assert "claude" in r1.output
    assert "claude" in r2.output


# --- --into regression coverage (consultant finding: must not drop --into) ---


def test_rescan_into_with_only_captures_into_named_profile(tmp_home: Path, tmp_state: Path) -> None:
    """--into P --only X captures X into the existing profile P."""
    runner.invoke(app, ["init", "--only", "copilot"])
    runner.invoke(app, ["create", "shared"])
    result = runner.invoke(app, ["rescan", "--into", "shared", "--only", "claude"])
    assert result.exit_code == 0, result.output
    assert get_deps().store.get_active().get("claude") == "shared"


def test_rescan_into_with_all_captures_all_into_named_profile(
    tmp_home: Path, tmp_state: Path
) -> None:
    runner.invoke(app, ["init", "--only", "copilot"])
    runner.invoke(app, ["create", "shared"])
    result = runner.invoke(app, ["rescan", "--into", "shared", "--all"])
    assert result.exit_code == 0, result.output
    assert get_deps().store.get_active().get("claude") == "shared"


def test_rescan_into_with_dry_run_no_mutation(tmp_home: Path, tmp_state: Path) -> None:
    runner.invoke(app, ["init", "--only", "copilot"])
    runner.invoke(app, ["create", "shared"])
    result = runner.invoke(app, ["rescan", "--into", "shared", "--dry-run"])
    assert result.exit_code == 0, result.output
    # claude should still be unmanaged after a dry-run.
    assert "claude" not in get_deps().store.get_active()


def test_rescan_into_tty_prompt_path(
    tmp_home: Path, tmp_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--into P with no --only/--all on TTY: prompt the user, then capture
    accepted tools into P."""
    runner.invoke(app, ["init", "--only", "copilot"])
    runner.invoke(app, ["create", "shared"])
    monkeypatch.setattr("switcher.cli._stdin_is_tty", lambda: True)
    result = runner.invoke(app, ["rescan", "--into", "shared"], input="\n")
    assert result.exit_code == 0, result.output
    assert get_deps().store.get_active().get("claude") == "shared"
