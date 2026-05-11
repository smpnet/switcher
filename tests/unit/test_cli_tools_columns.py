# pyright: reportPrivateUsage=none
"""Spec §2.3 — switcher tools shows Installed + Managed columns.

Spec §3.5 — status command empty-active-map message.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from click.testing import Result
from typer.testing import CliRunner

from switcher.cli import _build_tools_table_rows, app, get_deps

runner = CliRunner()


def _combined(result: Result) -> str:
    """Concatenate stdout + stderr for failure messages on setup asserts."""
    stdout = getattr(result, "stdout", "") or ""
    stderr = getattr(result, "stderr", "") or ""
    return f"stdout:\n{stdout}\nstderr:\n{stderr}"


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
    # The init step turned this into a symlink; remove it entirely so
    # detect_installed returns False for claude.
    if claude_path.is_symlink():
        claude_path.unlink()
    elif claude_path.is_dir():
        shutil.rmtree(claude_path)
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
    claude_path = tmp_home / ".claude"
    if claude_path.is_symlink():
        claude_path.unlink()
    elif claude_path.is_dir():
        shutil.rmtree(claude_path)
    result = runner.invoke(app, ["tools"])
    assert result.exit_code == 0
    assert "live path is missing" in result.output
    assert "switcher unmanage claude" in result.output


def test_status_on_empty_active_map(tmp_home: Path, tmp_state: Path) -> None:
    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, _combined(init_result)
    unmanage_claude = runner.invoke(app, ["unmanage", "claude"])
    assert unmanage_claude.exit_code == 0, _combined(unmanage_claude)
    unmanage_copilot = runner.invoke(app, ["unmanage", "copilot"])
    assert unmanage_copilot.exit_code == 0, _combined(unmanage_copilot)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "no tools currently managed" in result.output.lower()
