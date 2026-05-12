# pyright: reportPrivateUsage=none
"""Spec §2.3 — switcher tools shows Installed + Managed columns.

Spec §3.5 — status command empty-active-map message.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from click.testing import Result
from typer.testing import CliRunner

from switcher.cli import _build_tools_table_rows, app, get_deps
from switcher.paths import IS_WINDOWS

runner = CliRunner()


def _combined(result: Result) -> str:
    """Concatenate stdout + stderr for failure messages on setup asserts."""
    stdout = getattr(result, "stdout", "") or ""
    stderr = getattr(result, "stderr", "") or ""
    return f"stdout:\n{stdout}\nstderr:\n{stderr}"


def _drop_link_or_dir(p: Path) -> None:
    """Link-aware removal: handles Windows junctions, POSIX symlinks, and
    real directories. shutil.rmtree raises on a junction/symlink; tests
    that 'undo init' need this dispatch."""
    if IS_WINDOWS and os.path.isjunction(p):
        p.rmdir()
        return
    if p.is_symlink():
        p.unlink()
        return
    if p.is_dir():
        shutil.rmtree(p)


def test_tools_rows_pre_init_all_unmanaged(tmp_home: Path, tmp_state: Path) -> None:
    rows = _build_tools_table_rows(get_deps())
    ids = {r.tool_id for r in rows}
    assert {"claude", "copilot"} <= ids
    for r in rows:
        assert r.managed is False, f"{r.tool_id} should be unmanaged pre-init"


def test_tools_rows_after_init_both_managed(tmp_home: Path, tmp_state: Path) -> None:
    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, _combined(init_result)
    rows = _build_tools_table_rows(get_deps())
    by_id = {r.tool_id: r for r in rows}
    assert by_id["claude"].managed is True
    assert by_id["claude"].installed is True
    assert by_id["copilot"].managed is True
    assert by_id["copilot"].installed is True
    assert all(not r.pathological for r in rows)


def test_tools_rows_after_unmanage_claude(tmp_home: Path, tmp_state: Path) -> None:
    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, _combined(init_result)
    unmanage_result = runner.invoke(app, ["unmanage", "claude"])
    assert unmanage_result.exit_code == 0, _combined(unmanage_result)
    rows = _build_tools_table_rows(get_deps())
    by_id = {r.tool_id: r for r in rows}
    assert by_id["claude"].managed is False
    assert by_id["claude"].installed is True
    assert by_id["copilot"].managed is True


def test_tools_rows_pathological_when_managed_live_missing(
    tmp_home: Path,
    tmp_state: Path,
) -> None:
    """Simulate the pathological case: tool is in active map but its
    live path was manually deleted. The row's pathological flag fires."""
    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, _combined(init_result)
    claude_path = tmp_home / ".claude"
    # The init step turned this into a symlink (POSIX) or a junction
    # (Windows); remove it entirely so detect_installed returns False
    # for claude. Windows junctions need rmdir(); shutil.rmtree on a
    # junction or symlink raises (CI surfaced this as OSError).
    _drop_link_or_dir(claude_path)
    rows = _build_tools_table_rows(get_deps())
    by_id = {r.tool_id: r for r in rows}
    assert by_id["claude"].managed is True
    assert by_id["claude"].installed is False
    assert by_id["claude"].pathological is True


def test_tools_command_renders_table_with_columns(tmp_home: Path, tmp_state: Path) -> None:
    """Coarse-grained sanity check on the rendered output."""
    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, _combined(init_result)
    result = runner.invoke(app, ["tools"])
    assert result.exit_code == 0
    assert "Installed" in result.output
    assert "Managed" in result.output
    assert "claude" in result.output
    assert "copilot" in result.output
    # At least one ✓ glyph for the managed column.
    assert "✓" in result.output


def test_tools_command_renders_pathological_footer(tmp_home: Path, tmp_state: Path) -> None:
    """Pathological row triggers the footer warning under the table."""
    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, _combined(init_result)
    _drop_link_or_dir(tmp_home / ".claude")
    result = runner.invoke(app, ["tools"])
    assert result.exit_code == 0
    assert "live path is missing" in result.output
    assert "switcher unmanage claude" in result.output


def test_status_on_empty_active_map(tmp_home: Path, tmp_state: Path) -> None:
    """Initialized + every tool unmanaged → suggest rescan (spec §3.5)."""
    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, _combined(init_result)
    unmanage_claude = runner.invoke(app, ["unmanage", "claude"])
    assert unmanage_claude.exit_code == 0, _combined(unmanage_claude)
    unmanage_copilot = runner.invoke(app, ["unmanage", "copilot"])
    assert unmanage_copilot.exit_code == 0, _combined(unmanage_copilot)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    out = result.output.lower()
    assert "no tools currently managed" in out
    # Profiles still on disk → status hints at `rescan`, not `init`.
    assert "rescan" in out
    assert "init" not in out


def test_tools_command_propagates_storage_error_on_corrupt_config(
    tmp_home: Path, tmp_state: Path
) -> None:
    """Hermes review: `_build_tools_table_rows` previously caught
    `Exception` and silently rendered every tool as unmanaged on a
    corrupt config.json. That hides exactly the failure modes the
    rest of the CLI is careful to surface. Instead, StorageError
    must propagate so handle_errors can render it to stderr and
    exit non-zero."""
    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, _combined(init_result)
    # Corrupt the active map: write malformed JSON shape.
    config_json = tmp_state / "config.json"
    assert config_json.exists()
    config_json.write_text('{"active": "not-a-dict"}', encoding="utf-8")

    result = runner.invoke(app, ["tools"])
    assert result.exit_code != 0, "tools should fail closed on corrupt state"
    err = (result.stderr or "") + result.output
    assert "config.json" in err.lower() or "malformed" in err.lower()


def test_tools_subprocess_does_not_crash_on_cp1252_stdout(tmp_home: Path, tmp_state: Path) -> None:
    """Regression for the v0.1.4 Windows CI break: the new tools table
    emits ✓ / — / ⚠ glyphs that crash with UnicodeEncodeError if stdout
    is cp1252 / cp437 (Windows-cmd default). cli.py reconfigures stdout
    to UTF-8 (errors='replace') at import time so the command degrades
    gracefully on legacy consoles instead of crashing.

    Repro the Windows-encoding scenario portably by spawning `python -m
    switcher tools` with PYTHONIOENCODING=cp1252 (and an argv-injected
    init dir). If the encoding fix regresses, the subprocess's stdout
    encoder will raise on the first `✓` and the exit code will be 1.
    """
    import subprocess
    import sys

    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, _combined(init_result)

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "cp1252:replace"
    env["SWITCHER_STATE_DIR"] = str(tmp_state)
    if IS_WINDOWS:
        env["USERPROFILE"] = str(tmp_home)
        env["LOCALAPPDATA"] = str(tmp_home / "AppData" / "Local")
    else:
        env["HOME"] = str(tmp_home)

    proc = subprocess.run(
        [sys.executable, "-m", "switcher", "tools"],
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"tools crashed under cp1252 stdout: {proc.stderr!r}"


def test_status_pre_init_suggests_init(tmp_home: Path, tmp_state: Path) -> None:
    """Uninitialized → suggest init, NOT rescan (Hermes review).

    `rescan` on an uninitialized state raises StateNotInitializedError, so
    the previous wording told users to run a command that would fail.
    """
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    out = result.output.lower()
    assert "no tools currently managed" in out
    assert "init" in out
    # Don't tell users to `rescan` before init exists.
    assert "rescan" not in out
