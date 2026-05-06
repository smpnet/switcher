"""Service-level operations: detect, init, use, save, create, which, rename, delete."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from switcher.errors import (
    AlreadyLinkedError,
    PathNotADirectoryError,
    ProfileExistsError,
    ProfileIsActiveError,
    StateAlreadyInitializedError,
    StateNotInitializedError,
    ToolHasNoActiveProfileError,
    ToolNotInProfileError,
    UnknownProfileError,
    UnknownToolError,
)
from switcher.models import Tool
from switcher.paths import IS_WINDOWS, PathResolver
from switcher.registry import build_registry
from switcher.service import ProfileService
from switcher.store import FileProfileStore


@pytest.fixture
def registry() -> tuple[Tool, ...]:
    """Use the real builtins so live-dir paths resolve correctly via the fixtures."""
    return build_registry(Path("/nonexistent"))  # builtins only


@pytest.fixture
def service(tmp_home: Path, tmp_state: Path, registry: tuple[Tool, ...]) -> ProfileService:
    store = FileProfileStore(tmp_state)
    resolver = PathResolver(home=tmp_home)
    return ProfileService(store, resolver, registry)


# ---------------- detect_installed ----------------


def test_detect_installed_finds_seeded_tools(service: ProfileService, tmp_home: Path) -> None:
    tools = service.detect_installed()
    ids = {t.id for t in tools}
    assert "claude" in ids
    assert "copilot" in ids


def test_detect_installed_skips_missing(
    tmp_home: Path,
    tmp_state: Path,
    registry: tuple[Tool, ...],
) -> None:
    shutil.rmtree(tmp_home / ".claude")
    store = FileProfileStore(tmp_state)
    resolver = PathResolver(home=tmp_home)
    service = ProfileService(store, resolver, registry)
    ids = {t.id for t in service.detect_installed()}
    assert "claude" not in ids


# ---------------- init ----------------


def test_init_creates_dated_current_and_vanilla(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    name = service.init()
    assert name.endswith("-current")
    profiles = sorted(p.name for p in service.list_profiles())
    assert "vanilla" in profiles
    assert name in profiles


def test_init_replaces_live_dirs_with_links(service: ProfileService, tmp_home: Path) -> None:
    service.init()
    claude = tmp_home / ".claude"
    assert claude.is_symlink() or (IS_WINDOWS and os.path.isjunction(claude))


def test_init_active_map_uses_dated_name(
    service: ProfileService, tmp_state: Path, tmp_home: Path
) -> None:
    name = service.init()
    store = FileProfileStore(tmp_state)
    active = store.get_active()
    assert active  # not empty
    for active_profile in active.values():
        assert active_profile == name


def test_init_refuses_when_already_initialized(service: ProfileService) -> None:
    service.init()
    with pytest.raises(StateAlreadyInitializedError):
        service.init()


def test_init_rejects_non_dir_live_path(
    tmp_home: Path, tmp_state: Path, registry: tuple[Tool, ...]
) -> None:
    """A live path that's a regular file (not link, not dir) must fail pre-flight.

    detect_installed includes any path resolver.exists() reports as present —
    including regular files. Without the is_dir() pre-flight, init() would
    create the dated profile, then fail mid-_capture_tool when move_or_seed_dir
    hit the file, leaving switcher partially initialized and a retry blocked
    by StateAlreadyInitializedError.
    """
    claude = tmp_home / ".claude"
    shutil.rmtree(claude)
    claude.write_text("not a directory")
    store = FileProfileStore(tmp_state)
    resolver = PathResolver(home=tmp_home)
    service = ProfileService(store, resolver, registry)
    with pytest.raises(PathNotADirectoryError):
        service.init()
    # Pre-flight runs before any _store.create(), so no profiles persisted
    assert not store.list()


def test_init_refuses_when_live_dir_already_linked(
    tmp_home: Path,
    tmp_state: Path,
    registry: tuple[Tool, ...],
) -> None:
    # Pre-link ~/.claude → somewhere harmless
    target = tmp_home / "elsewhere"
    target.mkdir()
    claude = tmp_home / ".claude"
    shutil.rmtree(claude)
    if IS_WINDOWS:
        from switcher.links import link_dir

        link_dir(target, claude)
    else:
        claude.symlink_to(target, target_is_directory=True)
    store = FileProfileStore(tmp_state)
    resolver = PathResolver(home=tmp_home)
    service = ProfileService(store, resolver, registry)
    with pytest.raises(AlreadyLinkedError):
        service.init()


# ---------------- use ----------------


def test_use_switches_active_to_target(service: ProfileService, tmp_state: Path) -> None:
    service.init()
    service.use("vanilla")
    active = FileProfileStore(tmp_state).get_active()
    for v in active.values():
        assert v == "vanilla"


def test_use_with_only_targets_subset(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    name = service.init()
    service.use("vanilla", only=["claude"])
    active = FileProfileStore(tmp_state).get_active()
    assert active["claude"] == "vanilla"
    assert active["copilot"] == name  # copilot stays on the original profile


def test_use_unknown_profile_raises(service: ProfileService) -> None:
    service.init()
    with pytest.raises(UnknownProfileError):
        service.use("nonexistent")


def test_use_only_with_tool_not_in_profile_raises(service: ProfileService) -> None:
    service.init()
    with pytest.raises(ToolNotInProfileError):
        service.use("vanilla", only=["nonexistent_tool"])


def test_use_before_init_raises(service: ProfileService) -> None:
    with pytest.raises(StateNotInitializedError):
        service.use("vanilla")


def test_use_is_idempotent(service: ProfileService, tmp_state: Path) -> None:
    service.init()
    service.use("vanilla")
    service.use("vanilla")  # again
    active = FileProfileStore(tmp_state).get_active()
    for v in active.values():
        assert v == "vanilla"


def test_use_pre_validates_tools_before_mutating(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """A profile with a stale tool id must fail before any live link changes.

    Without pre-flight validation, the swap loop would process tools in
    sorted order: 'claude' would switch successfully, then 'ghost' would
    raise UnknownToolError, leaving the filesystem half-switched. Pre-flight
    must reject the call without touching any live link.
    """
    service.init()
    store = FileProfileStore(tmp_state)
    # Hand-craft a profile dir that references a tool not in the registry.
    store.create("stale", {"claude": True, "ghost": True})
    (store.profile_dir("stale") / "claude").mkdir()
    claude_link = tmp_home / ".claude"
    original_target = claude_link.resolve()
    with pytest.raises(UnknownToolError):
        service.use("stale")
    assert claude_link.resolve() == original_target


def test_use_pre_validates_target_subdirs_before_mutating(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """A profile missing one tool's profile_subdir must fail before any swap.

    Setup: only seed 'claude' subdir; intentionally omit copilot's subdirs.
    Sorted target order is ['claude', 'copilot'] — without pre-flight 2,
    swap_link succeeds for claude (its subdir exists) and then raises on
    copilot's missing subdir, leaving claude pointed at the partial profile.
    Pre-flight must reject the whole call without touching any link.
    """
    service.init()
    store = FileProfileStore(tmp_state)
    store.create("partial", {"claude": True, "copilot": True})
    (store.profile_dir("partial") / "claude").mkdir()
    # Intentionally do NOT create copilot-auth / copilot-config subdirs.
    claude_link = tmp_home / ".claude"
    original_target = claude_link.resolve()
    with pytest.raises(PathNotADirectoryError):
        service.use("partial")
    assert claude_link.resolve() == original_target


# ---------------- save ----------------


def test_save_snapshots_live_state(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    service.init()
    # Modify the live config (writes through the symlink)
    (tmp_home / ".claude" / "marker.txt").write_text("hello")
    service.save("snapshot")
    snap_marker = FileProfileStore(tmp_state).profile_dir("snapshot") / "claude" / "marker.txt"
    assert snap_marker.read_text() == "hello"


def test_save_rejects_existing_profile(service: ProfileService) -> None:
    service.init()
    service.save("snap1")
    with pytest.raises(ProfileExistsError):
        service.save("snap1")


def test_save_before_init_raises(service: ProfileService) -> None:
    with pytest.raises(StateNotInitializedError):
        service.save("any")


def test_save_rejects_non_dir_live_path(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """If a live config 'dir' is actually a file, save() must fail loudly.

    detect_installed only filters by exists(), so a regular file at the live
    path slips through. Without pre-flight, copytree is silently skipped and
    the snapshot records an empty dir — a profile that looks valid until the
    user tries to use it. Pre-flight matches move_or_seed_dir's stance.
    """
    service.init()
    # Replace the live ~/.claude link with a regular file. After init the link
    # is a symlink on POSIX (unlinkable) and a junction on Windows (Path.rmdir
    # accepts junctions; Path.unlink/DeleteFile rejects them — same split as
    # _force_remove in links.py).
    claude = tmp_home / ".claude"
    if IS_WINDOWS:
        claude.rmdir()
    else:
        claude.unlink()
    claude.write_text("not a directory")
    with pytest.raises(PathNotADirectoryError):
        service.save("snap")
    # Pre-flight runs before _store.create(), so the profile must not exist
    assert not FileProfileStore(tmp_state).profile_dir("snap").exists()


def test_save_rolls_back_on_copytree_failure(
    service: ProfileService,
    tmp_home: Path,
    tmp_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-snapshot copytree failure must clean up the partial profile.

    Pre-flight catches every static precondition (missing dirs, files at live
    paths, dangling links), but copytree can still fail for runtime reasons —
    transient I/O, permissions, concurrent deletion. Without rollback the
    persisted-but-empty profile dir would block save() retry with
    ProfileExistsError. Mirrors store.create's own metadata-write rollback.
    """
    service.init()

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("simulated transient I/O failure")

    monkeypatch.setattr("switcher.service.shutil.copytree", boom)
    with pytest.raises(OSError, match="simulated"):
        service.save("snap")
    assert not FileProfileStore(tmp_state).profile_dir("snap").exists()


@pytest.mark.skipif(
    IS_WINDOWS,
    reason="POSIX dangling-symlink semantics: Path.symlink_to needs Developer Mode "
    "on Windows, and broken junctions don't surface as is_symlink() in detect_installed",
)
def test_save_rejects_dangling_symlink_live_path(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """A dangling symlink at a live path must fail loud, not snapshot empty.

    detect_installed routes through resolver.exists() (`exists() or is_symlink()`),
    so a broken link is reported as installed — but plain `live.exists()` in
    the pre-flight would skip it, leaving a snapshot with the tool listed in
    its tools dict but no data. The pre-flight must use the same surface as
    detect_installed so the two stay in lockstep.
    """
    service.init()
    claude = tmp_home / ".claude"
    claude.unlink()
    claude.symlink_to(tmp_home / "missing_target", target_is_directory=True)
    with pytest.raises(PathNotADirectoryError):
        service.save("snap")
    assert not FileProfileStore(tmp_state).profile_dir("snap").exists()


# ---------------- create ----------------


def test_create_makes_profile_with_credentials_seeded(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    name = service.init()
    store = FileProfileStore(tmp_state)
    cred_src = store.profile_dir(name) / "claude" / ".credentials.json"
    cred_src.write_text('{"token": "abc"}')
    service.create("experiment")
    cred_dst = store.profile_dir("experiment") / "claude" / ".credentials.json"
    assert cred_dst.read_text() == '{"token": "abc"}'


def test_create_skips_missing_credentials(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """If the active profile has no credential file yet, create() must not error.

    The profile_subdir layout must still be materialized — _seed_credentials
    creates the dirs unconditionally so a later use()/save() against this
    profile finds the directory structure it expects.
    """
    service.init()
    service.create("fresh")
    fresh_dir = FileProfileStore(tmp_state).profile_dir("fresh")
    assert (fresh_dir / "claude").is_dir()  # subdir materialized
    assert not (fresh_dir / "claude" / ".credentials.json").exists()


def test_create_rejects_existing_profile(service: ProfileService) -> None:
    service.init()
    service.create("expt")
    with pytest.raises(ProfileExistsError):
        service.create("expt")


def test_create_includes_uninstalled_active_tools(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """A tool in active but not currently installed must still land in the new profile.

    create() is a state-store data copy, not a live-config operation.
    Tying its tool set to detect_installed() would let a temporarily
    uninstalled tool's credentials evaporate the next time the user
    creates a profile. The tool set must come from the active map.
    """
    service.init()
    # Uninstall copilot live (but it's still in active from init).
    # After init, the live path is a junction (Windows) or symlink (POSIX) into
    # the captured profile. shutil.rmtree refuses both shapes -- it raises
    # "Cannot call rmtree on a symbolic link" because os.path.islink returns
    # True for both classic symlinks and (per Python 3.13's ntpath) Windows
    # junctions. Use the link-aware removal path on each platform.
    if IS_WINDOWS:
        copilot_live = tmp_home / "AppData" / "Local" / "github-copilot"
        # Junction: rmdir works (RemoveDirectory handles the reparse point);
        # DeleteFile (Path.unlink) and shutil.rmtree do not.
        if os.path.isjunction(copilot_live):
            copilot_live.rmdir()
        elif copilot_live.exists():
            shutil.rmtree(copilot_live)
    else:
        # Live link → still appears as a link to a now-missing target
        copilot_link = tmp_home / ".copilot"
        if copilot_link.is_symlink():
            copilot_link.unlink()
        elif copilot_link.exists():
            shutil.rmtree(copilot_link)
    service.create("backup")
    backup = FileProfileStore(tmp_state).get("backup")
    assert "copilot" in backup.tools
    assert "claude" in backup.tools


def test_create_rolls_back_on_seed_failure(
    service: ProfileService,
    tmp_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure mid-seed must remove the half-built profile so retry isn't blocked.

    Without rollback, the profile directory would persist after the failure
    and a retry would hit ProfileExistsError instead of letting the user
    try again. Mirrors save()'s rollback contract.
    """
    service.init()

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated mid-seed failure")

    monkeypatch.setattr("switcher.service.shutil.copy2", boom)
    # Seed the source profile with a credential so copy2 actually fires
    store = FileProfileStore(tmp_state)
    active_name = next(iter(store.get_active().values()))
    cred_src = store.profile_dir(active_name) / "claude" / ".credentials.json"
    cred_src.write_text('{"token": "x"}')
    with pytest.raises(OSError, match="simulated"):
        service.create("doomed")
    assert not store.profile_dir("doomed").exists()


def test_create_before_init_raises(service: ProfileService) -> None:
    with pytest.raises(StateNotInitializedError):
        service.create("anything")


# ---------------- which ----------------


def test_which_returns_active_profile(service: ProfileService) -> None:
    name = service.init()
    assert service.which("claude") == name


def test_which_unknown_tool_raises(service: ProfileService) -> None:
    """A tool ID not in the registry is a typo, not a known-but-inactive tool.

    UnknownToolError exists for exactly this case; routing it through
    ToolHasNoActiveProfileError would mask typos as "no profile" answers.
    """
    service.init()
    with pytest.raises(UnknownToolError):
        service.which("nonexistent_tool")


def test_which_registered_but_inactive_tool_raises(
    tmp_home: Path, tmp_state: Path, registry: tuple[Tool, ...]
) -> None:
    """Registered tool that wasn't installed at init time is missing from
    active — caller learns it's *registered* but inactive, not unknown."""
    # Make claude appear uninstalled so init() doesn't add it to active
    shutil.rmtree(tmp_home / ".claude")
    store = FileProfileStore(tmp_state)
    resolver = PathResolver(home=tmp_home)
    service = ProfileService(store, resolver, registry)
    service.init()
    with pytest.raises(ToolHasNoActiveProfileError):
        service.which("claude")


def test_which_before_init_raises(service: ProfileService) -> None:
    with pytest.raises(StateNotInitializedError):
        service.which("claude")


# ---------------- rename ----------------


def test_rename_active_profile_relinks(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    name = service.init()
    service.rename(name, "client-A")
    store = FileProfileStore(tmp_state)
    active = store.get_active()
    assert active["claude"] == "client-A"
    # Live link points at the renamed dir
    claude_link = tmp_home / ".claude"
    expected = (store.profile_dir("client-A") / "claude").resolve()
    assert claude_link.resolve() == expected


def test_rename_unknown_raises(service: ProfileService) -> None:
    service.init()
    with pytest.raises(UnknownProfileError):
        service.rename("missing", "new")


def test_rename_to_existing_raises(service: ProfileService) -> None:
    service.init()
    with pytest.raises(ProfileExistsError):
        service.rename("vanilla", "vanilla")  # already exists


def test_rename_before_init_raises(service: ProfileService) -> None:
    with pytest.raises(StateNotInitializedError):
        service.rename("a", "b")


def test_rename_repoints_orphan_active_entries(service: ProfileService, tmp_state: Path) -> None:
    """An active entry for a tool no longer in the registry must still be
    re-pointed to the new profile name. Otherwise the active map keeps a
    reference to the renamed-away ``old``, which no longer exists in the
    store — a silent inconsistency that surfaces on the next use()/which().
    """
    name = service.init()
    store = FileProfileStore(tmp_state)
    active = store.get_active()
    active["ghost"] = name  # orphan: not in the registry
    store.set_active(active)
    service.rename(name, "client-A")
    after = store.get_active()
    assert after["ghost"] == "client-A"
    assert after["claude"] == "client-A"


def test_rename_remains_recoverable_when_swap_link_fails(
    service: ProfileService,
    tmp_home: Path,
    tmp_state: Path,
) -> None:
    """If swap_link fails mid-rename, the active map MUST already say `new`
    so that ``use(<new>)`` is a clean idempotent recovery path.

    Without ordering set_active before the swap loop, a mid-loop swap_link
    failure leaves active still pointing at ``old`` — which no longer
    exists in the store, since store.rename already moved it. Re-running
    ``rename(old, new)`` would raise UnknownProfileError, leaving the user
    with no programmatic recovery.
    """
    name = service.init()

    call_count = {"n": 0}

    def boom(target: Path, live: Path) -> None:
        call_count["n"] += 1
        raise OSError("simulated swap failure")

    # Patch swap_link via unittest.mock.patch.object scoped to this `with`
    # block. Earlier versions of this test used the shared monkeypatch
    # fixture and called monkeypatch.undo() to restore swap_link before
    # the recovery service.use() call -- but on Windows that ALSO undid
    # the conftest fixture's USERPROFILE/HOME env-var setup (since the
    # monkeypatch fixture is shared with conftest), causing service.use()
    # to expand `~` against the runner's REAL profile dir and create
    # junctions in the wrong filesystem location entirely. patch.object's
    # context-manager teardown only undoes our specific replacement,
    # leaving fixture state alone.
    import switcher.service as svc

    with patch.object(svc, "swap_link", new=boom):
        with pytest.raises(OSError, match="simulated"):
            service.rename(name, "client-A")

        # After the failure: store dir was renamed, active says new
        store = FileProfileStore(tmp_state)
        assert store.profile_dir("client-A").exists()
        assert not store.profile_dir(name).exists()
        after_active = store.get_active()
        assert all(v == "client-A" for v in after_active.values())

    # And `use(<new>)` must be a clean recovery path
    service.use("client-A")
    claude_link = tmp_home / ".claude"
    expected = (store.profile_dir("client-A") / "claude").resolve()
    assert claude_link.resolve() == expected


def test_rename_pre_validates_live_paths_are_links(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """A real directory at a live path must fail BEFORE store.rename mutates anything.

    Without pre-flight, store.rename would succeed; swap_link would then
    refuse with IsADirectoryError on the first affected tool, leaving the
    rename half-applied (profile dir moved, live links stale).
    """
    name = service.init()
    # Replace the claude symlink with a real directory
    claude = tmp_home / ".claude"
    if claude.is_symlink() or (IS_WINDOWS and os.path.isjunction(claude)):
        claude.unlink() if not IS_WINDOWS else claude.rmdir()
    claude.mkdir()
    with pytest.raises(PathNotADirectoryError):
        service.rename(name, "client-A")
    # Pre-flight ran before store.rename, so the old profile is still there
    store = FileProfileStore(tmp_state)
    assert store.profile_dir(name).exists()
    assert not store.profile_dir("client-A").exists()


# ---------------- delete ----------------


def test_delete_inactive_profile_succeeds(service: ProfileService) -> None:
    name = service.init()
    service.use("vanilla")  # switch off `name`, freeing it for delete
    service.delete(name)
    assert name not in [p.name for p in service.list_profiles()]


def test_delete_active_profile_refuses(service: ProfileService) -> None:
    name = service.init()  # name is active for everything
    with pytest.raises(ProfileIsActiveError):
        service.delete(name)


def test_delete_unknown_raises(service: ProfileService) -> None:
    service.init()
    with pytest.raises(UnknownProfileError):
        service.delete("missing")


def test_delete_before_init_raises(service: ProfileService) -> None:
    with pytest.raises(StateNotInitializedError):
        service.delete("anything")
