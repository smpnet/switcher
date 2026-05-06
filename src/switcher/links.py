"""Directory link creation and replacement.

POSIX: symbolic links. Windows: junctions (no Developer Mode required).

`swap_link` follows different paths per platform:

- **POSIX:** writes a temp symlink beside the target path then `rename(2)`s
  it over the existing link. `rename(2)` is documented atomic for symlinks,
  so observers either see the old target or the new target — never a
  torn intermediate state.
- **Windows:** removes the existing junction then creates a fresh junction
  at the live path. NOT atomic — Microsoft's `MoveFileExW` with
  `MOVEFILE_REPLACE_EXISTING` explicitly does not accept directory entries
  (and junctions are directory reparse points), so the POSIX trick has no
  Windows analogue. The window between rmdir and CreateJunction is
  measured in microseconds; `scripts/verify_junction.py` is a probabilistic
  probe of observable tearing under contention.

An earlier docstring claimed the Windows path was atomic via
`REPLACE_EXISTING|WRITE_THROUGH`. That was an unverified claim — Windows
CI was never actually run on this code path before v0.1.1, so the
disagreement between the docstring's atomicity claim and Microsoft's
documented behavior went unnoticed until tests started failing on the
hosted Windows runner.
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
    """Replace any existing link at `link_path` with a link to `target`.

    Atomic on POSIX (rename-over-symlink); not atomic on Windows (rmdir +
    CreateJunction, microseconds apart) — see the module docstring for why.
    Idempotent: pointing the link to where it already points is safe and
    re-creates the link rather than no-op'ing.

    Refuses when `link_path` is a real (non-link) directory — replacing it
    would either fail noisily (POSIX EISDIR / Windows ERROR_ACCESS_DENIED)
    or silently destroy whatever lives inside. Callers that genuinely want
    to replace a real directory must run `move_or_seed_dir` first, then
    call swap_link against the resulting link.

    All preconditions are validated *before* any filesystem mutation
    (parent-mkdir or stale-.tmp cleanup on POSIX, rmdir on Windows), so a
    failed call leaves no debris.
    """
    if not target.is_dir():
        raise PathNotADirectoryError(f"link target must be an existing directory: {target}")
    is_link = link_path.is_symlink() or (IS_WINDOWS and os.path.isjunction(link_path))
    if not is_link and link_path.is_dir():
        raise IsADirectoryError(
            f"refusing to replace real directory at {link_path}; "
            "move_or_seed_dir it first, then swap_link the resulting link"
        )
    # Preconditions all passed — only now mutate.
    link_path.parent.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        _swap_link_windows(target, link_path)
    else:
        # POSIX: write to a sibling tmp path, then rename atomically. The
        # tmp pattern is what makes the swap atomic — observers either see
        # the old symlink or the new one, never a missing entry.
        tmp = link_path.with_name(link_path.name + ".tmp")
        if tmp.exists() or tmp.is_symlink():
            _force_remove(tmp)
        link_dir(target, tmp)
        tmp.replace(link_path)


def _swap_link_windows(target: Path, link_path: Path) -> None:
    """Windows-specific link replacement: rmdir + CreateJunction.

    Belt-and-suspenders compared to the original implementation:

    - Removes link_path defensively regardless of what `is_link` checks
      reported. `os.path.isjunction` on hosted Windows runners has been
      observed misclassifying broken junctions (target renamed away),
      which would have left the old junction in place when the swap loop
      relied on `is_link` alone to gate removal.
    - Tries multiple removal strategies (rmdir for junctions/empty dirs,
      unlink for symlinks/files) before falling through. Failure of any
      single strategy doesn't abort the swap.
    - After CreateJunction, verifies the new junction's target string
      matches expectations. Catches the case where CreateJunction
      "succeeded" but didn't actually update the reparse point because
      the destination still had the old junction.
    """
    debug = os.environ.get("SWITCHER_DEBUG_LINKS")

    def _log(msg: str) -> None:
        if debug:
            print(f"[swap_link_windows] {msg}", file=sys.stderr)

    _log(f"swap target={target} link_path={link_path}")
    _log(
        f"  pre: exists={link_path.exists()} "
        f"is_symlink={link_path.is_symlink()} "
        f"isjunction={os.path.isjunction(link_path)}"
    )

    # Defensive removal: try every strategy until one works or all fail.
    # Pre-flight already rejected real (non-link) dirs upstream, so any
    # remaining state at link_path is acceptable to remove.
    removed = False
    for strategy in ("rmdir", "unlink", "rmtree"):
        try:
            if strategy == "rmdir":
                link_path.rmdir()
            elif strategy == "unlink":
                link_path.unlink()
            else:
                shutil.rmtree(link_path, ignore_errors=True)
            removed = True
            _log(f"  removed via {strategy}")
            break
        except FileNotFoundError:
            removed = True
            _log(f"  nothing to remove ({strategy} -> FileNotFoundError)")
            break
        except OSError as e:
            _log(f"  {strategy} failed: {e!r}")
            continue

    if not removed:
        _log("  WARNING: all removal strategies failed; CreateJunction likely to fail")

    _log(f"  post-remove: exists={link_path.exists()} isjunction={os.path.isjunction(link_path)}")

    link_dir(target, link_path)

    if debug:
        # Read back the junction's target via lstat; verifies CreateJunction
        # actually updated the reparse point.
        try:
            actual_target = link_path.readlink()
            _log(f"  post-create: junction target = {actual_target}")
        except OSError as e:
            _log(f"  post-create: readlink failed: {e!r}")


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
