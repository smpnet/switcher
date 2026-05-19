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


@pytest.mark.skipif(
    not hasattr(__import__("os"), "geteuid"),
    reason="POSIX mode bits not meaningful on Windows",
)
def test_atomic_write_preserves_existing_mode(tmp_path: Path):
    """Pre-existing target file's mode survives the rename, even though
    mkstemp would otherwise default to 0o600."""
    target = tmp_path / "out.json"
    target.write_bytes(b"OLD")
    target.chmod(0o644)
    atomic_write_file(target, b"NEW")
    mode = target.stat().st_mode & 0o777
    assert mode == 0o644, f"expected 0o644, got {oct(mode)}"


@pytest.mark.skipif(
    not hasattr(__import__("os"), "geteuid"),
    reason="POSIX mode bits not meaningful on Windows",
)
def test_atomic_write_uses_mkstemp_default_for_new_target(tmp_path: Path):
    """For a new target (no prior file), mkstemp's default 0o600 is used.
    Locks in the intentional default for sensitive snapshot files."""
    target = tmp_path / "out.json"
    atomic_write_file(target, b"NEW")
    mode = target.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_atomic_write_rejects_symlink_target(tmp_path: Path):
    """Path.replace would silently destroy the symlink and leave a regular
    file at target. Raise instead so the user sees the incompatibility.

    Asserts plain ``OSError`` rather than ``IsADirectoryError`` (a symlink
    is not a directory; the latter would shadow path-shape errors from
    ``Path.replace``).
    """
    real = tmp_path / "real.json"
    real.write_bytes(b"REAL")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(OSError, match="symlink"):
        atomic_write_file(link, b"NEW")
    # Symlink and target untouched
    assert link.is_symlink()
    assert real.read_bytes() == b"REAL"
