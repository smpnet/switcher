# pyright: reportPrivateUsage=none
"""Unit tests for v0.1.4 FS-truth migration helpers (spec §4.2)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from switcher.links import _create_junction
from switcher.paths import IS_WINDOWS, PathResolver
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


def _replace_with_symlink(path: Path, target: Path) -> None:
    """Replace whatever is at `path` (real dir, file, or symlink/junction)
    with a directory link pointing at `target`. Idempotent — handles the
    conftest seed that pre-creates real dirs at live-path locations.

    On Windows uses a directory junction so the suite runs in CI without
    Developer Mode / elevation (matches the repo pattern in test_rescan.py).
    """
    if path.is_symlink() or (IS_WINDOWS and os.path.isjunction(path)):
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()
    if IS_WINDOWS:
        _create_junction(target, path)
    else:
        path.symlink_to(target)


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
    # be present so the legacy github-copilot orphan can be rescued.
    resolved = {p.resolve() for p in parents}
    assert tmp_home.resolve() in resolved
    assert (tmp_home / ".config").resolve() in resolved


def test_candidate_parents_for_unknown_tool_uses_legacy_only(
    tmp_state: Path, tmp_home: Path
) -> None:
    service = _make_service(tmp_state, tmp_home)
    parents = service._candidate_parents_for("nonexistent")
    # No registry entry → only legacy parents (e.g., ~/.config on POSIX).
    resolved = {p.resolve() for p in parents}
    assert (tmp_home / ".config").resolve() in resolved


def test_discover_empty_profile_dir_returns_empty(tmp_state: Path, tmp_home: Path) -> None:
    service = _make_service(tmp_state, tmp_home)
    # Profile doesn't exist on disk.
    assert service._discover_live_paths_for_active("copilot", "missing") == []


def test_discover_finds_symlink_into_owned_subdir(tmp_state: Path, tmp_home: Path) -> None:
    service = _make_service(tmp_state, tmp_home)
    profile_name = "test-profile"
    profile_dir = service._store.profile_dir(profile_name)
    (profile_dir / "copilot-config").mkdir(parents=True)
    # Canonical live-path basename — required by the basename filter that
    # rejects non-managed aliases. Conftest pre-seeds ~/.copilot as a real
    # dir, so replace it with a symlink.
    link = tmp_home / ".copilot"
    _replace_with_symlink(link, profile_dir / "copilot-config")

    discovered = service._discover_live_paths_for_active("copilot", profile_name)
    assert str(link) in discovered


def _seed_multi_tool_profile(service: ProfileService, tmp_home: Path) -> tuple[Path, Path]:
    """Pre-stage a profile dir with copilot+claude subdirs and live symlinks
    at canonical basenames. Returns (copilot_link, claude_link).
    """
    profile_dir = service._store.profile_dir("multi")
    (profile_dir / "copilot-config").mkdir(parents=True)
    (profile_dir / "claude").mkdir(parents=True)
    copilot_link = tmp_home / ".copilot"
    claude_link = tmp_home / ".claude"
    _replace_with_symlink(copilot_link, profile_dir / "copilot-config")
    _replace_with_symlink(claude_link, profile_dir / "claude")
    return copilot_link, claude_link


def test_discover_copilot_ignores_claude_symlinks_in_shared_profile(
    tmp_state: Path, tmp_home: Path
) -> None:
    """Copilot's discovery must NOT pick up Claude's symlink even when both
    tools' subdirs live in the same profile dir."""
    service = _make_service(tmp_state, tmp_home)
    copilot_link, claude_link = _seed_multi_tool_profile(service, tmp_home)
    copilot_paths = service._discover_live_paths_for_active("copilot", "multi")
    assert str(copilot_link) in copilot_paths
    assert str(claude_link) not in copilot_paths


def test_discover_claude_ignores_copilot_symlinks_in_shared_profile(
    tmp_state: Path, tmp_home: Path
) -> None:
    """Claude's discovery must NOT pick up Copilot's symlink even when both
    tools' subdirs live in the same profile dir."""
    service = _make_service(tmp_state, tmp_home)
    copilot_link, claude_link = _seed_multi_tool_profile(service, tmp_home)
    claude_paths = service._discover_live_paths_for_active("claude", "multi")
    assert str(claude_link) in claude_paths
    assert str(copilot_link) not in claude_paths


@pytest.mark.skipif(
    IS_WINDOWS, reason="POSIX-specific legacy parent (~/.config); Windows uses %LOCALAPPDATA%"
)
def test_discover_legacy_copilot_auth_path_after_rewrite(tmp_state: Path, tmp_home: Path) -> None:
    """After the Copilot builtin rewrite, the registry only mentions ~/.copilot.
    The orphan symlink at ~/.config/github-copilot must still be discovered."""
    service = _make_service(tmp_state, tmp_home)
    profile_name = "legacy"
    profile_dir = service._store.profile_dir(profile_name)
    (profile_dir / "copilot-config").mkdir(parents=True)
    (profile_dir / "copilot-auth").mkdir(parents=True)

    # Pre-stage two symlinks: one in the current-registry parent (~/),
    # one in the legacy parent (~/.config/).
    new_link = tmp_home / ".copilot"  # conftest pre-creates as real dir
    _replace_with_symlink(new_link, profile_dir / "copilot-config")
    config_dir = tmp_home / ".config"
    config_dir.mkdir(exist_ok=True)
    old_link = config_dir / "github-copilot"  # conftest pre-creates as real dir
    _replace_with_symlink(old_link, profile_dir / "copilot-auth")

    discovered = service._discover_live_paths_for_active("copilot", profile_name)
    assert str(new_link) in discovered
    assert str(old_link) in discovered


def test_discover_skips_broken_symlinks(tmp_state: Path, tmp_home: Path) -> None:
    """A symlink at a canonical basename whose target is missing must not
    raise — the discovery walker has to absorb the missing-target case and
    skip the entry. Using a canonical basename (`.copilot`) so the basename
    filter doesn't short-circuit before the resolve attempt."""
    service = _make_service(tmp_state, tmp_home)
    profile_name = "broken"
    profile_dir = service._store.profile_dir(profile_name)
    (profile_dir / "copilot-config").mkdir(parents=True)
    # Replace conftest-seeded ~/.copilot real dir with a broken symlink.
    broken = tmp_home / ".copilot"
    _replace_with_symlink(broken, tmp_home / "does-not-exist")

    discovered = service._discover_live_paths_for_active("copilot", profile_name)
    # The target doesn't resolve into an owned subdir → filtered out, no exception.
    assert str(broken) not in discovered


def test_discover_skips_regular_files_in_candidate_parents(tmp_state: Path, tmp_home: Path) -> None:
    service = _make_service(tmp_state, tmp_home)
    profile_name = "files"
    profile_dir = service._store.profile_dir(profile_name)
    (profile_dir / "copilot-config").mkdir(parents=True)
    # A regular file at ~/.copilot-real — not a symlink, must not be discovered.
    real = tmp_home / ".copilot-real"
    real.write_text("regular file")
    discovered = service._discover_live_paths_for_active("copilot", profile_name)
    assert str(real) not in discovered


def test_discover_rejects_non_canonical_basenames(tmp_state: Path, tmp_home: Path) -> None:
    """A user-created symlink at a non-canonical name that happens to resolve
    into the profile dir (e.g., `~/copilot-backup -> <profile>/copilot-config`)
    must NOT be captured as a managed live path — uninstall would otherwise
    delete/rename a user alias switcher never owned."""
    service = _make_service(tmp_state, tmp_home)
    profile_name = "alias"
    profile_dir = service._store.profile_dir(profile_name)
    (profile_dir / "copilot-config").mkdir(parents=True)

    # User-created backup symlink at a name switcher does NOT own.
    backup = tmp_home / "copilot-backup"
    backup.symlink_to(profile_dir / "copilot-config")

    discovered = service._discover_live_paths_for_active("copilot", profile_name)
    assert str(backup) not in discovered
