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
def service_with_claude_unmanaged(
    service_with_both_tools: ProfileService,
) -> ProfileService:
    """Init every registered builtin, then unmanage claude. The remaining
    active map carries codex and copilot; both will participate in any
    subsequent save."""
    service = service_with_both_tools
    active = service._store.get_active()
    cache = service.get_active_live_paths()
    del active["claude"]
    cache.pop("claude", None)
    service._store.set_active_state(active, cache)
    return service


def test_save_snapshots_only_managed_tools(
    service_with_claude_unmanaged: ProfileService,
) -> None:
    """active = registry minus claude, every tool still installed →
    save captures every managed tool (codex, copilot); claude is absent.

    The assertion intentionally does NOT enumerate every managed tool —
    `test_full_lifecycle` already validates per-tool save behavior for
    every registered builtin. This test specifically validates the
    claude-unmanage filtering; extending it to assert codex's presence
    would re-encode coverage that lives in the generic lifecycle test.
    """
    service = service_with_claude_unmanaged
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

    Iterates active_live_paths instead of hardcoding tool names so
    every registered builtin's live path gets removed — adding a new
    builtin must not require updating this test.
    """
    from switcher.links import remove_link

    service = service_with_both_tools
    # Remove every live link init() installed. detect_installed() now
    # returns [], so the managed-filter intersection is empty.
    for paths in service.get_active_live_paths().values():
        for raw_path in paths:
            live = Path(raw_path)
            if service._resolver.is_link(live):
                remove_link(live)
    assert service._store.get_active(), "active map should still be populated"
    with pytest.raises(NoToolsManagedError):
        service.save("would-be-empty")
