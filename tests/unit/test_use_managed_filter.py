# pyright: reportPrivateUsage=none
"""Spec §3.1 — use() filters by active.keys() (durability fix)."""

from __future__ import annotations

from pathlib import Path

import pytest

from switcher.errors import (
    NoToolsManagedError,
    ToolNotInProfileError,
    ToolNotManagedError,
)
from switcher.paths import PathResolver
from switcher.registry import build_registry
from switcher.service import ProfileService
from switcher.store import FileProfileStore


@pytest.fixture
def service_with_both_tools(tmp_home: Path, tmp_state: Path) -> ProfileService:
    """Build a service against the conftest tmp_home/tmp_state, then init.

    After init both claude and copilot are managed and pointed at the
    dated-current profile; vanilla also exists.
    """
    store = FileProfileStore(tmp_state)
    service = ProfileService(
        store,
        PathResolver(home=tmp_home),
        build_registry(tmp_state / "registry.d"),
    )
    service.init()
    return service


def _unmanage_claude(service: ProfileService) -> None:
    """Simulate `unmanage claude` ahead of T16 by mutating active state directly."""
    active = service._store.get_active()
    cache = service.get_active_live_paths()
    del active["claude"]
    cache.pop("claude", None)
    service._store.set_active_state(active, cache)


def test_use_with_full_profile_only_switches_managed_tools(
    service_with_both_tools: ProfileService,
) -> None:
    """After dropping claude from active, use(vanilla) must not re-activate it."""
    service = service_with_both_tools
    _unmanage_claude(service)
    assert "claude" not in service._store.get_active()

    service.use("vanilla")

    active_after = service._store.get_active()
    assert "claude" not in active_after
    assert active_after.get("copilot") == "vanilla"


def test_use_only_requested_tool_not_managed_raises(
    service_with_both_tools: ProfileService,
) -> None:
    service = service_with_both_tools
    _unmanage_claude(service)

    with pytest.raises(ToolNotManagedError):
        service.use("vanilla", only=["claude"])


def test_use_only_requested_tool_not_in_profile_raises(
    service_with_both_tools: ProfileService,
) -> None:
    service = service_with_both_tools
    with pytest.raises(ToolNotInProfileError):
        service.use("vanilla", only=["nonexistent"])


def test_use_with_empty_active_map_raises(
    service_with_both_tools: ProfileService,
) -> None:
    service = service_with_both_tools
    service._store.set_active_state({}, {})
    with pytest.raises(NoToolsManagedError):
        service.use("vanilla")


def test_use_with_profile_containing_no_managed_tools_raises(
    service_with_both_tools: ProfileService,
) -> None:
    """Consultant-flagged edge: active is non-empty (so _require_managed passes)
    but the chosen profile contains zero currently-managed tools. Default
    target_ids would be empty -> silent no-op. Must raise instead.
    """
    service = service_with_both_tools
    _unmanage_claude(service)

    # Build a profile whose tools dict has ONLY claude. Intersect with
    # active = {copilot} → empty target_ids.
    service._store.create("claude-only", {"claude": True})

    with pytest.raises(NoToolsManagedError):
        service.use("claude-only")
