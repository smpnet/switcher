# pyright: reportPrivateUsage=none
"""Unit tests for the v0.1.4 _require_managed guard (spec §3.5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from switcher.errors import NoToolsManagedError
from switcher.paths import PathResolver
from switcher.registry import build_registry
from switcher.service import ProfileService
from switcher.store import FileProfileStore


def _build_service(tmp_state: Path, tmp_home: Path) -> ProfileService:
    store = FileProfileStore(tmp_state)
    resolver = PathResolver(home=tmp_home)
    registry = build_registry(tmp_state / "registry.d")
    return ProfileService(store, resolver, registry)


def test_require_managed_raises_on_empty_active_map(
    tmp_state: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The freshly-uninstalled-no-purge case: profiles on disk, active map empty.

    Stubbing `get_active` to return {} mirrors that state without having to
    run the full uninstall path.
    """
    service = _build_service(tmp_state, tmp_home)

    def _empty_active() -> dict[str, str]:
        return {}

    monkeypatch.setattr(service._store, "get_active", _empty_active)
    with pytest.raises(NoToolsManagedError, match="no tools currently managed"):
        service._require_managed()


def test_require_managed_silent_when_active_nonempty(tmp_state: Path, tmp_home: Path) -> None:
    """Post-init active map is non-empty; the guard returns without raising."""
    service = _build_service(tmp_state, tmp_home)
    service.init()
    service._require_managed()  # must not raise
