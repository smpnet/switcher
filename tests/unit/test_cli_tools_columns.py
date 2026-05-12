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


def test_tools_renders_orphan_row_with_dedicated_footer(tmp_home: Path, tmp_state: Path) -> None:
    """Hermes blocker: an orphan id in the active map (no registry entry)
    used to be invisible to `tools` because the row builder iterated the
    registry only. After `uninstall --force` non-purge leaves an orphan
    skipped tool in active, `tools` must surface it as a dedicated row
    AND a footer telling the user how to repair it."""
    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, _combined(init_result)
    deps = get_deps()
    active = deps.store.get_active()
    profile_name = next(iter(active.values()))
    active["orphan_tool"] = profile_name
    deps.store.set_active_state(active, deps.store.get_active_live_paths())

    rows = _build_tools_table_rows(get_deps())
    by_id = {r.tool_id: r for r in rows}
    assert "orphan_tool" in by_id, "orphan id in active map must appear as its own table row"
    orphan_row = by_id["orphan_tool"]
    assert orphan_row.is_orphan is True
    assert orphan_row.managed is True
    assert orphan_row.installed is False
    assert orphan_row.pathological is True

    result = runner.invoke(app, ["tools"])
    assert result.exit_code == 0
    assert "orphan_tool" in result.output
    # Orphan footer wording — distinct from the missing-live-path footer.
    assert "no registry entry" in result.output.lower()
    assert "switcher unmanage orphan_tool --force" in result.output


def test_tools_pathological_when_managed_multi_dir_partially_present(
    tmp_home: Path, tmp_state: Path
) -> None:
    """Hermes blocker: a multi-dir managed tool whose first config_dir
    still exists but a later config_dir is missing must render
    pathological (= ⚠) — `detect_installed()` only checks the FIRST
    config_dir, so without a per-row multi-dir check the broken state
    looks healthy.

    Repro uses the legacy two-dir Copilot override so both paths exist
    after init, then deletes only `~/.copilot` (the SECOND config_dir
    in the override; the first is `~/.config/github-copilot`).
    """
    # Install the two-dir override directly under registry.d. Both halves
    # of each config_dir entry use platform-appropriate templates so the
    # registry resolves to the same paths the conftest tmp_home pre-seeds.
    registry_d = tmp_state / "registry.d"
    registry_d.mkdir(parents=True, exist_ok=True)
    (registry_d / "copilot.toml").write_text(
        'id = "copilot"\n'
        'name = "GitHub Copilot CLI (test two-dir override)"\n'
        "[[config_dirs]]\n"
        'posix_path = "~/.config/github-copilot"\n'
        'windows_path = "%LOCALAPPDATA%\\\\github-copilot"\n'
        'profile_subdir = "copilot-auth"\n'
        "[[config_dirs]]\n"
        'posix_path = "~/.copilot"\n'
        'windows_path = "%USERPROFILE%\\\\.copilot"\n'
        'profile_subdir = "copilot-config"\n'
    )

    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, _combined(init_result)

    # Delete only the SECOND config_dir's symlink (~/.copilot). The first
    # (.config/github-copilot or %LOCALAPPDATA%\github-copilot) still exists,
    # so first-dir-only detection still reports copilot as installed.
    # `~/.copilot` on POSIX and `%USERPROFILE%\.copilot` on Windows both
    # resolve to `tmp_home / ".copilot"` under the conftest fixture.
    _drop_link_or_dir(tmp_home / ".copilot")

    rows = _build_tools_table_rows(get_deps())
    by_id = {r.tool_id: r for r in rows}
    copilot_row = by_id["copilot"]
    assert copilot_row.managed is True
    # The whole point: even though detect_installed (first-dir-only) still
    # returns True, the per-row multi-dir check must classify pathological.
    assert copilot_row.pathological is True, (
        "managed multi-dir tool with one missing live path must be pathological"
    )
    assert copilot_row.installed is False
