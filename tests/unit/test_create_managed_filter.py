# pyright: reportPrivateUsage=none
"""Spec §3.5 — create() refuses on empty active map."""

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


def test_create_on_empty_active_raises(
    service_with_both_tools: ProfileService,
) -> None:
    """Active map cleared (post-uninstall-no-purge state) — create must fail loud.

    Without the guard, create() silently writes an empty-tools profile that
    looks valid until the user tries to use() it; spec §3.5 mandates a hard
    error across save/create/use.
    """
    service = service_with_both_tools
    service._store.set_active_state({}, {})
    with pytest.raises(NoToolsManagedError):
        service.create("would-be-empty")
