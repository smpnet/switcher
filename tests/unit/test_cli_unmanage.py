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
from switcher.service import ProfileService, _temp_dir_for_uninstall

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


def _orphan_copilot_service(
    tmp_state: Path, tmp_home: Path
) -> tuple[ProfileService, ProfileService, str]:
    """Helper used by the orphan-no-cache + --force tests below.

    Returns (full_service, orphan_service, profile_name) where:
      - full_service has the unmodified registry (used for `init`).
      - orphan_service has copilot REMOVED from its registry — copilot
        is now an orphan from this service's POV.
      - cache for copilot has been stripped to satisfy the
        orphan-no-cache precondition.
    """
    from switcher.paths import PathResolver
    from switcher.registry import build_registry
    from switcher.store import FileProfileStore

    full_registry = build_registry(tmp_state / "registry.d")
    full = ProfileService(
        FileProfileStore(tmp_state),
        PathResolver(home=tmp_home),
        full_registry,
    )
    full.init()
    active = full._store.get_active()
    assert "copilot" in active
    profile_name = active["copilot"]

    no_copilot = tuple(t for t in full_registry if t.id != "copilot")
    orphan = ProfileService(
        FileProfileStore(tmp_state),
        PathResolver(home=tmp_home),
        no_copilot,
    )
    cache = orphan._store.get_active_live_paths()
    new_cache = {k: v for k, v in cache.items() if k != "copilot"}
    orphan._store.set_active_state(active, new_cache)
    return full, orphan, profile_name


def test_unmanage_force_refuses_when_orphan_has_owned_subdir_in_profile(
    tmp_home: Path, tmp_state: Path
) -> None:
    """Hermes blocker (most realistic shape): `unmanage --force` on
    orphan-no-cache used to drop the active entry even when the tool
    still had data inside owned profile subdirs. A subsequent
    `uninstall --purge` then silently destroyed the data — the tool
    was no longer in `active`, so its purge-time guard never fired.

    The fix refuses the drop when any historical/current owned subdir
    for the tool exists in the profile dir. The user's escape: restore
    the registry TOML, then `unmanage` cleans up properly.
    """
    import pytest as _pytest

    from switcher.errors import UninstallPreflightError

    _full, orphan, profile_name = _orphan_copilot_service(tmp_state, tmp_home)
    profile_dir = orphan._store.profile_dir(profile_name)
    # init populated copilot-config; that owned subdir is exactly what we're
    # protecting from a later `uninstall --purge` data loss.
    assert (profile_dir / "copilot-config").is_dir()

    with _pytest.raises(UninstallPreflightError, match="still has profile data"):
        orphan.unmanage("copilot", force=True)
    # active map unchanged — the refusal preserves the purge guard.
    assert "copilot" in orphan._store.get_active()


def test_unmanage_force_succeeds_when_orphan_has_no_on_disk_presence(
    tmp_home: Path, tmp_state: Path
) -> None:
    """Counterpart to the blocker test: when nothing on disk is at risk
    (no owned subdir in profile, no discoverable symlink), --force is
    the documented escape hatch and should still succeed.
    """
    _full, orphan, profile_name = _orphan_copilot_service(tmp_state, tmp_home)
    profile_dir = orphan._store.profile_dir(profile_name)
    # Remove the owned subdir so the new owned-subdir refusal doesn't fire.
    shutil.rmtree(profile_dir / "copilot-config")
    # And remove the live symlink at ~/.copilot so derive-on-read can't
    # populate cache — keeps the orphan-no-cache branch reachable.
    copilot_link = tmp_home / ".copilot"
    if copilot_link.is_symlink():
        copilot_link.unlink()
    elif copilot_link.is_dir():
        shutil.rmtree(copilot_link)

    report = orphan.unmanage("copilot", force=True)
    assert report.skipped_orphan is True
    assert "copilot" not in orphan._store.get_active()


def test_unmanage_force_succeeds_for_truly_unknown_orphan_tool(
    tmp_home: Path, tmp_state: Path
) -> None:
    """A tool id that was never in the registry AND has no historical
    metadata can still be force-skipped — switcher has no on-disk
    presence to defend. (Pre-fix behavior preserved for the case where
    the safety check has nothing to find.)
    """
    from switcher.paths import PathResolver
    from switcher.registry import build_registry
    from switcher.service import ProfileService
    from switcher.store import FileProfileStore

    full_registry = build_registry(tmp_state / "registry.d")
    service = ProfileService(
        FileProfileStore(tmp_state),
        PathResolver(home=tmp_home),
        full_registry,
    )
    service.init()
    active = service._store.get_active()
    new_active = dict(active)
    new_active["fake_orphan_tool"] = next(iter(active.values()))
    cache = service._store.get_active_live_paths()
    service._store.set_active_state(new_active, dict(cache))

    # Discovery + owned-subdir checks both return empty for an unknown id.
    report = service.unmanage("fake_orphan_tool", force=True)
    assert report.skipped_orphan is True
    assert "fake_orphan_tool" not in service._store.get_active()


def test_unmanage_force_dry_run_orphan_does_not_say_already_restored(
    tmp_home: Path, tmp_state: Path
) -> None:
    """CodeRabbit blocker: `unmanage --force --dry-run` on an orphan-no-cache
    tool used to print "no-op (already restored)", but the real run actually
    drops the tool from the active map and leaves any symlinks in place.
    The dry-run preview now says "would skip orphan" so operators evaluating
    --dry-run see the actual semantics.
    """
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, _combined(setup)

    # Inject an orphan tool: present in active map but no registry entry
    # AND no live_paths cache. Use the store directly to construct this
    # state since the public CLI path can't.
    from switcher.cli import get_deps

    deps = get_deps()
    active = deps.store.get_active()
    new_active = dict(active)
    new_active["orphan_tool"] = next(iter(active.values()))  # any profile
    cache = deps.store.get_active_live_paths()
    new_cache = dict(cache)  # leave cache empty for orphan_tool
    deps.store.set_active_state(new_active, new_cache)

    result = runner.invoke(app, ["unmanage", "orphan_tool", "--force", "--dry-run"])
    assert result.exit_code == 0, _combined(result)
    out = result.stdout.lower()
    assert "no-op" not in out, (
        f"orphan dry-run mislabeled as no-op (real run drops from active map): {out}"
    )
    assert "skip orphan" in out or "drop from active map" in out, (
        f"expected explicit orphan-skip wording in dry-run output: {out}"
    )


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
