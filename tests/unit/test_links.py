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
from switcher.links import IS_WINDOWS, link_dir, move_or_seed_dir, remove_link, swap_link


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


@pytest.mark.skipif(
    IS_WINDOWS,
    reason="POSIX-only: swap_link uses .tmp + atomic rename on POSIX, but on "
    "Windows it uses rmdir + CreateJunction directly (no .tmp file involved), "
    "so there is nothing to clean up. Stale-.tmp residue from a crashed prior "
    "swap on Windows is cosmetic clutter at most -- not a correctness concern "
    "for the new code path.",
)
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


def test_swap_link_failed_target_validation_leaves_no_debris(tmp_path: Path) -> None:
    """`swap_link` must validate `target` *before* it mutates anything —
    otherwise a missing/file target leaves behind a freshly-mkdir'd
    link_path.parent (or worse, a removed .tmp from a prior crashed run).
    Same debris discipline as `move_or_seed_dir`."""
    missing_target = tmp_path / "missing"
    link_path = tmp_path / "deep" / "nested" / "link"
    assert not link_path.parent.exists()
    with pytest.raises(PathNotADirectoryError):
        swap_link(missing_target, link_path)
    # The error path must not have created link_path.parent.
    assert not link_path.parent.exists()


def test_swap_link_failed_target_validation_preserves_stale_tmp(tmp_path: Path) -> None:
    """A leftover `.tmp` from a prior crash must not be cleaned up if the
    new swap is going to fail anyway — a future retry of the same call,
    once the target is valid, would lose the chance to surface the stale
    .tmp's existence."""
    missing_target = tmp_path / "missing"
    link_path = tmp_path / "link"
    stale_tmp = tmp_path / "link.tmp"
    stale_tmp.mkdir()
    (stale_tmp / "garbage").write_text("from a prior crashed swap")
    with pytest.raises(PathNotADirectoryError):
        swap_link(missing_target, link_path)
    assert (stale_tmp / "garbage").read_text() == "from a prior crashed swap"


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


def test_move_or_seed_rejects_existing_symlink_target(tmp_path: Path) -> None:
    """A pre-existing symlink at profile_target must trip the precondition
    too — `is_symlink()` covers POSIX symlinks (and Windows symbolic links,
    on the rare systems with Developer Mode); junctions are covered in
    the Windows-only test below."""
    real = tmp_path / "real"
    real.mkdir()
    live = tmp_path / "live"
    live.mkdir()
    target = tmp_path / "profile" / "claude"
    target.parent.mkdir(parents=True)
    if sys.platform == "win32":
        link_dir(real, target)
    else:
        target.symlink_to(real)
    with pytest.raises(ProfileTargetExistsError):
        move_or_seed_dir(live, target)


def test_move_or_seed_failed_precondition_leaves_no_debris(tmp_path: Path) -> None:
    """A failed `move_or_seed_dir` must not have already created
    profile_target's parent — init retries should encounter the same
    filesystem state as a fresh attempt, not a half-built dir tree."""
    live = tmp_path / "live"
    live.write_text("not a directory")  # triggers PathNotADirectoryError
    target = tmp_path / "profile" / "claude"
    assert not target.parent.exists()
    with pytest.raises(PathNotADirectoryError):
        move_or_seed_dir(live, target)
    # The error path must not have left target.parent behind.
    assert not target.parent.exists()


def test_move_or_seed_existing_target_leaves_no_debris(tmp_path: Path) -> None:
    """ProfileTargetExistsError must also not leak parent-mkdir side effects.
    The parent already exists in this test (we mkdir'd it to host the
    target), but we still confirm the function didn't add any debris
    underneath it beyond what we set up."""
    live = tmp_path / "live"
    live.mkdir()
    target = tmp_path / "profile" / "claude"
    target.mkdir(parents=True)
    sibling = target.parent / "sibling-must-not-appear"
    with pytest.raises(ProfileTargetExistsError):
        move_or_seed_dir(live, target)
    assert not sibling.exists()
    # And the original directories are still intact.
    assert live.is_dir()
    assert target.is_dir()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction-specific")
def test_move_or_seed_rejects_existing_junction_target(tmp_path: Path) -> None:
    """Windows junctions are not detected by `Path.is_symlink()`, and a
    *broken* junction also fails `Path.exists()` — without an explicit
    `os.path.isjunction()` arm the precondition would let the move fall
    through to a raw OSError. Pin that hole shut."""
    real = tmp_path / "real"
    real.mkdir()
    live = tmp_path / "live"
    live.mkdir()
    target = tmp_path / "profile" / "claude"
    target.parent.mkdir(parents=True)
    link_dir(real, target)
    with pytest.raises(ProfileTargetExistsError):
        move_or_seed_dir(live, target)


# --- remove_link helper (v0.1.3) ----------------------------------------------


def _make_dir_link(tmp_path: Path) -> tuple[Path, Path]:
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    # `link_dir` is the public surface that branches to junctions on Windows
    # and symlinks on POSIX — same behavior as the prior-art Windows-only
    # tests above (e.g. `test_move_or_seed_rejects_existing_junction_target`).
    link_dir(target, link)
    return target, link


def test_remove_link_drops_a_symlink_or_junction(tmp_path: Path) -> None:
    target, link = _make_dir_link(tmp_path)
    remove_link(link)
    assert not link.exists()
    assert not link.is_symlink()
    if IS_WINDOWS:
        assert not os.path.isjunction(link)
    # The target itself is unaffected.
    assert target.is_dir()


def test_remove_link_refuses_a_real_directory(tmp_path: Path) -> None:
    real_dir = tmp_path / "realdir"
    real_dir.mkdir()
    with pytest.raises(PathNotADirectoryError):
        remove_link(real_dir)
    assert real_dir.is_dir()  # untouched


def test_remove_link_refuses_a_regular_file(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("content")
    with pytest.raises(PathNotADirectoryError):
        remove_link(f)
    assert f.is_file()


def test_remove_link_refuses_a_missing_path(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    with pytest.raises(PathNotADirectoryError):
        remove_link(missing)


def test_remove_link_drops_a_broken_link(tmp_path: Path) -> None:
    """Cleanup primitive must handle dangling links — the exact case it's
    designed for during partial/unhappy-path teardown. POSIX `is_symlink()`
    works on broken symlinks; Windows `os.path.isjunction()` works on
    broken junctions because the reparse point persists even after the
    target directory is removed."""
    target, link = _make_dir_link(tmp_path)
    # Break the link by removing the target. The link reparse point/
    # symlink entry remains; only the target dir is gone.
    target.rmdir()
    remove_link(link)
    # `link.exists()` is vacuously False for a broken link (it follows
    # the dangling target), so it's not evidence of removal. `lstat`
    # inspects the link entry itself — its raise is the proof. (Both
    # `is_symlink()` and `os.path.isjunction()` ultimately call lstat
    # too, so the lstat check subsumes them.)
    with pytest.raises(FileNotFoundError):
        link.lstat()
