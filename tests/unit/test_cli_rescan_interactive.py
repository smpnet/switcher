# pyright: reportPrivateUsage=none
"""Spec §3.3 — rescan auto-interactive on TTY."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from switcher.cli import app, get_deps

runner = CliRunner()


def _combined(result: Result) -> str:
    """Concatenate stdout + stderr for failure messages on setup asserts."""
    stdout = getattr(result, "stdout", "") or ""
    stderr = getattr(result, "stderr", "") or ""
    return f"stdout:\n{stdout}\nstderr:\n{stderr}"


def _setup_init_only_copilot() -> None:
    init_result = runner.invoke(app, ["init", "--only", "copilot"])
    assert init_result.exit_code == 0, _combined(init_result)


def _setup_create(name: str) -> None:
    create_result = runner.invoke(app, ["create", name])
    assert create_result.exit_code == 0, _combined(create_result)


def test_rescan_all_flag_captures_all(tmp_home: Path, tmp_state: Path) -> None:
    _setup_init_only_copilot()
    result = runner.invoke(app, ["rescan", "--all"])
    assert result.exit_code == 0
    assert "claude" in get_deps().store.get_active()


def test_rescan_only_unchanged_behavior(tmp_home: Path, tmp_state: Path) -> None:
    _setup_init_only_copilot()
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
    """init-only-copilot leaves claude AND codex unmanaged; rescan prompts for
    each in registry order (alphabetical-by-filename: claude → codex). Two
    default-Y answers capture both."""
    _setup_init_only_copilot()
    monkeypatch.setattr("switcher.cli._stdin_is_tty", lambda: True)
    result = runner.invoke(app, ["rescan"], input="\n\n")
    assert result.exit_code == 0, result.output
    active = get_deps().store.get_active()
    assert "claude" in active and "codex" in active


def test_rescan_tty_prompt_rejects_all(
    tmp_home: Path, tmp_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two `n` answers reject both unmanaged tools."""
    _setup_init_only_copilot()
    monkeypatch.setattr("switcher.cli._stdin_is_tty", lambda: True)
    result = runner.invoke(app, ["rescan"], input="n\nn\n")
    assert result.exit_code == 0, result.output
    active = get_deps().store.get_active()
    assert "claude" not in active and "codex" not in active


def test_rescan_non_tty_warns_and_captures_all(
    tmp_home: Path, tmp_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_init_only_copilot()
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
    _setup_init_only_copilot()
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
    _setup_init_only_copilot()
    monkeypatch.setattr("switcher.cli._stdin_is_tty", lambda: True)
    r1 = runner.invoke(app, ["rescan", "--dry-run"], input="")
    r2 = runner.invoke(app, ["rescan", "--all", "--dry-run"])
    assert r1.exit_code == 0 and r2.exit_code == 0
    assert "claude" in r1.output
    assert "claude" in r2.output


# --- --into regression coverage (consultant finding: must not drop --into) ---


def test_rescan_into_with_only_captures_into_named_profile(tmp_home: Path, tmp_state: Path) -> None:
    """--into P --only X captures X into the existing profile P."""
    _setup_init_only_copilot()
    _setup_create("shared")
    result = runner.invoke(app, ["rescan", "--into", "shared", "--only", "claude"])
    assert result.exit_code == 0, result.output
    assert get_deps().store.get_active().get("claude") == "shared"


def test_rescan_into_with_all_captures_all_into_named_profile(
    tmp_home: Path, tmp_state: Path
) -> None:
    _setup_init_only_copilot()
    _setup_create("shared")
    result = runner.invoke(app, ["rescan", "--into", "shared", "--all"])
    assert result.exit_code == 0, result.output
    assert get_deps().store.get_active().get("claude") == "shared"


def test_rescan_into_with_dry_run_no_mutation(tmp_home: Path, tmp_state: Path) -> None:
    _setup_init_only_copilot()
    _setup_create("shared")
    result = runner.invoke(app, ["rescan", "--into", "shared", "--dry-run"])
    assert result.exit_code == 0, result.output
    # claude should still be unmanaged after a dry-run.
    assert "claude" not in get_deps().store.get_active()


def test_rescan_keyboard_interrupt_during_prompt_exits_cleanly(
    tmp_home: Path, tmp_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hermes review: Ctrl-C during bare-rescan's per-tool prompt must
    exit cleanly. Without the handle_errors KeyboardInterrupt branch, the
    raw traceback would surface to the user.
    """
    _setup_init_only_copilot()
    monkeypatch.setattr("switcher.cli._stdin_is_tty", lambda: True)

    def raising_input(_prompt: str) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", raising_input)
    result = runner.invoke(app, ["rescan"])
    assert result.exit_code == 130, result.output
    assert "Traceback" not in result.output
    out = (result.output + (result.stderr or "")).lower()
    assert "aborted" in out
    # No mutation: claude was unmanaged before, still not in active.
    assert "claude" not in get_deps().store.get_active()


def test_rescan_pre_init_fails_before_prompt_or_warning(
    tmp_home: Path, tmp_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hermes review: bare `rescan` on an uninitialized machine must
    fail with StateNotInitializedError BEFORE the new TTY prompt or
    non-TTY warning fires. Otherwise the user sees 'capturing all
    detected unmanaged tools' guidance, then the command fails — a
    misleading regression vs the pre-v0.1.4 flow."""
    monkeypatch.setattr("switcher.cli._stdin_is_tty", lambda: False)
    result = runner.invoke(app, ["rescan"])
    assert result.exit_code != 0
    out = (result.output + (result.stderr or "")).lower()
    assert "has not been initialized" in out or "switcher init" in out
    # No premature capture-all warning before the init guard fires.
    assert "warning" not in out
    assert "capturing all" not in out


def test_rescan_pre_init_fails_before_tty_prompt(
    tmp_home: Path, tmp_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TTY counterpart to the Hermes finding: must NOT prompt before
    surfacing the not-initialized error."""
    monkeypatch.setattr("switcher.cli._stdin_is_tty", lambda: True)
    # Pass empty input — if the prompt fires before the init check,
    # input() will raise EOFError and the test fails noisily, which is
    # still informative. The exit_code != 0 assertion catches both.
    result = runner.invoke(app, ["rescan"], input="")
    assert result.exit_code != 0
    out = (result.output + (result.stderr or "")).lower()
    assert "has not been initialized" in out or "switcher init" in out
    assert "detected unmanaged tools" not in out


def test_rescan_into_tty_prompt_path(
    tmp_home: Path, tmp_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--into P with no --only/--all on TTY: prompt the user, then capture
    accepted tools into P. Two unmanaged tools (claude + codex) → two answers."""
    _setup_init_only_copilot()
    _setup_create("shared")
    monkeypatch.setattr("switcher.cli._stdin_is_tty", lambda: True)
    result = runner.invoke(app, ["rescan", "--into", "shared"], input="\n\n")
    assert result.exit_code == 0, result.output
    active = get_deps().store.get_active()
    assert active.get("claude") == "shared"
    assert active.get("codex") == "shared"
