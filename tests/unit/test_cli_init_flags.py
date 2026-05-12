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


# --- T14: --interactive -----------------------------------------------------


def test_init_interactive_requires_tty(
    tmp_home: Path, tmp_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-TTY stdin must reject --interactive with a clear error."""
    monkeypatch.setattr("switcher.cli._stdin_is_tty", lambda: False)
    result = runner.invoke(app, ["init", "--interactive"])
    assert result.exit_code != 0
    assert "tty" in _combined(result).lower()


def test_init_interactive_mutually_exclusive_with_only(tmp_home: Path, tmp_state: Path) -> None:
    result = runner.invoke(app, ["init", "--interactive", "--only", "claude"])
    assert result.exit_code != 0
    assert "mutually exclusive" in _combined(result).lower()


def test_init_interactive_mutually_exclusive_with_skip(tmp_home: Path, tmp_state: Path) -> None:
    result = runner.invoke(app, ["init", "--interactive", "--skip", "claude"])
    assert result.exit_code != 0
    assert "mutually exclusive" in _combined(result).lower()


def test_init_interactive_default_yes_captures_all(
    tmp_home: Path, tmp_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty input lines accept the default-Y; all tools captured."""
    monkeypatch.setattr("switcher.cli._stdin_is_tty", lambda: True)
    result = runner.invoke(app, ["init", "--interactive"], input="\n\n")
    assert result.exit_code == 0, _combined(result)
    from switcher.cli import get_deps

    active = get_deps().store.get_active()
    assert "claude" in active and "copilot" in active


def test_init_interactive_all_no_raises_nothing_to_initialize(
    tmp_home: Path, tmp_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("switcher.cli._stdin_is_tty", lambda: True)
    result = runner.invoke(app, ["init", "--interactive"], input="n\nn\n")
    assert result.exit_code != 0
    out = _combined(result).lower()
    assert "nothing to initialize" in out or "every detected tool was skipped" in out


def test_init_skip_excludes_every_registered_tool_errors(tmp_home: Path, tmp_state: Path) -> None:
    """abby review: --skip claude,copilot leaves the target set empty; the
    error must reflect 'skipped everything', not 'requested not installed'."""
    result = runner.invoke(app, ["init", "--skip", "claude,copilot"])
    assert result.exit_code != 0
    out = _combined(result).lower()
    assert "every registered tool" in out or "excluded every" in out
    # And the misleading --only-shaped message must NOT appear.
    assert "requested tools are installed" not in out


def test_init_interactive_some_no_surfaces_skipped(
    tmp_home: Path, tmp_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Y for claude, N for copilot: report shows skipped_via_interactive."""
    monkeypatch.setattr("switcher.cli._stdin_is_tty", lambda: True)
    result = runner.invoke(app, ["init", "--interactive"], input="\nn\n")
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "Captured: claude" in out
    assert "Skipped (via interactive): copilot" in out
    assert "switcher rescan --only copilot" in out


def test_init_skip_excludes_every_detected_tool_errors(
    tmp_home_no_copilot: Path, tmp_state: Path
) -> None:
    """Hermes review: when only some registered tools are installed, --skip
    of those installed tools used to fall through to service.init's generic
    'no requested tools are installed' error — accurate for --only but
    misleading for --skip. The CLI now intersects `target_ids` with
    detect_installed and surfaces a --skip-specific error before reaching
    the service layer.

    Repro: tmp_home_no_copilot has only claude installed. `init --skip claude`
    builds target_ids=["copilot"] (registry minus skip), but copilot isn't
    installed, so the intersection with detected is empty. The dedicated
    'every detected tool' error fires.
    """
    result = runner.invoke(app, ["init", "--skip", "claude"])
    assert result.exit_code != 0
    out = _combined(result).lower()
    assert "every detected tool" in out
    # And the misleading --only-shaped message must NOT appear.
    assert "requested tools are installed" not in out


def test_init_interactive_keyboard_interrupt_exits_cleanly(
    tmp_home: Path, tmp_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hermes review: Ctrl-C during the interactive prompt loop must exit
    cleanly (non-zero, no traceback), NOT bubble up as a raw KeyboardInterrupt
    traceback. The catch lives in handle_errors so it covers every command
    that calls input() directly.
    """
    monkeypatch.setattr("switcher.cli._stdin_is_tty", lambda: True)

    def raising_input(_prompt: str) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", raising_input)
    result = runner.invoke(app, ["init", "--interactive"])
    assert result.exit_code == 130, _combined(result)
    # No raw traceback in the output.
    assert "Traceback" not in _combined(result)
    assert "aborted" in _combined(result).lower()
    # Critical: no service-level mutation should have run — active map
    # is still untouched (init never reached service.init).
    from switcher.cli import get_deps

    assert get_deps().store.get_active() == {}


def test_init_interactive_eof_exits_cleanly(
    tmp_home: Path, tmp_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EOFError counterpart of the Ctrl-C test: stdin closing during the
    prompt also produces a clean exit, not a traceback."""
    monkeypatch.setattr("switcher.cli._stdin_is_tty", lambda: True)

    def raising_input(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", raising_input)
    result = runner.invoke(app, ["init", "--interactive"])
    assert result.exit_code == 130, _combined(result)
    assert "Traceback" not in _combined(result)


def test_init_interactive_zero_detected_distinct_wording(
    tmp_path: Path, tmp_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hermes nit: when --interactive is invoked on a machine with no
    installed tools, the error wording must NOT say 'every detected tool
    was skipped' (there were no detected tools to skip). The CLI now
    distinguishes the zero-detected case with its own message.
    """
    # Empty home — no .claude, no .copilot, no github-copilot.
    home = tmp_path / "home"
    home.mkdir()
    if IS_WINDOWS:
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    else:
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    monkeypatch.setattr("switcher.cli._stdin_is_tty", lambda: True)
    result = runner.invoke(app, ["init", "--interactive"])
    assert result.exit_code != 0
    out = _combined(result).lower()
    assert "no installed tools detected" in out
    # And the misleading "every detected tool was skipped" wording must NOT fire.
    assert "every detected tool was skipped" not in out


def test_init_skip_after_init_raises_already_initialized_not_skip_error(
    tmp_home: Path, tmp_state: Path
) -> None:
    """Hermes blocker: `init --skip claude` on an already-initialized
    repo used to error with `--skip excluded every detected tool` —
    misleading because the real cause is that switcher is already
    initialized. The CLI now preflights state-already-initialized
    BEFORE any flag-specific detection / prompting runs.
    """
    first = runner.invoke(app, ["init"])
    assert first.exit_code == 0, _combined(first)
    second = runner.invoke(app, ["init", "--skip", "claude"])
    assert second.exit_code != 0
    out = _combined(second).lower()
    assert "already initialized" in out
    # The misleading flag-specific wording must NOT appear.
    assert "every detected tool" not in out


def test_init_interactive_after_init_does_not_prompt_and_errors(
    tmp_home: Path, tmp_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hermes blocker: `init --interactive` on an already-initialized repo
    used to prompt the user (and exit with the misleading
    'every detected tool was skipped' if all were declined). The CLI
    preflight must short-circuit before any input() call fires.

    Repro: stub input() to fail loudly so we'd notice if the prompt
    actually ran.
    """
    first = runner.invoke(app, ["init"])
    assert first.exit_code == 0, _combined(first)

    monkeypatch.setattr("switcher.cli._stdin_is_tty", lambda: True)

    def must_not_run(_prompt: str) -> str:
        raise AssertionError(
            "input() must NOT be called: state-already-initialized "
            "preflight should short-circuit before the prompt loop"
        )

    monkeypatch.setattr("builtins.input", must_not_run)

    result = runner.invoke(app, ["init", "--interactive"])
    assert result.exit_code != 0
    out = _combined(result).lower()
    assert "already initialized" in out


def test_init_only_after_init_raises_already_initialized(tmp_home: Path, tmp_state: Path) -> None:
    """Symmetric to the --skip case: `init --only` after init must also
    surface already-initialized, not the per-flag detection error."""
    first = runner.invoke(app, ["init"])
    assert first.exit_code == 0, _combined(first)
    second = runner.invoke(app, ["init", "--only", "copilot"])
    assert second.exit_code != 0
    out = _combined(second).lower()
    assert "already initialized" in out
