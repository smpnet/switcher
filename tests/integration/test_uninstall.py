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

from .conftest import install_two_dir_copilot_override

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


def test_uninstall_purge_removes_state_dir(tmp_state: Path, tmp_home: Path) -> None:
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


@pytest.mark.skipif(
    IS_WINDOWS, reason="POSIX symlink semantics; Windows config-dir symlinks need elevation"
)
def test_uninstall_preserves_symlinks_inside_managed_config_dir(
    tmp_state: Path, tmp_home: Path
) -> None:
    """Hermes review: `shutil.copytree(..., symlinks=False)` dereferences
    symlinks inside the managed config dir, so uninstall silently changes
    the user's tree shape and an interrupted uninstall becomes
    non-resumable (`_dirs_match()` sees more entries in temp than in
    profile because `os.walk` does not recurse into linked dirs). With
    `symlinks=True`, the symlink structure round-trips through capture
    and uninstall.
    """
    # Stage a symlink target dir + a content file under ~/.claude BEFORE
    # init, so the symlink is captured into the profile in its original
    # form.
    target_outside = tmp_home / ".claude-external-data"
    target_outside.mkdir()
    (target_outside / "sentinel.txt").write_text("external")
    (tmp_home / ".claude" / "linked-subdir").symlink_to(target_outside, target_is_directory=True)
    (tmp_home / ".claude" / "linked-file.txt").symlink_to(target_outside / "sentinel.txt")

    s = _service(tmp_state, tmp_home)
    s.init()

    # Pre-condition: the symlinks survived capture into the profile.
    profile_name = next(iter(s._store.get_active().values()))
    profile_claude = tmp_state / "profiles" / profile_name / "claude"
    assert (profile_claude / "linked-subdir").is_symlink()
    assert (profile_claude / "linked-file.txt").is_symlink()

    s.uninstall()

    # Live ~/.claude is a real directory again — but the nested symlinks
    # are preserved (NOT dereferenced into real copies).
    claude_live = tmp_home / ".claude"
    assert claude_live.is_dir() and not _is_link(claude_live)
    assert (claude_live / "linked-subdir").is_symlink()
    assert (claude_live / "linked-file.txt").is_symlink()
    # The symlinks resolve to the same external target they originally pointed at.
    assert (claude_live / "linked-subdir").resolve() == target_outside.resolve()
    assert (claude_live / "linked-file.txt").read_text() == "external"


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


def test_uninstall_processes_all_cached_paths_when_registry_shrinks(
    tmp_state: Path, tmp_home: Path
) -> None:
    """Spec §3.4 / Hermes review: if a TOML edit removes a config_dir after
    init, the cache still has both live paths but the registry only has
    one. Uninstall must process ALL cached paths (driving from the cache),
    not silently drop the extras and leave a dangling symlink behind.
    """
    # Start with the legacy two-dir copilot shape so init captures both
    # config dirs into the cache. Then simulate the registry shrinking.
    install_two_dir_copilot_override(tmp_state)
    s = _service(tmp_state, tmp_home)
    s.init()
    # Pre-condition: copilot's cache has 2 entries (the override's two config_dirs).
    assert len(s._store.get_active_live_paths()["copilot"]) == 2

    # Simulate registry drift: shrink copilot to a single config_dir by
    # rewriting the user override. The cached entries are unchanged (cache
    # writes only on capture/rescan).
    registry_d = tmp_state / "registry.d"
    (registry_d / "copilot.toml").write_text(
        'id = "copilot"\n'
        'name = "GitHub Copilot CLI (shrunk)"\n'
        "[[config_dirs]]\n"
        'posix_path = "~/.copilot"\n'
        'windows_path = "%USERPROFILE%\\\\.copilot"\n'
        'profile_subdir = "copilot-config"\n'
    )

    # Re-instantiate so the new registry takes effect.
    shrunk = _service(tmp_state, tmp_home)

    # Dry-run should plan BOTH cached restores, not just the registry's one.
    report = shrunk.uninstall(dry_run=True)
    copilot_mappings = [m for m in report.mappings if m.tool_id == "copilot"]
    assert len(copilot_mappings) == 2, [m.live_path for m in copilot_mappings]
    # No CORRUPT classifications — both cached paths are still symlinks.
    assert all(m.state.value != "corrupt" for m in copilot_mappings)

    # Real uninstall restores both live dirs to real directories — no
    # dangling symlink left behind.
    shrunk.uninstall()
    assert (tmp_home / ".copilot").is_dir() and not _is_link(tmp_home / ".copilot")
    posix_first = tmp_home / ".config" / "github-copilot"
    windows_first = tmp_home / "AppData" / "Local" / "github-copilot"
    first_live = windows_first if IS_WINDOWS else posix_first
    assert first_live.is_dir() and not _is_link(first_live)


def test_uninstall_purge_aborted_input_clears_active_before_propagating(
    tmp_state: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hermes review: if `input()` raises (stdin closed, Ctrl-C) during the
    --purge confirmation, the unwind has already restored live dirs. The
    abort must still clear active/active_live_paths so config.json doesn't
    claim tools are managed when their symlinks are already gone.
    """
    s = _service(tmp_state, tmp_home)
    s.init()

    def raising_input(_prompt: str) -> str:
        raise EOFError("simulated stdin close during confirmation")

    # Bypass the non-TTY pre-flight guard so the code reaches the input()
    # prompt that we're actually testing. The repro models a TTY session
    # whose stdin dies AFTER the unwind, not a non-interactive invocation.
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", raising_input)

    with pytest.raises(EOFError, match="simulated stdin close"):
        s.uninstall(purge=True, yes=False)

    # Live dirs already restored before the prompt fired.
    assert (tmp_home / ".claude").is_dir() and not _is_link(tmp_home / ".claude")
    # On-disk active state was cleared before EOFError propagated, so a
    # fresh store sees "no tools managed" (matching the live dirs).
    fresh = _service(tmp_state, tmp_home)
    assert fresh._store.get_active() == {}
    assert fresh._store.get_active_live_paths() == {}


def test_uninstall_uses_cached_live_paths_after_registry_drift(
    tmp_state: Path, tmp_home: Path
) -> None:
    """Spec §3.4 / Hermes review: uninstall must use active_live_paths
    when the tool's TOML changes config_dirs post-init. Without this the
    cache is consulted only for orphan tools, and a TOML edit (e.g. the
    user moves a config dir to a different location) makes uninstall
    fail with `live path missing` against the new path even though the
    old (cached) symlink is still on disk.
    """
    s = _service(tmp_state, tmp_home)
    s.init()
    # Cache has the original ~/.claude path.
    assert "claude" in s._store.get_active_live_paths()

    # Simulate registry drift: user TOML overrides claude with a different
    # posix_path. profile_subdir stays "claude" so the on-disk profile
    # subdir keeps matching what the cached symlink points to.
    registry_d = tmp_state / "registry.d"
    registry_d.mkdir(parents=True, exist_ok=True)
    (registry_d / "claude.toml").write_text(
        'id = "claude"\n'
        'name = "Claude (drifted)"\n'
        "[[config_dirs]]\n"
        'posix_path = "~/.claude-elsewhere"\n'
        'windows_path = "%USERPROFILE%\\\\.claude-elsewhere"\n'
        'profile_subdir = "claude"\n'
    )

    # Re-instantiate so the new registry takes effect for this service.
    drifted = _service(tmp_state, tmp_home)

    # Dry-run honors the cached path: no CORRUPT classification.
    report = drifted.uninstall(dry_run=True)
    assert all(m.state.value != "corrupt" for m in report.mappings), [
        (m.tool_id, m.state.value, m.corruption_reason) for m in report.mappings
    ]

    # Real uninstall restores the cached live path to a real directory.
    drifted.uninstall()
    assert (tmp_home / ".claude").is_dir() and not _is_link(tmp_home / ".claude")


def test_uninstall_after_config_dirs_reorder(tmp_state: Path, tmp_home: Path) -> None:
    """CodeRabbit round 4: when a tool's config_dirs are reordered in the
    TOML between init and uninstall, the classifier must still pair each
    cached live path with its actual on-disk subdir (read from the symlink
    target), not with `tool.config_dirs[i].profile_subdir`. The old
    count-match shortcut paired by index, so a reorder caused healthy
    mappings to be classified CORRUPT (target-mismatch) and blocked
    uninstall.
    """
    # Start with the legacy two-dir copilot shape so init captures both.
    install_two_dir_copilot_override(tmp_state)
    s = _service(tmp_state, tmp_home)
    s.init()
    # Pre-condition: copilot's cache has 2 entries.
    cached_before = list(s._store.get_active_live_paths()["copilot"])
    assert len(cached_before) == 2

    # Simulate user reordering config_dirs in the TOML between init and
    # uninstall. The subdirs and live paths stay the same, only the order
    # within `config_dirs` swaps.
    registry_d = tmp_state / "registry.d"
    (registry_d / "copilot.toml").write_text(
        'id = "copilot"\n'
        'name = "GitHub Copilot CLI (reordered)"\n'
        "[[config_dirs]]\n"
        'posix_path = "~/.copilot"\n'
        'windows_path = "%USERPROFILE%\\\\.copilot"\n'
        'profile_subdir = "copilot-config"\n'
        "[[config_dirs]]\n"
        'posix_path = "~/.config/github-copilot"\n'
        'windows_path = "%LOCALAPPDATA%\\\\github-copilot"\n'
        'profile_subdir = "copilot-auth"\n'
    )

    # Re-instantiate so the reordered registry takes effect.
    reordered = _service(tmp_state, tmp_home)

    # Dry-run: both mappings classify cleanly, no CORRUPT from mispairing.
    report = reordered.uninstall(dry_run=True)
    copilot_mappings = [m for m in report.mappings if m.tool_id == "copilot"]
    assert len(copilot_mappings) == 2
    assert all(m.state.value != "corrupt" for m in copilot_mappings), [
        (m.profile_subdir, m.state.value, m.corruption_reason) for m in copilot_mappings
    ]

    # Real uninstall succeeds and restores both live dirs to real directories.
    reordered.uninstall()
    assert (tmp_home / ".copilot").is_dir() and not _is_link(tmp_home / ".copilot")
    posix_first = tmp_home / ".config" / "github-copilot"
    windows_first = tmp_home / "AppData" / "Local" / "github-copilot"
    first_live = windows_first if IS_WINDOWS else posix_first
    assert first_live.is_dir() and not _is_link(first_live)


def test_uninstall_purge_clears_active_state_before_destructive_delete(
    tmp_state: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §3.5: --purge must clear active/active_live_paths in config.json
    BEFORE the destructive rmtree so that a partial rmtree failure can't
    leave persisted state lying about tools whose symlinks are already gone.

    Repro shape (Hermes review): rmtree of state_dir fails after the live
    dirs have been restored to real directories. Without the pre-rmtree
    clear, `status` would still show the tools as active even though their
    symlinks no longer exist.
    """
    import switcher.service as svc_mod

    s = _service(tmp_state, tmp_home)
    s.init()

    # The only `shutil.rmtree` call in the uninstall purge path is the
    # state-dir wipe, so an unconditional raise simulates that single
    # failure point without affecting other code paths.
    def failing_rmtree(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated FS error during state-dir rmtree")

    monkeypatch.setattr(svc_mod.shutil, "rmtree", failing_rmtree)

    with pytest.raises(RuntimeError, match="simulated FS error"):
        s.uninstall(purge=True, yes=True)

    # Live dirs already restored before the rmtree failure.
    assert (tmp_home / ".claude").is_dir() and not _is_link(tmp_home / ".claude")

    # Config persisted "no tools managed" before the destructive delete,
    # so a re-instantiated store sees an empty active map — matching the
    # restored live dirs — instead of lying about a stale active entry.
    fresh = _service(tmp_state, tmp_home)
    assert fresh._store.get_active() == {}
    assert fresh._store.get_active_live_paths() == {}


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


def test_uninstall_resume_after_partial_uninstall_and_registry_drift(
    tmp_state: Path, tmp_home: Path
) -> None:
    """Hermes blocker post-b621f02: an interrupted uninstall on the legacy
    two-dir Copilot, followed by a registry rewrite to the new single-dir
    shape, used to wedge the resume — the cached path was no longer a
    link AND the current registry didn't mention it, so
    `_classify_uninstall_mappings` defaulted to CORRUPT and the pre-flight
    refused. The historical-pair table now provides the missing
    profile_subdir so the resume completes cleanly.

    Repro flow:
      1. init with the legacy two-dir copilot override
         (~/.config/github-copilot + ~/.copilot)
      2. execute only the first uninstall mapping (simulate
         crash/interruption mid-uninstall)
      3. rewrite the registry to drop the github-copilot config_dir
      4. re-run uninstall — must succeed (resumes the second mapping
         and treats the first as ALREADY_RESTORED).
    """
    install_two_dir_copilot_override(tmp_state)
    s = _service(tmp_state, tmp_home)
    s.init()

    # Pre-conditions: both copilot live paths are symlinks; cache has both.
    legacy_link = tmp_home / ".config" / "github-copilot"
    new_link = tmp_home / ".copilot"
    assert _is_link(legacy_link)
    assert _is_link(new_link)
    assert len(s._store.get_active_live_paths()["copilot"]) == 2

    # Step 2: simulate partial uninstall — restore ONLY the legacy mapping.
    profile_name = s._store.get_active()["copilot"]
    profile_dir = s._store.profile_dir(profile_name)
    legacy_target = profile_dir / "copilot-auth"
    _drop_link(legacy_link)
    shutil.copytree(legacy_target, legacy_link)
    # Cache untouched — still claims both paths are managed.

    # Step 3: rewrite registry to drop github-copilot. Now the registry
    # only mentions ~/.copilot.
    registry_d = tmp_state / "registry.d"
    (registry_d / "copilot.toml").write_text(
        'id = "copilot"\n'
        'name = "GitHub Copilot CLI (shrunk)"\n'
        "[[config_dirs]]\n"
        'posix_path = "~/.copilot"\n'
        'windows_path = "%USERPROFILE%\\\\.copilot"\n'
        'profile_subdir = "copilot-config"\n'
    )

    # Step 4: re-run uninstall — must NOT wedge on CORRUPT classification.
    shrunk = _service(tmp_state, tmp_home)
    report = shrunk.uninstall(dry_run=True)
    copilot_mappings = [m for m in report.mappings if m.tool_id == "copilot"]
    assert len(copilot_mappings) == 2, [
        (m.profile_subdir, m.state.value, m.corruption_reason) for m in copilot_mappings
    ]
    # Neither mapping CORRUPT — historical pair provided the missing subdir.
    assert all(m.state.value != "corrupt" for m in copilot_mappings), [
        (m.profile_subdir, m.state.value, m.corruption_reason) for m in copilot_mappings
    ]

    # Real uninstall completes — second link is unwound, first is no-op.
    shrunk.uninstall()
    assert (tmp_home / ".copilot").is_dir() and not _is_link(tmp_home / ".copilot")
    assert legacy_link.is_dir() and not _is_link(legacy_link)
