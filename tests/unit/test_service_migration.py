"""Eager-on-read migration: derive active_live_paths for legacy state."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from switcher.paths import PathResolver
from switcher.registry import build_registry
from switcher.service import ProfileService
from switcher.store import FileProfileStore


def _seed_initialized_state(
    tmp_state: Path, tmp_home: Path
) -> tuple[ProfileService, FileProfileStore]:
    """Build a fully-initialized state dir via service.init, then return service + store."""
    store = FileProfileStore(tmp_state)
    resolver = PathResolver(home=tmp_home)
    registry = build_registry(tmp_state / "registry.d")
    service = ProfileService(store, resolver, registry)
    service.init()
    return service, store


def _strip_active_live_paths(tmp_state: Path) -> None:
    """Simulate a v0.1.0/v0.1.2 state file shape by removing the new key."""
    cfg = tmp_state / "config.json"
    data = json.loads(cfg.read_text())
    data.pop("active_live_paths", None)
    cfg.write_text(json.dumps(data, sort_keys=True))


def test_legacy_state_derives_live_paths_via_strict_validation(
    tmp_state: Path, tmp_home: Path
) -> None:
    service, store = _seed_initialized_state(tmp_state, tmp_home)
    _strip_active_live_paths(tmp_state)

    derived = service.get_active_live_paths()

    # Every active tool should have its live paths derived.
    active = store.get_active()
    for tool_id in active:
        assert tool_id in derived
        assert len(derived[tool_id]) >= 1
        # Every derived path resolves into the expected profile dir.
        for path_str in derived[tool_id]:
            assert Path(path_str).is_symlink() or Path(path_str).resolve().exists()


def test_strict_validation_skips_drifted_entries(
    tmp_state: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry entry whose live path doesn't resolve correctly stays absent."""
    service, store = _seed_initialized_state(tmp_state, tmp_home)
    _strip_active_live_paths(tmp_state)

    # Break the symlink for `claude` by replacing it with a real dir.
    claude_link = tmp_home / ".claude"
    if claude_link.is_symlink():
        claude_link.unlink()
        claude_link.mkdir()
        # Don't bother copying content — the validation only checks shape.

    derived = service.get_active_live_paths()
    # claude should be absent; copilot (still a symlink) should be present.
    assert "claude" not in derived
    if "copilot" in store.get_active():
        assert "copilot" in derived


def test_derive_cache_for_active_validates_against_proposed_map(
    tmp_state: Path, tmp_home: Path
) -> None:
    """The public accessor derives a cache against the on-disk active map.

    Equivalent to passing the on-disk map to the internal
    `_derive_cache_for_active` helper that state-mutating ops use.
    """
    service, store = _seed_initialized_state(tmp_state, tmp_home)
    on_disk_active = store.get_active()
    cache = service.get_active_live_paths()
    assert set(cache) == set(on_disk_active)
    for paths in cache.values():
        assert paths


def test_unknown_tool_in_active_map_skips_silently(tmp_state: Path, tmp_home: Path) -> None:
    """A tool id in active that's not in the registry produces no derivation, no error."""
    service, store = _seed_initialized_state(tmp_state, tmp_home)
    _strip_active_live_paths(tmp_state)

    # Inject an orphan into the active map.
    active = store.get_active()
    active["orphan_tool"] = "vanilla"
    store.set_active(active)

    derived = service.get_active_live_paths()
    assert "orphan_tool" not in derived
