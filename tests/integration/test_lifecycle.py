"""Full filesystem lifecycle exercise.

Runs init -> status -> use vanilla -> save snapshot -> create experiment
-> use experiment -> delete vanilla (after switching off it) -> rename.
Asserts disk state at each step: which entries are dirs, which are links,
where each link points -- for *every* registered tool, not just claude.
A regression that only breaks symlink/junction handling for one tool's
secondary config dir would otherwise slip through.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from switcher.paths import IS_WINDOWS, PathResolver
from switcher.registry import build_registry
from switcher.service import ProfileService
from switcher.store import FileProfileStore

pytestmark = pytest.mark.integration


def _is_link(p: Path) -> bool:
    return p.is_symlink() or (IS_WINDOWS and os.path.isjunction(p))


def test_full_lifecycle(tmp_home: Path, tmp_state: Path) -> None:
    store = FileProfileStore(tmp_state)
    resolver = PathResolver(home=tmp_home)
    registry = build_registry(tmp_state / "registry.d")
    service = ProfileService(store, resolver, registry)

    def assert_active_profile(profile_name: str) -> None:
        """Every registered tool's links and active-map entry point at profile_name."""
        active = store.get_active()
        for tool in registry:
            assert active[tool.id] == profile_name, (
                f"{tool.id} active={active.get(tool.id)!r}, expected {profile_name!r}"
            )
            for i, dm in enumerate(tool.config_dirs):
                live = resolver.tool_dir(tool, i)
                assert _is_link(live), f"{live} ({tool.id}) should be a link"
                target = (store.profile_dir(profile_name) / dm.profile_subdir).resolve()
                assert live.resolve() == target, f"{live} -> {live.resolve()}, expected {target}"

    # --- init ----------------------------------------------------------------
    current = service.init()
    assert sorted(p.name for p in store.list()) == sorted([current, "vanilla"])
    # init creates a profile_subdir under both profiles for every tool
    for profile in (current, "vanilla"):
        for tool in registry:
            for dm in tool.config_dirs:
                assert (store.profile_dir(profile) / dm.profile_subdir).is_dir()
    assert_active_profile(current)

    # --- use vanilla ---------------------------------------------------------
    service.use("vanilla")
    assert_active_profile("vanilla")

    # --- save snapshot -------------------------------------------------------
    # Writes through the live link land in the active profile's subdir
    (tmp_home / ".claude" / "marker.txt").write_text("snap-data")
    service.save("snap")
    snap_marker = store.profile_dir("snap") / "claude" / "marker.txt"
    assert snap_marker.read_text() == "snap-data"

    # --- create experiment + use it -----------------------------------------
    service.create("experiment")
    service.use("experiment")
    assert_active_profile("experiment")

    # --- delete vanilla (must not be active) --------------------------------
    # vanilla is no longer active (experiment is), so this should succeed
    service.delete("vanilla")
    assert "vanilla" not in {p.name for p in store.list()}

    # --- rename experiment while it's active --------------------------------
    service.rename("experiment", "client-A")
    assert_active_profile("client-A")
