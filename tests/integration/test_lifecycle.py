"""Full filesystem lifecycle exercise.

Runs init -> status -> use vanilla -> save snapshot -> create experiment
-> use experiment -> delete vanilla (after switching off it) -> rename.
Asserts disk state at each step: which entries are dirs, which are links,
where each link points.
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
    service = ProfileService(store, resolver, build_registry(tmp_state / "registry.d"))

    # --- init ----------------------------------------------------------------
    current = service.init()
    assert _is_link(tmp_home / ".claude")
    assert (store.profile_dir(current) / "claude").is_dir()
    assert (store.profile_dir("vanilla") / "claude").is_dir()
    assert sorted(p.name for p in store.list()) == sorted([current, "vanilla"])

    # --- use vanilla ---------------------------------------------------------
    service.use("vanilla")
    active = store.get_active()
    for v in active.values():
        assert v == "vanilla"
    # Live link now points into vanilla
    assert (tmp_home / ".claude").resolve() == (store.profile_dir("vanilla") / "claude").resolve()

    # --- save snapshot -------------------------------------------------------
    (tmp_home / ".claude" / "marker.txt").write_text("snap-data")
    service.save("snap")
    snap_marker = store.profile_dir("snap") / "claude" / "marker.txt"
    assert snap_marker.read_text() == "snap-data"

    # --- create experiment + use it -----------------------------------------
    service.create("experiment")
    service.use("experiment")
    assert (tmp_home / ".claude").resolve() == (
        store.profile_dir("experiment") / "claude"
    ).resolve()

    # --- delete vanilla (must not be active) --------------------------------
    # vanilla is no longer active (experiment is), so this should succeed
    service.delete("vanilla")
    assert "vanilla" not in {p.name for p in store.list()}

    # --- rename experiment while it's active --------------------------------
    service.rename("experiment", "client-A")
    assert (tmp_home / ".claude").resolve() == (store.profile_dir("client-A") / "claude").resolve()
    active = store.get_active()
    assert active["claude"] == "client-A"
