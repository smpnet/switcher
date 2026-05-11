# pyright: reportPrivateUsage=none
"""Spec §3.2 — save() snapshots only managed tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from switcher.errors import NoToolsManagedError
from switcher.paths import PathResolver
from switcher.registry import build_registry
from switcher.service import ProfileService
from switcher.store import FileProfileStore


@pytest.fixture
def service_with_both_tools(tmp_home: Path, tmp_state: Path) -> ProfileService:
    store = FileProfileStore(tmp_state)
    service = ProfileService(
        store,
        PathResolver(home=tmp_home),
        build_registry(tmp_state / "registry.d"),
    )
    service.init()
    return service


@pytest.fixture
def service_with_both_tools_then_unmanage_claude(
    service_with_both_tools: ProfileService,
) -> ProfileService:
    service = service_with_both_tools
    active = service._store.get_active()
    cache = service.get_active_live_paths()
    del active["claude"]
    cache.pop("claude", None)
    service._store.set_active_state(active, cache)
    return service


def test_save_snapshots_only_managed_tools(
    service_with_both_tools_then_unmanage_claude: ProfileService,
) -> None:
    """active = {copilot}, both still installed → save captures only copilot."""
    service = service_with_both_tools_then_unmanage_claude
    service.save("test-snap")

    profile_dir = service._store.profile_dir("test-snap")
    assert (profile_dir / "copilot-config").is_dir()
    assert not (profile_dir / "claude").exists()


def test_save_with_empty_active_map_raises(
    service_with_both_tools: ProfileService,
) -> None:
    service = service_with_both_tools
    service._store.set_active_state({}, {})
    with pytest.raises(NoToolsManagedError):
        service.save("would-be-empty")


def test_save_when_no_managed_tools_are_installed_raises(
    service_with_both_tools: ProfileService, tmp_home: Path
) -> None:
    """active has entries but every managed tool's live path is gone.

    Without this guard, save() silently writes an empty profile that
    looks valid until the user tries to use() it — same silent-empty
    failure mode §3.5 closes off for the empty-active-map case.
    Reachable via external uninstall of every managed tool or a
    registry reshuffle that drops every managed tool id.
    """
    service = service_with_both_tools
    # Remove every live symlink init() installed. detect_installed() now
    # returns [], so the managed-filter intersection is empty.
    (tmp_home / ".claude").unlink()
    (tmp_home / ".copilot").unlink()
    assert service._store.get_active(), "active map should still be populated"
    with pytest.raises(NoToolsManagedError):
        service.save("would-be-empty")
