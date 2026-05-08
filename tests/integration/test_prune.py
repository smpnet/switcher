# pyright: reportPrivateUsage=none
"""End-to-end prune flow (spec §5).

A few tests reach into private store internals (`s._store.get_active`) and
private service helpers (`s._compute_orphans` via monkeypatch) for setup
that the public API doesn't expose.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from switcher.errors import ProfileIsActiveError, PruneError
from switcher.paths import PathResolver
from switcher.registry import build_registry
from switcher.service import ProfileService
from switcher.store import FileProfileStore

pytestmark = pytest.mark.integration


def _service(tmp_state: Path, tmp_home: Path) -> ProfileService:
    return ProfileService(
        FileProfileStore(tmp_state),
        PathResolver(home=tmp_home),
        build_registry(tmp_state / "registry.d"),
    )


def test_prune_removes_orphan_profiles(tmp_state: Path, tmp_home: Path) -> None:
    s = _service(tmp_state, tmp_home)
    s.init()
    # Create two orphan profiles (neither is in any tool's active map).
    s.create("orphan-a")
    s.create("orphan-b")

    report = s.prune(force=True)

    # After init, active points each tool to <today>-current; vanilla is NOT
    # in active.values() so it qualifies as an orphan too (spec §5: orphan =
    # on-disk profile name ∉ active.values()).
    assert sorted(report.deleted) == ["orphan-a", "orphan-b", "vanilla"]
    assert not (tmp_state / "profiles" / "orphan-a").exists()
    assert not (tmp_state / "profiles" / "orphan-b").exists()
    assert not (tmp_state / "profiles" / "vanilla").exists()


def test_prune_empty_set_returns_empty_report(tmp_state: Path, tmp_home: Path) -> None:
    s = _service(tmp_state, tmp_home)
    s.init()
    # vanilla becomes orphan after init (since active points to <today>-current).
    # So prune --force WILL delete vanilla. To get an empty orphan set, switch
    # active over to vanilla first.
    s.use("vanilla")
    # Now <today>-current is orphan. prune should delete it.
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    expected_orphan = f"{today}-current"
    s.prune(force=True)
    assert not (tmp_state / "profiles" / expected_orphan).exists()
    # Re-running: no orphans now.
    report = s.prune(force=True)
    assert report.deleted == []


def test_prune_service_layer_guard_reachable(
    tmp_state: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense-in-depth: monkey-patch orphan computation to return an active profile.
    The service-layer guard in ProfileService.delete must catch it."""
    s = _service(tmp_state, tmp_home)
    s.init()
    # Compute the active profile name.
    active_profile = next(iter(s._store.get_active().values()))

    # Monkeypatch _compute_orphans to return the active profile.
    monkeypatch.setattr(s, "_compute_orphans", lambda: [active_profile])

    with pytest.raises(ProfileIsActiveError):
        s.prune(force=True)
    # Active profile is still on disk.
    assert (tmp_state / "profiles" / active_profile).exists()


def test_prune_dry_run_makes_no_changes(tmp_state: Path, tmp_home: Path) -> None:
    s = _service(tmp_state, tmp_home)
    s.init()
    s.create("orphan-a")

    report = s.prune(force=True, dry_run=True)

    assert report.deleted == []
    assert "orphan-a" in report.sizes_bytes
    assert (tmp_state / "profiles" / "orphan-a").exists()


def test_prune_without_force_in_non_tty_raises(
    tmp_state: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _service(tmp_state, tmp_home)
    s.init()
    s.create("orphan-a")

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    with pytest.raises(PruneError, match="--force"):
        s.prune(force=False)
    # Orphan still on disk.
    assert (tmp_state / "profiles" / "orphan-a").exists()


def test_prune_without_force_in_tty_still_refuses_at_service_layer(
    tmp_state: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Service trusts force=True for confirmation; CLI must always pass force=True."""
    s = _service(tmp_state, tmp_home)
    s.init()
    s.create("orphan-a")

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    with pytest.raises(PruneError, match="requires force=True"):
        s.prune(force=False)
    assert (tmp_state / "profiles" / "orphan-a").exists()


def test_prune_wraps_filesystem_oserror_as_prune_error(
    tmp_state: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Filesystem failures during orphan-walk surface as PruneError, not raw OSError.
    Mirrors the StorageError / UninstallPreflightError contract."""
    s = _service(tmp_state, tmp_home)
    s.init()
    s.create("orphan-a")

    # Force size computation to fail mid-walk (e.g. permission denied on a
    # profile subdir). Must surface as PruneError — not raw OSError.
    def _boom(self: ProfileService, name: str) -> int:
        raise PermissionError(f"denied: {name}")

    monkeypatch.setattr(ProfileService, "_profile_size_bytes", _boom)

    with pytest.raises(PruneError, match="orphan walk"):
        s.prune(force=True)
