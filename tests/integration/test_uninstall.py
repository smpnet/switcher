# pyright: reportPrivateUsage=none
"""End-to-end uninstall flow (spec §3).

A few tests reach into private store internals to set up corruption / orphan
scenarios that the public API doesn't surface (e.g. injecting an orphan
tool id into the active map). The pragma keeps strict-typecheck quiet for
those test-only setup paths.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from switcher.errors import UninstallPreflightError
from switcher.paths import IS_WINDOWS, PathResolver
from switcher.registry import build_registry
from switcher.service import ProfileService
from switcher.store import FileProfileStore

pytestmark = pytest.mark.integration


def _service(tmp_state: Path, tmp_home: Path) -> ProfileService:
    return ProfileService(
        FileProfileStore(tmp_state),
        PathResolver(home=tmp_home),
        build_registry(tmp_state / "registry.d"),
    )


def _is_link(p: Path) -> bool:
    return p.is_symlink() or (IS_WINDOWS and os.path.isjunction(p))


def _drop_link(p: Path) -> None:
    if IS_WINDOWS and os.path.isjunction(p):
        p.rmdir()
        return
    if p.is_symlink():
        p.unlink()


def test_uninstall_default_restores_real_dirs_and_clears_active(
    tmp_state: Path, tmp_home: Path
) -> None:
    s = _service(tmp_state, tmp_home)
    s.init()

    s.uninstall()

    # Every live path is now a real dir (no longer a link).
    for live in [
        tmp_home / ".claude",
        tmp_home / ".copilot",
        tmp_home / ".config" / "github-copilot",
    ]:
        if live.exists():  # github-copilot may resolve differently per OS
            assert live.is_dir() and not _is_link(live)
    # active and active_live_paths cleared.
    assert s._store.get_active() == {}
    assert s._store.get_active_live_paths() == {}
    # State dir preserved.
    assert tmp_state.is_dir()
    assert (tmp_state / "profiles").is_dir()


def test_uninstall_purge_removes_state_dir(
    tmp_state: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _service(tmp_state, tmp_home)
    s.init()

    s.uninstall(purge=True, yes=True)

    assert not tmp_state.exists()


def test_uninstall_purge_refuses_non_tty_without_yes(
    tmp_state: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _service(tmp_state, tmp_home)
    s.init()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    with pytest.raises(UninstallPreflightError, match="non-interactive"):
        s.uninstall(purge=True, yes=False)
    # Pre-flight catches before swap → state still there.
    assert (tmp_state / "config.json").exists()


def test_uninstall_resume_skips_already_restored_mapping(tmp_state: Path, tmp_home: Path) -> None:
    """A previously-aborted run left ~/.claude already restored; rerun completes."""
    s = _service(tmp_state, tmp_home)
    s.init()
    # Manually pre-restore claude.
    claude_link = tmp_home / ".claude"
    claude_target = tmp_state / "profiles" / next(iter(s._store.get_active().values())) / "claude"
    _drop_link(claude_link)
    shutil.copytree(claude_target, claude_link)

    s.uninstall()  # should NOT raise

    assert claude_link.is_dir() and not _is_link(claude_link)
    assert s._store.get_active() == {}


def test_uninstall_corrupt_state_rejects_without_force(tmp_state: Path, tmp_home: Path) -> None:
    s = _service(tmp_state, tmp_home)
    s.init()
    # Replace ~/.claude with a real dir whose contents do NOT match profile.
    claude_link = tmp_home / ".claude"
    _drop_link(claude_link)
    claude_link.mkdir()
    (claude_link / "bogus.txt").write_text("not in profile")

    with pytest.raises(UninstallPreflightError, match="does not match"):
        s.uninstall()


def test_uninstall_purge_refuses_when_force_would_skip_tools(
    tmp_state: Path, tmp_home: Path
) -> None:
    s = _service(tmp_state, tmp_home)
    s.init()
    # Inject an orphan tool: in active map, no registry, no cache.
    active = s._store.get_active()
    active["orphan"] = next(iter(active.values()))
    s._store.set_active(active)
    cache = s._store.get_active_live_paths()
    cache.pop("orphan", None)
    s._store.set_active_live_paths(cache)

    with pytest.raises(UninstallPreflightError, match="would be skipped"):
        s.uninstall(force=True, purge=True, yes=True)


def test_uninstall_force_skips_orphan_tool_in_non_purge_mode(
    tmp_state: Path, tmp_home: Path
) -> None:
    s = _service(tmp_state, tmp_home)
    s.init()
    active = s._store.get_active()
    orphan_profile = next(iter(active.values()))
    active["orphan"] = orphan_profile
    s._store.set_active(active)

    report = s.uninstall(force=True)

    # Real tools restored, orphan kept in active map (its symlink remains).
    assert "orphan" in s._store.get_active()
    assert s._store.get_active() == {"orphan": orphan_profile}
    assert ("orphan", "no registry entry and no cached live_paths") in report.skipped


def test_uninstall_declined_purge_clears_active(
    tmp_state: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §3.5: declined --purge prompt still clears active map after unwind."""
    s = _service(tmp_state, tmp_home)
    s.init()
    # Simulate an interactive prompt that declines.
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def _decline(_prompt: str) -> str:
        return "no"

    monkeypatch.setattr("builtins.input", _decline)

    report = s.uninstall(purge=True)

    assert report.purged is False
    # Live paths are real dirs.
    assert (tmp_home / ".claude").is_dir() and not _is_link(tmp_home / ".claude")
    # State dir preserved.
    assert tmp_state.exists()
    # Active map cleared.
    assert s._store.get_active() == {}
    assert s._store.get_active_live_paths() == {}


def test_uninstall_purge_succeeded_sets_purged_flag(tmp_state: Path, tmp_home: Path) -> None:
    s = _service(tmp_state, tmp_home)
    s.init()
    report = s.uninstall(purge=True, yes=True)
    assert report.purged is True
    assert not tmp_state.exists()


def test_uninstall_dry_run_purge_bypasses_non_tty_guard(
    tmp_state: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §3.1: dry-run --purge previews both phases without mutating or prompting."""
    s = _service(tmp_state, tmp_home)
    s.init()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    report = s.uninstall(purge=True, dry_run=True)  # should NOT raise

    assert report.purged is False  # nothing actually purged
    assert tmp_state.exists()
    # Live paths still symlinks (dry run = no mutation).
    assert _is_link(tmp_home / ".claude")
