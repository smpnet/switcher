"""Link mechanics: symlink on POSIX, junction on Windows; atomic swap."""

import os
import sys
from pathlib import Path

import pytest

from switcher.errors import (
    AlreadyLinkedError,
    PathNotADirectoryError,
    ProfileTargetExistsError,
)
from switcher.links import link_dir, move_or_seed_dir, swap_link


def test_link_dir_creates_link_to_directory(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "marker").write_text("hello")
    link = tmp_path / "link"
    link_dir(target, link)
    # Reads through link
    assert (link / "marker").read_text() == "hello"


def test_link_dir_target_visible_through_link(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link_dir(target, link)
    (target / "after-link").write_text("written via target")
    assert (link / "after-link").read_text() == "written via target"


def test_swap_link_creates_link_when_missing(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    swap_link(target, link)
    assert link.exists()
    # Confirm it's a link of some kind
    if sys.platform == "win32":
        assert link.is_symlink() or os.path.isjunction(link)
    else:
        assert link.is_symlink()


def test_swap_link_replaces_existing_link(tmp_path: Path) -> None:
    target_a = tmp_path / "a"
    target_a.mkdir()
    (target_a / "marker").write_text("A")
    target_b = tmp_path / "b"
    target_b.mkdir()
    (target_b / "marker").write_text("B")
    link = tmp_path / "link"
    swap_link(target_a, link)
    assert (link / "marker").read_text() == "A"
    swap_link(target_b, link)
    assert (link / "marker").read_text() == "B"


def test_swap_link_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "marker").write_text("hello")
    link = tmp_path / "link"
    swap_link(target, link)
    swap_link(target, link)  # again
    assert (link / "marker").read_text() == "hello"


def test_swap_link_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "deep" / "nested" / "link"
    swap_link(target, link)
    assert link.exists()


def test_swap_link_cleans_up_stale_tmp(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    # Pre-create a leftover .tmp from a crashed prior swap
    leftover = tmp_path / "link.tmp"
    leftover.mkdir()
    (leftover / "garbage").write_text("stale")
    swap_link(target, link)
    assert link.exists()
    assert not leftover.exists()


def test_move_or_seed_creates_empty_when_live_missing(tmp_path: Path) -> None:
    live = tmp_path / "missing"
    target = tmp_path / "profile" / "claude"
    move_or_seed_dir(live, target)
    assert target.is_dir()
    assert list(target.iterdir()) == []


def test_move_or_seed_moves_existing_dir(tmp_path: Path) -> None:
    live = tmp_path / "live"
    live.mkdir()
    (live / "data.json").write_text("{}")
    target = tmp_path / "profile" / "claude"
    move_or_seed_dir(live, target)
    assert not live.exists()
    assert (target / "data.json").read_text() == "{}"


def test_move_or_seed_rejects_existing_link(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    live = tmp_path / "live"
    if sys.platform == "win32":
        link_dir(real, live)
    else:
        live.symlink_to(real)
    target = tmp_path / "profile" / "claude"
    with pytest.raises(AlreadyLinkedError):
        move_or_seed_dir(live, target)


def test_move_or_seed_rejects_non_directory(tmp_path: Path) -> None:
    live = tmp_path / "live"
    live.write_text("I'm a file, not a dir")
    target = tmp_path / "profile" / "claude"
    with pytest.raises(PathNotADirectoryError):
        move_or_seed_dir(live, target)


# --- Review-fix coverage (post-batch-3) ---------------------------------------


def test_link_dir_rejects_missing_target(tmp_path: Path) -> None:
    """A link with no real backing dir is dangling on POSIX and outright
    fails on Windows (junction creation requires a real dir). Reject up
    front so behavior matches across platforms."""
    target = tmp_path / "missing"
    link = tmp_path / "link"
    with pytest.raises(PathNotADirectoryError):
        link_dir(target, link)


def test_link_dir_rejects_file_target(tmp_path: Path) -> None:
    target = tmp_path / "not_a_dir"
    target.write_text("plain file")
    link = tmp_path / "link"
    with pytest.raises(PathNotADirectoryError):
        link_dir(target, link)


def test_swap_link_refuses_to_replace_real_directory(tmp_path: Path) -> None:
    """`swap_link` must not silently destroy a real (non-link) directory at
    `link_path` — the contract is replace-an-existing-link, not replace-anything."""
    target = tmp_path / "target"
    target.mkdir()
    occupied = tmp_path / "link"
    occupied.mkdir()
    (occupied / "user_data").write_text("don't lose me")
    with pytest.raises(IsADirectoryError):
        swap_link(target, occupied)
    # Side-effect check: the existing dir must still be intact.
    assert (occupied / "user_data").read_text() == "don't lose me"


def test_move_or_seed_rejects_existing_profile_target(tmp_path: Path) -> None:
    """Init reruns after a partial failure are realistic; the destination
    profile dir might already contain files. Don't let `live.replace()`
    silently overwrite (or fail with a raw OSError on Windows) — surface
    a domain error so the CLI can give actionable advice."""
    live = tmp_path / "live"
    live.mkdir()
    (live / "data.json").write_text("{}")
    target = tmp_path / "profile" / "claude"
    target.mkdir(parents=True)
    (target / "preexisting").write_text("from a previous run")
    with pytest.raises(ProfileTargetExistsError):
        move_or_seed_dir(live, target)
    # Side effects: live must remain untouched, target must remain intact.
    assert (live / "data.json").read_text() == "{}"
    assert (target / "preexisting").read_text() == "from a previous run"
