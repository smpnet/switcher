"""Tests for the ConfigFile snapshot-path derivation helper."""

from __future__ import annotations

from pathlib import Path

import pytest

from switcher.store import FileProfileStore


def test_snapshot_path_under_dot_switcher(tmp_path: Path) -> None:
    store = FileProfileStore(tmp_path)
    p = store.config_file_snapshot_path("workA", "claude", "claude.json")
    expected = (
        tmp_path
        / "profiles"
        / "workA"
        / ".switcher"
        / "config_files"
        / "claude"
        / "claude.json"
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
