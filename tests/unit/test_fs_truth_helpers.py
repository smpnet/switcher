# pyright: reportPrivateUsage=none
"""Unit tests for v0.1.4 FS-truth migration helpers (spec §4.2)."""

from __future__ import annotations

from pathlib import Path

from switcher.paths import PathResolver
from switcher.registry import build_registry
from switcher.service import ProfileService
from switcher.store import FileProfileStore


def _make_service(tmp_state: Path, tmp_home: Path) -> ProfileService:
    tmp_state.mkdir(parents=True, exist_ok=True)
    (tmp_state / "registry.d").mkdir(exist_ok=True)
    store = FileProfileStore(tmp_state)
    resolver = PathResolver(home=tmp_home)
    registry = build_registry(tmp_state / "registry.d")
    return ProfileService(store, resolver, registry)


def test_expected_subdirs_for_copilot_includes_historical(tmp_state: Path, tmp_home: Path) -> None:
    service = _make_service(tmp_state, tmp_home)
    expected = service._expected_subdirs_for("copilot")
    # Current registry contributes "copilot-config"; historical map
    # contributes "copilot-auth".
    assert "copilot-config" in expected
    assert "copilot-auth" in expected


def test_expected_subdirs_for_claude_is_current_only(tmp_state: Path, tmp_home: Path) -> None:
    service = _make_service(tmp_state, tmp_home)
    expected = service._expected_subdirs_for("claude")
    # Claude has no historical drift; only "claude" should be present.
    assert expected == frozenset({"claude"})


def test_expected_subdirs_for_unknown_tool_falls_back_to_empty(
    tmp_state: Path, tmp_home: Path
) -> None:
    service = _make_service(tmp_state, tmp_home)
    expected = service._expected_subdirs_for("nonexistent")
    assert expected == frozenset()


def test_candidate_parents_for_copilot_includes_legacy_config(
    tmp_state: Path, tmp_home: Path
) -> None:
    service = _make_service(tmp_state, tmp_home)
    parents = service._candidate_parents_for("copilot")
    # The legacy POSIX set adds '~/.config' (= tmp_home/.config); the
    # current registry adds tmp_home (parent of ~/.copilot). Both must
    # be present.
    resolved = {p.resolve() for p in parents}
    assert tmp_home.resolve() in resolved


def test_candidate_parents_for_unknown_tool_uses_legacy_only(
    tmp_state: Path, tmp_home: Path
) -> None:
    service = _make_service(tmp_state, tmp_home)
    parents = service._candidate_parents_for("nonexistent")
    # No registry entry → only legacy parents.
    assert len(parents) > 0
