"""Directory link creation and atomic replacement.

POSIX: symbolic links. Windows: junctions (no Developer Mode required).

`swap_link` writes a temp link beside the target path then atomically renames.
On POSIX `rename(2)` is documented atomic; on Windows `MoveFileExW` with
REPLACE_EXISTING|WRITE_THROUGH is atomic for junctions in our verified testing
(see plan task "verification spike").
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from switcher.errors import AlreadyLinkedError, PathNotADirectoryError

IS_WINDOWS = sys.platform == "win32"


def link_dir(target: Path, link_path: Path) -> None:
    """Create a directory link at `link_path` pointing to `target`.

    Junction on Windows; symlink on POSIX. Caller is responsible for ensuring
    `link_path` does not already exist.
    """
    if IS_WINDOWS:
        _create_junction(target, link_path)
    else:
        link_path.symlink_to(target, target_is_directory=True)


def _create_junction(target: Path, link_path: Path) -> None:
    """Create a directory junction.

    Prefers `_winapi.CreateJunction` (private CPython API but stable); falls
    back to `cmd /c mklink /J` if that ever disappears.
    """
    try:
        import _winapi  # type: ignore[import-not-found]

        _winapi.CreateJunction(str(target), str(link_path))
        return
    except (ImportError, AttributeError):
        pass
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link_path), str(target)],
        check=True,
        capture_output=True,
    )


def _force_remove(path: Path) -> None:
    """Remove a path regardless of what kind of entry it is."""
    if path.is_symlink() or (IS_WINDOWS and os.path.isjunction(path)):
        path.unlink(missing_ok=True)
        return
    if path.is_file():
        path.unlink(missing_ok=True)
        return
    if path.is_dir():
        shutil.rmtree(path)


def swap_link(target: Path, link_path: Path) -> None:
    """Atomically replace whatever is at `link_path` with a link to `target`.

    Pattern: write to `link_path + ".tmp"`, then `os.replace` over `link_path`.
    Idempotent: pointing the link to where it already points is safe.
    """
    tmp = link_path.with_name(link_path.name + ".tmp")
    if tmp.exists() or tmp.is_symlink() or (IS_WINDOWS and os.path.isjunction(tmp)):
        _force_remove(tmp)
    link_path.parent.mkdir(parents=True, exist_ok=True)
    link_dir(target, tmp)
    tmp.replace(link_path)


def move_or_seed_dir(live: Path, profile_target: Path) -> None:
    """Move a live config dir into a profile, or seed an empty one if missing.

    Used by `init` to migrate live config dirs. The four cases:
      missing live           → create empty dir at profile_target
      symlink/junction live  → AlreadyLinkedError
      file (non-dir) live    → PathNotADirectoryError
      real dir live          → os.replace (atomic move)
    """
    profile_target.parent.mkdir(parents=True, exist_ok=True)
    is_link = live.is_symlink() or (IS_WINDOWS and os.path.isjunction(live))
    if not live.exists() and not is_link:
        profile_target.mkdir(parents=True, exist_ok=False)
        return
    if is_link:
        raise AlreadyLinkedError(f"{live} is already a link; refusing to move")
    if not live.is_dir():
        raise PathNotADirectoryError(f"{live} exists but is not a directory")
    live.replace(profile_target)
