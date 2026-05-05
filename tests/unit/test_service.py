"""Service-level operations: detect, init, use, save, create, which, rename, delete."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from switcher.errors import (
    AlreadyLinkedError,
    PathNotADirectoryError,
    ProfileExistsError,
    StateAlreadyInitializedError,
    StateNotInitializedError,
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
    # Replace the live ~/.claude link with a regular file
    claude = tmp_home / ".claude"
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
