# pyright: reportPrivateUsage=none
"""Unit tests for v0.1.4 FS-truth migration helpers (spec §4.2)."""

from __future__ import annotations

import shutil
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


def _replace_with_symlink(path: Path, target: Path) -> None:
    """Replace whatever is at `path` (real dir, file, or symlink) with a
    symlink pointing at `target`. Idempotent — handles the conftest seed
    that pre-creates real dirs at live-path locations."""
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()
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


def test_discover_empty_profile_dir_returns_empty(tmp_state: Path, tmp_home: Path) -> None:
    service = _make_service(tmp_state, tmp_home)
    # Profile doesn't exist on disk.
    assert service._discover_live_paths_for_active("copilot", "missing") == []


def test_discover_finds_symlink_into_owned_subdir(tmp_state: Path, tmp_home: Path) -> None:
    service = _make_service(tmp_state, tmp_home)
    profile_name = "test-profile"
    profile_dir = service._store.profile_dir(profile_name)
    (profile_dir / "copilot-config").mkdir(parents=True)
    link = tmp_home / ".copilot-link"
    link.symlink_to(profile_dir / "copilot-config")

    discovered = service._discover_live_paths_for_active("copilot", profile_name)
    assert str(link) in discovered


def test_discover_per_tool_isolation_in_multi_tool_profile(tmp_state: Path, tmp_home: Path) -> None:
    """Copilot's discovery must NOT pick up Claude's symlink and vice versa,
    even when both tools' subdirs live in the same profile dir."""
    service = _make_service(tmp_state, tmp_home)
    profile_name = "multi"
    profile_dir = service._store.profile_dir(profile_name)
    (profile_dir / "copilot-config").mkdir(parents=True)
    (profile_dir / "claude").mkdir(parents=True)
    copilot_link = tmp_home / ".copilot-link"
    claude_link = tmp_home / ".claude-link"
    copilot_link.symlink_to(profile_dir / "copilot-config")
    claude_link.symlink_to(profile_dir / "claude")

    copilot_paths = service._discover_live_paths_for_active("copilot", profile_name)
    claude_paths = service._discover_live_paths_for_active("claude", profile_name)
    assert str(copilot_link) in copilot_paths
    assert str(claude_link) not in copilot_paths
    assert str(claude_link) in claude_paths
    assert str(copilot_link) not in claude_paths


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
    service = _make_service(tmp_state, tmp_home)
    profile_name = "broken"
    profile_dir = service._store.profile_dir(profile_name)
    (profile_dir / "copilot-config").mkdir(parents=True)
    # A symlink pointing to a non-existent target — must not raise.
    broken = tmp_home / ".broken-link"
    broken.symlink_to(tmp_home / "does-not-exist")
    # The symlink resolves to something not under profile_dir, so it
    # gets filtered out — no exception.
    discovered = service._discover_live_paths_for_active("copilot", profile_name)
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
