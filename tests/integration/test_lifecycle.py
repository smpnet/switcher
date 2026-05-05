"""Full filesystem lifecycle exercise at the ProfileService level.

Drives the service API directly -- CLI wiring (Typer command
registration, option parsing, handle_errors rendering) is the E2E
suite's scope (Task 22 in the v0.1.0 plan). Walks the full lifecycle
(init -> use -> save -> create -> use -> delete -> rename) and
asserts disk state at every step for *every* registered tool, so a
regression confined to one tool's secondary config_dir can't pass
silently.
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
    # Writes through every tool's every live link must land in the matching
    # subdir of the snapshot profile. A regression that mishandled copilot's
    # secondary config_dir (different profile_subdir, different copy logic)
    # would otherwise hide behind the .claude-only assertion.
    markers: dict[tuple[str, str], str] = {}
    for tool in registry:
        for i, dm in enumerate(tool.config_dirs):
            live = resolver.tool_dir(tool, i)
            marker = f"snap-{tool.id}-{dm.profile_subdir}"
            (live / "marker.txt").write_text(marker)
            markers[(tool.id, dm.profile_subdir)] = marker
    service.save("snap")
    for (_tool_id, subdir), marker in markers.items():
        assert (store.profile_dir("snap") / subdir / "marker.txt").read_text() == marker

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
