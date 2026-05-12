# pyright: reportPrivateUsage=none
"""Spec §2.2 — switcher unmanage <tool>."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from typer.testing import CliRunner

from switcher.cli import app
from switcher.paths import IS_WINDOWS
from switcher.service import _temp_dir_for_uninstall

runner = CliRunner()


def _combined(result: object) -> str:
    stdout = getattr(result, "stdout", "") or ""
    stderr = getattr(result, "stderr", "") or ""
    return stdout + stderr


def _is_link(p: Path) -> bool:
    """True for POSIX symlinks AND Windows directory junctions.

    `Path.is_symlink()` returns False for junctions on Windows, which would
    silently turn a "post-unmanage path is not a link" assertion into a
    false pass when init/unmanage left a junction in place by accident.
    """
    return p.is_symlink() or (IS_WINDOWS and os.path.isjunction(p))


def _drop_link(p: Path) -> None:
    if IS_WINDOWS and os.path.isjunction(p):
        p.rmdir()
        return
    if p.is_symlink():
        p.unlink()


def test_unmanage_happy_path(tmp_home: Path, tmp_state: Path) -> None:
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, _combined(setup)

    result = runner.invoke(app, ["unmanage", "claude"])
    assert result.exit_code == 0, _combined(result)

    from switcher.cli import get_deps

    assert "claude" not in get_deps().store.get_active()
    # Live path is a real dir, not a symlink/junction.
    claude_path = tmp_home / ".claude"
    assert claude_path.is_dir()
    assert not _is_link(claude_path)


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
    # Symlink (POSIX) or junction (Windows) still in place — dry-run
    # never touches the filesystem.
    assert _is_link(tmp_home / ".claude")


def test_unmanage_dry_run_labels_missing_live_temp_present_as_recover(
    tmp_home: Path, tmp_state: Path
) -> None:
    """abby review: dry-run must not call MISSING_LIVE_TEMP_PRESENT a 'no-op'
    — a real run renames the sibling temp back into place. The preview has to
    surface that recovery action so operators can trust the dry-run."""
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, _combined(setup)

    # Simulate a crash between unlink and rename for claude: drop the symlink
    # at the live path, leave a populated sibling temp dir.
    from switcher.cli import get_deps

    deps = get_deps()
    service = deps.service
    mapping = next(m for m in service._classify_uninstall_mappings() if m.tool_id == "claude")
    temp = _temp_dir_for_uninstall(mapping.live_path)
    _drop_link(mapping.live_path)
    shutil.copytree(mapping.profile_dir_subdir, temp)

    result = runner.invoke(app, ["unmanage", "claude", "--dry-run"])
    assert result.exit_code == 0, _combined(result)
    out = result.stdout
    assert "MISSING_LIVE_TEMP_PRESENT" in out
    # `re.search(\s+)` so the assertion survives Rich's terminal-width
    # line-wrap on long Windows paths (the `soft_wrap=True` on the CLI
    # print is the primary fix; this is defense-in-depth).
    assert re.search(r"would\s+recover", out, flags=re.IGNORECASE), out
    # "no-op" must NOT appear for this mapping — that's the unsafe wording.
    assert "no-op" not in out.lower()


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
    assert (tmp_home / ".claude").is_dir()
    assert not _is_link(tmp_home / ".claude")


def test_unmanage_when_uninitialized_errors(tmp_home: Path, tmp_state: Path) -> None:
    """Before init, unmanage must surface state-not-initialized, not crash."""
    result = runner.invoke(app, ["unmanage", "claude"])
    assert result.exit_code != 0
    out = _combined(result).lower()
    assert "not been initialized" in out or "switcher init" in out
