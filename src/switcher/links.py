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
import tempfile
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


def remove_link(link_path: Path) -> None:
    """Remove a symlink (POSIX) or junction (Windows). Raise on anything else.

    Cross-platform link removal: `Path.unlink` invokes `DeleteFile` on
    Windows, which refuses junction reparse points; `Path.rmdir`
    invokes `RemoveDirectory`, which accepts them. POSIX symlinks always
    go through `unlink`. Anything that's not a link (real dir, regular
    file, missing) raises `PathNotADirectoryError` — callers that need
    "remove anything" should use `_force_remove` instead.
    """
    if IS_WINDOWS and os.path.isjunction(link_path):
        link_path.rmdir()
        return
    # Defensive Windows fallback for broken junctions on hosts where
    # `os.path.isjunction` is unreliable — `_swap_link_windows` documents
    # the same caveat. For a broken junction, `exists()` returns False
    # (target is gone) and `is_symlink()` returns False (junctions are
    # reparse points, not symlinks), so without this branch the helper
    # would refuse to clean up exactly the case its docstring promises.
    # `exists()` returning True for real dirs/files keeps the rejection
    # path below safe from accidental removal.
    if IS_WINDOWS and not link_path.exists() and not link_path.is_symlink():
        try:
            link_path.rmdir()
            return
        except FileNotFoundError:
            pass  # truly missing — fall through to raise the strict error
        # Other OSErrors (PermissionError, sharing violations, ACL issues)
        # propagate: this helper is for cleanup, and silently masking an
        # operational failure would defeat the unhappy-path teardown
        # case the docstring promises.
    if link_path.is_symlink():
        link_path.unlink(missing_ok=True)
        return
    if not link_path.exists():
        raise PathNotADirectoryError(f"{link_path} does not exist; nothing to remove")
    raise PathNotADirectoryError(f"{link_path} is not a symlink or junction; refusing to remove")


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
    """Windows-specific link replacement: defensive remove + CreateJunction.

    Tries multiple removal strategies before re-creating the junction:
    rmdir handles junctions and empty dirs, unlink handles symlinks and
    files, rmtree is the heavy-hammer fallback. Each strategy short-
    circuits on FileNotFoundError (already gone) or success; any other
    OSError advances to the next strategy. Defensive ordering exists
    because os.path.isjunction is unreliable on some Windows hosts for
    broken junctions, so we don't trust the upstream `is_link` check
    to have correctly gated removal.
    """
    for strategy in ("rmdir", "unlink", "rmtree"):
        try:
            if strategy == "rmdir":
                link_path.rmdir()
            elif strategy == "unlink":
                link_path.unlink()
            else:
                shutil.rmtree(link_path, ignore_errors=True)
            break
        except FileNotFoundError:
            break
        except OSError:
            continue
    link_dir(target, link_path)


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


def restore_real_dir(temp_dir: Path, live_path: Path) -> None:
    """Replace a live symlink/junction with a real directory.

    The inverse of `swap_link`: validates `live_path` is a link, drops
    it via the link-aware `remove_link` helper, then renames `temp_dir`
    into place. The two operations are NOT atomic; the microseconds-long
    window where `live_path` is missing AND `temp_dir` is present is
    detectable and recoverable by the caller's pre-flight (see uninstall
    `MISSING_LIVE_TEMP_PRESENT` classification).

    Refuses to mutate if `temp_dir` is not a *real* directory. `is_dir()`
    follows links, so a symlink/junction at `temp_dir` would silently pass
    that check and rename the link itself into `live_path` — leaving a
    relinked live path instead of the real-dir restore the helper promises.
    A regular file at that path must also NOT be renamed over the live path.
    """
    if temp_dir.is_symlink() or (IS_WINDOWS and os.path.isjunction(temp_dir)):
        raise NotADirectoryError(
            f"temp dir {temp_dir} is a symlink/junction; restore_real_dir requires a real directory"
        )
    if not temp_dir.is_dir():
        raise NotADirectoryError(
            f"temp dir {temp_dir} is not a directory; copy step must precede restore"
        )
    remove_link(live_path)
    temp_dir.rename(live_path)


def atomic_write_file(target: Path, content: bytes) -> None:
    """Atomically write ``content`` to ``target`` via tmp-file + rename.

    Mirrors the discipline in ``oplog._write_records``:

    - ``mkdir(parents=True, exist_ok=True)`` on the parent first, so callers
      don't need to remember the contract. The ``.switcher/config_files/<subdir>/``
      reserved path used by ConfigFile snapshots won't exist on the first
      snapshot write per profile.
    - ``tempfile.mkstemp`` for the staging file — ``O_EXCL`` defeats the
      pre-placed-symlink class flagged on the journal write, the random
      suffix prevents collisions across overlapping callers, and
      same-directory placement keeps the rename within one filesystem so
      it's atomic on POSIX and Windows alike.
    - ``Path.replace`` for the rename (cross-platform since Python 3.3).
    - On any failure after ``mkstemp`` but before ``replace`` succeeds,
      ``unlink(missing_ok=True)`` the tmp file so retries don't accumulate
      orphans.

    **Scope:** torn-write prevention, not power-loss durability. No fsync
    on the tmp file or parent directory — matches the explicit trade-off
    documented in ``oplog._write_records``.

    **Mode preservation (POSIX):** ``tempfile.mkstemp`` creates files at
    mode 0o600 by default, so a naive tmp+rename would silently change
    the permissions of any pre-existing target file (typically tightening
    them, since 0o600 is more restrictive than typical umask defaults).
    To keep this primitive transparent to callers, when the target exists
    as a regular file we capture its mode pre-rename and apply it to the
    tmp file via ``os.chmod`` before the swap. Best-effort: if the
    capture or apply fails (rare, e.g. filesystems that reject chmod),
    the rename still proceeds with mkstemp's default mode.

    **Other metadata** (ACLs, xattrs, ownership) is not preserved — the
    rename swaps in a fresh inode and only mode bits are restored. Callers
    that need richer metadata preservation must layer it on top.

    **Symlink targets — best-effort rejection:** if ``target`` is a
    symlink at call time, raise ``IsADirectoryError`` so the user sees
    the incompatibility instead of having their redirection silently
    replaced by a regular file. This is **best-effort**, not a hard
    guarantee: there's a TOCTOU window between the up-front check and
    the eventual ``Path.replace`` during which another process could
    swap the target to a symlink. No POSIX primitive cleanly expresses
    "atomic replace iff target is a regular file", and the realistic
    threat for v1 consumers (``~/.claude.json``, switcher-private
    snapshots) is the user statically configuring a symlink — not a
    concurrent race. Closing the race tightly would require platform-
    specific tricks (renameat2's flags on Linux, no Windows analogue)
    that don't match the cross-platform contract this helper provides.
    """
    if target.is_symlink():
        raise IsADirectoryError(
            f"refusing to atomic-write through symlink at {target!r}; "
            "atomic rename would replace the link with a regular file, "
            "breaking the redirection. Resolve the symlink and write to "
            "the underlying path directly, or remove the symlink."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    # Capture pre-existing mode so the rename doesn't silently tighten
    # permissions when mkstemp's 0o600 default differs from the live file.
    prior_mode: int | None = None
    if target.is_file() and not target.is_symlink():
        try:
            prior_mode = target.stat().st_mode & 0o777
        except OSError:
            prior_mode = None

    fd, tmpname = tempfile.mkstemp(
        dir=target.parent,
        prefix=target.name + ".",
        suffix=".tmp",
    )
    tmppath = Path(tmpname)
    try:
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        if prior_mode is not None:
            try:
                os.chmod(tmppath, prior_mode)
            except OSError:
                # Best-effort; the rename still proceeds with mkstemp's
                # default mode. Filesystems that reject chmod (e.g. some
                # Windows shares) shouldn't block the write.
                pass
        tmppath.replace(target)
    except Exception:
        tmppath.unlink(missing_ok=True)
        raise
