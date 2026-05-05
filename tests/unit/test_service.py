"""Service-level operations: detect, init, use, save, create, which, rename, delete."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from switcher.errors import (
    AlreadyLinkedError,
    StateAlreadyInitializedError,
    StateNotInitializedError,
    ToolNotInProfileError,
    UnknownProfileError,
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
