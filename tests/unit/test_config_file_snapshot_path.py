"""Tests for the ConfigFile snapshot-path derivation helper."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from switcher.errors import StorageError
from switcher.store import FileProfileStore

IS_WINDOWS = sys.platform == "win32"


def test_snapshot_path_under_dot_switcher(tmp_path: Path) -> None:
    store = FileProfileStore(tmp_path)
    p = store.config_file_snapshot_path("workA", "claude", "claude.json")
    expected = (
        tmp_path / "profiles" / "workA" / ".switcher" / "config_files" / "claude" / "claude.json"
    )
    assert p == expected


def test_snapshot_path_validates_profile_name(tmp_path: Path) -> None:
    store = FileProfileStore(tmp_path)
    # validate_safe_name rejects path-separator-bearing inputs by raising
    # ValueError; assert on the message so a future rename of the validator's
    # error text doesn't silently break this test.
    with pytest.raises(ValueError, match="invalid name"):
        store.config_file_snapshot_path("../escape", "claude", "claude.json")


def test_snapshot_path_validates_subdir(tmp_path: Path) -> None:
    store = FileProfileStore(tmp_path)
    with pytest.raises(ValueError, match="invalid name"):
        store.config_file_snapshot_path("workA", "../escape", "claude.json")


def test_snapshot_path_validates_filename(tmp_path: Path) -> None:
    store = FileProfileStore(tmp_path)
    with pytest.raises(ValueError, match="invalid name"):
        store.config_file_snapshot_path("workA", "claude", "../escape.json")


def test_snapshot_path_does_not_create_dirs(tmp_path: Path) -> None:
    store = FileProfileStore(tmp_path)
    p = store.config_file_snapshot_path("workA", "claude", "claude.json")
    # Helper is pure path derivation; mkdir is the caller's responsibility
    # (atomic_write_file handles it).
    assert not p.exists()
    assert not p.parent.exists()


@pytest.mark.skipif(IS_WINDOWS, reason="symlinks require elevation on Windows")
@pytest.mark.parametrize(
    "ancestor_rel",
    [".switcher", ".switcher/config_files", ".switcher/config_files/claude"],
    ids=["dot-switcher", "config-files", "subdir"],
)
def test_snapshot_path_rejects_link_at_reserved_ancestor(tmp_path: Path, ancestor_rel: str) -> None:
    """Hermes pass-PR-6: a symlink at any reserved ancestor under
    ``.switcher/config_files/<subdir>`` silently redirects snapshot
    I/O outside the state store — the leaf's ``is_symlink`` check
    follows the redirection and reports a healthy regular file.
    The path builder is the single chokepoint every IO call site
    routes through, so it must refuse when any reserved ancestor
    is link/junction-shaped.
    """
    store = FileProfileStore(tmp_path)
    profile_dir = tmp_path / "profiles" / "workA"
    ancestor = profile_dir / ancestor_rel
    ancestor.parent.mkdir(parents=True, exist_ok=True)
    external = tmp_path / "outside"
    external.mkdir()
    ancestor.symlink_to(external, target_is_directory=True)

    with pytest.raises(StorageError, match="link or junction"):
        store.config_file_snapshot_path("workA", "claude", "claude.json")


@pytest.mark.skipif(IS_WINDOWS, reason="symlinks require elevation on Windows")
def test_snapshot_path_rejects_broken_link_at_reserved_ancestor(
    tmp_path: Path,
) -> None:
    """Broken (dangling) link/junction is still a redirection
    primitive; ``Path.exists()`` returns False but ``is_symlink()``
    returns True, so the builder must surface it before any IO."""
    store = FileProfileStore(tmp_path)
    profile_dir = tmp_path / "profiles" / "workA"
    profile_dir.mkdir(parents=True)
    (profile_dir / ".switcher").symlink_to(tmp_path / "does-not-exist", target_is_directory=True)

    with pytest.raises(StorageError, match="link or junction"):
        store.config_file_snapshot_path("workA", "claude", "claude.json")


def test_snapshot_path_allows_real_reserved_ancestors(tmp_path: Path) -> None:
    """Pre-existing reserved subtree as real directories must NOT
    trip the ancestor check — second snapshot writes onto an existing
    ``.switcher/config_files/<subdir>`` tree are the normal flow.
    """
    store = FileProfileStore(tmp_path)
    profile_dir = tmp_path / "profiles" / "workA"
    (profile_dir / ".switcher" / "config_files" / "claude").mkdir(parents=True)

    p = store.config_file_snapshot_path("workA", "claude", "claude.json")
    assert p == profile_dir / ".switcher" / "config_files" / "claude" / "claude.json"


@pytest.mark.skipif(IS_WINDOWS, reason="symlinks require elevation on Windows")
def test_snapshot_path_rejects_link_at_profiles_parent(tmp_path: Path) -> None:
    """Hermes pass-PR-8 #2: the ancestor scan must include
    ``<state>/profiles`` itself, not just per-profile subtrees. A
    symlinked profiles root lets EVERY profile's snapshot path
    resolve into an external directory. Empirically verified pre-fix:
    ``atomic_write_file`` on the returned path created the snapshot at
    ``<external>/workA/.switcher/config_files/claude/claude.json``.
    """
    store = FileProfileStore(tmp_path)
    external = tmp_path / "outside"
    external.mkdir()
    (tmp_path / "profiles").symlink_to(external, target_is_directory=True)

    with pytest.raises(StorageError, match="link or junction"):
        store.config_file_snapshot_path("workA", "claude", "claude.json")


@pytest.mark.skipif(IS_WINDOWS, reason="symlinks require elevation on Windows")
def test_snapshot_path_rejects_link_at_profile_dir(tmp_path: Path) -> None:
    """Hermes pass-PR-7 #1: the ancestor scan must include
    ``profile_dir`` itself, not just the reserved subtree underneath.
    A symlinked ``<state>/profiles/<name>`` lets EVERY snapshot path
    derived for that profile resolve into an external directory; the
    leaf-only ``is_symlink`` guard at read/write sites never fires
    because they follow the link transparently. Empirically verified
    pre-fix: ``atomic_write_file`` on the returned path created the
    snapshot at ``<external>/.switcher/config_files/claude/claude.json``.
    """
    store = FileProfileStore(tmp_path)
    (tmp_path / "profiles").mkdir(parents=True)
    external = tmp_path / "outside"
    external.mkdir()
    (tmp_path / "profiles" / "workA").symlink_to(external, target_is_directory=True)

    with pytest.raises(StorageError, match="link or junction"):
        store.config_file_snapshot_path("workA", "claude", "claude.json")
