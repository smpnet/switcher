"""Tests for atomic_write_file."""

from __future__ import annotations

from pathlib import Path

import pytest

from switcher.links import atomic_write_file


def test_atomic_write_creates_target(tmp_path: Path):
    target = tmp_path / "out.json"
    atomic_write_file(target, b'{"a": 1}')
    assert target.read_bytes() == b'{"a": 1}'


def test_atomic_write_creates_parent_dirs(tmp_path: Path):
    target = tmp_path / "deep" / "nested" / "out.json"
    atomic_write_file(target, b"{}")
    assert target.read_bytes() == b"{}"


def test_atomic_write_replaces_existing_file(tmp_path: Path):
    target = tmp_path / "out.json"
    target.write_bytes(b"OLD")
    atomic_write_file(target, b"NEW")
    assert target.read_bytes() == b"NEW"


def test_atomic_write_leaves_no_tmp_files_on_success(tmp_path: Path):
    target = tmp_path / "out.json"
    atomic_write_file(target, b"{}")
    leftover = [p.name for p in tmp_path.iterdir() if p.name.startswith("out.json.")]
    assert leftover == []


def test_atomic_write_unlinks_tmp_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    target = tmp_path / "out.json"

    def boom(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError, match="simulated rename failure"):
        atomic_write_file(target, b"{}")
    leftover = [p.name for p in tmp_path.iterdir() if p.name.startswith("out.json.")]
    assert leftover == [], f"unexpected tmp files: {leftover}"


def test_atomic_write_target_is_a_regular_file(tmp_path: Path):
    target = tmp_path / "out.json"
    atomic_write_file(target, b"{}")
    assert target.is_file()
    assert not target.is_symlink()
