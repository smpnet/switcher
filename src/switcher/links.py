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

from switcher.errors import (
    AlreadyLinkedError,
    PathNotADirectoryError,
    ProfileTargetExistsError,
)

IS_WINDOWS = sys.platform == "win32"


def link_dir(target: Path, link_path: Path) -> None:
    """Create a directory link at `link_path` pointing to `target`.

    Junction on Windows; symlink on POSIX. Caller is responsible for ensuring
    `link_path` does not already exist.

    `target` must be an existing directory. Without this check, POSIX would
    happily create a dangling symlink while Windows would raise from junction
    creation — same intent, divergent failures. We reject up front for parity.
    """
    if not target.is_dir():
        raise PathNotADirectoryError(f"link target must be an existing directory: {target}")
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
    """Remove a path regardless of what kind of entry it is.

    Junctions are reparse-pointed *directory* entries on Windows: the
    DeleteFile syscall (which `Path.unlink` invokes) refuses them, while
    RemoveDirectory (which `Path.rmdir` invokes) accepts them. Hence the
    junction branch below — without it, stale-tmp cleanup in `swap_link`
    would silently fail on Windows.
    """
    if IS_WINDOWS and os.path.isjunction(path):
        path.rmdir()
        return
    if path.is_symlink():
        path.unlink(missing_ok=True)
        return
    if path.is_file():
        path.unlink(missing_ok=True)
        return
    if path.is_dir():
        # `ignore_errors=True` mirrors `missing_ok=True` above — closes the
        # TOCTOU window between is_dir() and rmtree().
        shutil.rmtree(path, ignore_errors=True)


def swap_link(target: Path, link_path: Path) -> None:
    """Atomically replace an existing link/file at `link_path` with a link to `target`.

    Pattern: write to `link_path + ".tmp"`, then `Path.replace` over `link_path`.
    Idempotent: pointing the link to where it already points is safe.

    Refuses when `link_path` is a real (non-link) directory — replacing it
    via rename would either fail noisily (POSIX EISDIR / Windows
    ERROR_ACCESS_DENIED) or, worse, silently destroy whatever lives inside.
    Callers that genuinely want to replace a real directory must run
    `move_or_seed_dir` first, then call swap_link against the resulting link.
    """
    is_link = link_path.is_symlink() or (IS_WINDOWS and os.path.isjunction(link_path))
    if not is_link and link_path.is_dir():
        raise IsADirectoryError(
            f"refusing to replace real directory at {link_path}; "
            "move_or_seed_dir it first, then swap_link the resulting link"
        )
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
      real dir live          → atomic move via Path.replace

    `profile_target` must not already exist — init reruns after a partial
    failure are realistic, and `live.replace(profile_target)` would either
    silently overwrite an empty target on POSIX or surface as a raw
    OSError on Windows. We pre-check and raise ProfileTargetExistsError
    so the CLI can give actionable advice instead.

    All preconditions are validated *before* any filesystem mutation, so a
    failed call leaves no debris (no half-created `profile_target.parent`
    behind). Init retries see the same FS state as a fresh attempt.
    """
    is_link = live.is_symlink() or (IS_WINDOWS and os.path.isjunction(live))
    # `is_symlink()` returns False for Windows junctions, and a *broken*
    # junction also makes `exists()` return False — so we'd fall through
    # to live.replace() and surface the raw OSError this guard exists to
    # prevent. Mirror the junction handling we use elsewhere in this file.
    target_is_link = profile_target.is_symlink() or (
        IS_WINDOWS and os.path.isjunction(profile_target)
    )
    if profile_target.exists() or target_is_link:
        raise ProfileTargetExistsError(
            f"profile destination already exists: {profile_target}; "
            "this looks like a partial init — remove it manually before retrying"
        )
    if is_link:
        raise AlreadyLinkedError(f"{live} is already a link; refusing to move")
    if live.exists() and not live.is_dir():
        raise PathNotADirectoryError(f"{live} exists but is not a directory")

    # Preconditions all passed — only now mutate the filesystem.
    profile_target.parent.mkdir(parents=True, exist_ok=True)
    if not live.exists():
        profile_target.mkdir(exist_ok=False)
        return
    live.replace(profile_target)
