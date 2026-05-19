"""ProfileService — orchestrates resolver + links + store + registry into the
user-visible operations (init/use/save/create/which/rename/delete).

`now` is module-level so tests can monkeypatch.setattr it (or use freezegun)
without reaching inside the service.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Literal, overload

from switcher.errors import (
    AbortPreflightError,
    AlreadyLinkedError,
    InitInProgressError,
    NoInProgressInitError,
    NoInProgressRescanError,
    NothingToInitializeError,
    NoToolsManagedError,
    OpLogCorruptError,
    PathNotADirectoryError,
    ProfileExistsError,
    ProfileIsActiveError,
    PruneError,
    RescanCaptureError,
    RescanInProgressError,
    StateAlreadyInitializedError,
    StateNotInitializedError,
    StorageError,
    ToolHasNoActiveProfileError,
    ToolNotInProfileError,
    ToolNotManagedError,
    UninstallPreflightError,
    UnknownProfileError,
    UnknownToolError,
)
from switcher.json_paths import (
    UnsupportedWalkTargetError,
    apply_owned_paths,
    extract_owned_paths,
)
from switcher.links import (
    atomic_write_file,
    move_or_seed_dir,
    remove_link,
    restore_real_dir,
    swap_link,
)
from switcher.models import Profile, Tool

# Op-log record classes are namespace-private to oplog.py (the underscore marks
# them as internal-to-switcher, not a user-facing surface). Service is the
# legitimate cross-module consumer that builds and dispatches them; the
# per-line suppression keeps the convention without leaking module-wide.
from switcher.oplog import (
    ConfigFileDiskState,
    MappingDiskState,
    OpLogIO,
    _ConfigFileMappingIntent,  # pyright: ignore[reportPrivateUsage]
    _InitOp,  # pyright: ignore[reportPrivateUsage]
    _MappingIntent,  # pyright: ignore[reportPrivateUsage]
    _RenameOp,  # pyright: ignore[reportPrivateUsage]
    _RescanOp,  # pyright: ignore[reportPrivateUsage]
    classify_config_file_mapping,
    classify_mapping,
)
from switcher.paths import IS_WINDOWS, PathResolver
from switcher.registry import find_tool
from switcher.store import ProfileStore


class UninstallMappingState(Enum):
    SYMLINK = "symlink"
    ALREADY_RESTORED = "already_restored"
    MISSING_LIVE_TEMP_PRESENT = "missing_live_temp_present"
    CORRUPT = "corrupt"


@dataclass(frozen=True)
class _UninstallMapping:
    tool_id: str
    profile_subdir: str
    live_path: Path
    profile_dir_subdir: Path  # <state_dir>/profiles/<active>/<config_subdir>
    state: UninstallMappingState
    corruption_reason: str = ""  # populated when state == CORRUPT


def _temp_dir_for_uninstall(live_path: Path) -> Path:
    """Sibling temp-dir name used by uninstall's copy step."""
    return live_path.with_name(live_path.name + ".switcher-uninstall-tmp")


def now() -> datetime:
    return datetime.now(UTC)


# v0.1.4 FS-truth migration constants. See spec §4.2.
#
# Parents to scan for symlinks during legacy migration, in addition to the
# current registry's config-dir parents. Hardcoded because a registry-only
# Per-tool historical profile_subdir names: subdir names a given tool's
# profile dir may legitimately contain across switcher versions. Union of
# (current registry) + (historical) gives the expected-subdir set for
# FS-truth discovery. Sourced from git history of src/switcher/builtins/*.toml.
_HISTORICAL_PROFILE_SUBDIRS: dict[str, frozenset[str]] = {
    "copilot": frozenset({"copilot-config", "copilot-auth"}),
    # claude has always used "claude" — no historical drift to record.
}

# Per-tool historical (live_path_template, profile_subdir) pairs: exact
# paths (env-expanded at lookup time) a tool's live config dir may have
# occupied in prior switcher versions, paired with the profile_subdir they
# used to be linked into. Union of (current registry tool_dirs) +
# (historical pair keys) gives the EXACT set of paths discovery considers
# — NEVER a parent-x-basename cross-product, which historically allowed
# phantom combinations like `~/github-copilot` to be falsely classified as
# managed and later mutated by uninstall (Hermes / CodeRabbit blocker
# post-PR-#5).
#
# The pair shape (path → subdir, not just a set of paths) is needed by
# `_classify_uninstall_mappings`'s resume path: when a cached live path
# is no longer a link (e.g. a partial uninstall already restored it to a
# real dir) AND the current registry no longer mentions that path (e.g.
# the user shrank the tool's config_dirs between uninstalls), we still
# need to know which profile_subdir the cached path used to be linked
# into to resume the unwind. Without this we'd misclassify the
# already-restored mapping as CORRUPT (Hermes blocker post-b621f02).
#
# Add a new tool by adding the actual on-disk path strings it has used
# AND the profile_subdir it was linked into, per platform. Use POSIX-style
# strings on POSIX (resolver.expand handles `~`) and Windows %VAR%\path
# strings on Windows (resolver.expand handles env vars). Don't include
# current registry paths here — those come from the registry directly.
_HISTORICAL_LIVE_PATH_PAIRS_POSIX: dict[str, dict[str, str]] = {
    # tool_id → { live_path_template: profile_subdir }
    "copilot": {"~/.config/github-copilot": "copilot-auth"},
    # claude has always lived at ~/.claude → "claude"; no historical drift.
}
_HISTORICAL_LIVE_PATH_PAIRS_WINDOWS: dict[str, dict[str, str]] = {
    "copilot": {"%LOCALAPPDATA%\\github-copilot": "copilot-auth"},
    # claude has always lived at %USERPROFILE%\.claude → "claude".
}


class ProfileService:
    def __init__(
        self,
        store: ProfileStore,
        resolver: PathResolver,
        registry: Sequence[Tool],
    ) -> None:
        self._store = store
        self._resolver = resolver
        self._registry = tuple(registry)

    # Helpers ---------------------------------------------------------------

    def detect_installed(self) -> list[Tool]:
        """Tools whose **first** config dir exists on disk."""
        installed: list[Tool] = []
        for tool in self._registry:
            if not tool.config_dirs:
                continue
            first_dir = self._resolver.tool_dir(tool, 0)
            if self._resolver.exists(first_dir):
                installed.append(tool)
        return installed

    def all_live_paths_present(self, tool: Tool) -> bool:
        """True iff EVERY config_dir's resolved live path exists on disk.

        Stricter than `detect_installed` (which only checks `config_dirs[0]`).
        Used by the `tools` table's pathological-state check for managed
        multi-dir tools — a tool whose first dir is intact but a later dir
        was deleted is still in a broken state and must surface as ⚠.
        """
        return all(
            self._resolver.exists(self._resolver.tool_dir(tool, i))
            for i in range(len(tool.config_dirs))
        )

    def list_profiles(self) -> list[Profile]:
        """Convenience pass-through used by some tests; CLI uses store directly."""
        return self._store.list()

    def _require_initialized(self) -> None:
        if not self._store.list():
            raise StateNotInitializedError("switcher has not been initialized; run 'switcher init'")

    def _require_managed(self) -> None:
        """Guard for commands that need at least one tool under management.

        Distinct from _require_initialized: a freshly-uninstalled-no-purge
        state has profiles on disk (passes _require_initialized) but no
        active map (fails _require_managed).
        """
        if not self._store.get_active():
            raise NoToolsManagedError(
                "no tools currently managed; run 'switcher rescan' to discover installed tools"
            )

    def _seed_credentials(self, src_profile: str, dst_profile: str, tool: Tool) -> None:
        """Copy a tool's credential files from src_profile into dst_profile.

        Missing source files are skipped silently — the user may not have
        authenticated this tool yet.
        """
        for dm in tool.config_dirs:
            (self._store.profile_dir(dst_profile) / dm.profile_subdir).mkdir(
                parents=True, exist_ok=True
            )
        for cred in tool.credentials:
            src = self._store.profile_dir(src_profile) / cred.config_dir / cred.path
            dst = self._store.profile_dir(dst_profile) / cred.config_dir / cred.path
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

    def _seed_config_files(self, src_profile: str, dst_profile: str, tool: Tool) -> None:
        """Copy a tool's ConfigFile snapshots from src_profile into dst_profile.

        Mirrors ``_seed_credentials``: a state-store data copy, not a live
        capture. Without this, the first ``switcher use`` on a newly-created
        profile would hit snapshot-missing for every ConfigFile-equipped
        tool — warn-and-skip at best, silent live-data loss at worst if a
        future caller drops the warn guard.

        Silently skips a missing source snapshot — the source profile
        pre-dates this feature and the user can repair via
        ``switcher rescan --only <tool>`` (spec §3.6 migration path).
        Without this skip, every create() from a legacy profile would
        block on ``FileNotFoundError`` from ``shutil.copy2``.

        Source-snapshot shape validation matches the apply / classifier
        side: any link shape at the source snapshot path is corruption
        (the snapshot writer always uses atomic rename, never link
        creation), and a non-regular file is the same. Refuse loudly
        rather than copy through a link or silently treat a dangling
        symlink as "missing" — the latter would propagate corruption
        into the new profile and surface only as warn-and-skip at the
        next ``use``, defeating the corruption boundary the apply path
        already enforces (Hermes pass-PR-2).
        """
        for cf in tool.config_files:
            src = self._store.config_file_snapshot_path(
                src_profile, cf.profile_subdir, cf.profile_filename
            )
            dst = self._store.config_file_snapshot_path(
                dst_profile, cf.profile_subdir, cf.profile_filename
            )
            if src.is_symlink():
                raise StorageError(
                    f"refusing to seed ConfigFile snapshot through symlink at "
                    f"{src}; resolve the symlink (or remove it so the underlying "
                    f"path is readable) and re-run."
                )
            if not src.exists():
                continue
            if not src.is_file():
                # Directory / FIFO / device at the snapshot path. Switcher
                # owns this subtree, so the case shouldn't arise from
                # normal use; external tampering or a partial init/rescan
                # could create it. Same rejection rule as
                # ``_plan_config_file_applies`` on the apply side.
                kind = "directory" if src.is_dir() else "non-regular file"
                raise StorageError(f"expected regular file at snapshot {src}, got {kind}")
            # Validate JSON-object shape at the corruption boundary. A regular
            # file that fails to parse as a JSON object is still corrupt —
            # blindly copy2-ing it would propagate the corruption into the
            # child profile and defer the failure to the first ``use`` of
            # that child. ``.switcher/config_files/...`` is switcher-owned
            # state; refuse non-object snapshots loudly here the same way
            # the apply / classifier paths do (Hermes pass-PR-3 blocker).
            try:
                src_text = src.read_text(encoding="utf-8")
            except UnicodeDecodeError as e:
                raise StorageError(f"non-UTF-8 bytes at snapshot {src}: {e}") from e
            try:
                src_data = json.loads(src_text)
            except json.JSONDecodeError as e:
                raise StorageError(
                    f"malformed snapshot JSON at {src}: {e.msg} (line {e.lineno})"
                ) from e
            if not isinstance(src_data, dict):
                raise StorageError(f"snapshot at {src} is not a JSON object")
            # Write the already-validated bytes via atomic_write_file
            # instead of re-reading ``src`` through ``shutil.copy2``.
            # ``copy2`` would reopen the source file AFTER the
            # symlink/shape/JSON checks above; a concurrent external
            # rewrite of ``src`` between the validation read and the
            # copy could let unvalidated bytes through. Reusing
            # ``src_text`` closes that TOCTOU window (CR pass-PR-3
            # minor) and routes the write through the same
            # tmp-then-rename helper every other snapshot write uses.
            atomic_write_file(dst, src_text.encode("utf-8"))

    def _any_live_dir_dangling(self, tool: Tool) -> bool:
        """True iff any of ``tool``'s config_dirs live paths is a link
        that doesn't resolve (dangling symlink / broken Windows
        junction).

        Used by ``use()``'s capture-loop divergence dispatch to
        distinguish two failure modes for "live links match neither
        source nor destination":

          - Dangling: typical post-failed-rename recovery (store.rename
            moved the target dir, swap_link failed to retarget). No
            live data behind the dangling link to capture or destroy,
            so capture-skip is safe and the apply phase below cleans
            up by re-pointing the links at the destination.
          - Resolves elsewhere: genuinely ambiguous third-profile or
            externally-mutated state. Refuse rather than overwrite.
        """
        for i in range(len(tool.config_dirs)):
            live_dir = self._resolver.tool_dir(tool, i)
            if self._resolver.is_link(live_dir):
                try:
                    live_dir.resolve(strict=True)
                except OSError:
                    return True
        return False

    def _symlink_matches_active_source(self, tool: Tool, source_profile: str) -> bool:
        """True iff every config_dirs symlink for ``tool`` resolves to
        ``source_profile``'s expected subdir.

        Used by ``use()`` to detect post-partial-commit drift before the
        capture phase runs (abby r11). If a prior ``use()`` flushed the
        per-tool symlink swap + ConfigFile write but raised before
        ``set_active_state`` persisted, the on-disk active map still
        reports the *source* profile while the filesystem has moved to
        the *destination*. Capturing through live in that state would
        read destination content into the source profile's snapshot,
        silently corrupting it.

        Conservative: any ``OSError`` (broken target, dangling link,
        cross-FS resolve issue) is treated as divergence — same intent
        as "if we can't prove alignment, don't risk overwriting source."
        """
        expected_dir = self._store.profile_dir(source_profile)
        for i, dm in enumerate(tool.config_dirs):
            live_dir = self._resolver.tool_dir(tool, i)
            try:
                target = live_dir.resolve(strict=True)
                expected = (expected_dir / dm.profile_subdir).resolve(strict=True)
            except OSError:
                return False
            if target != expected:
                return False
        return True

    def _capture_config_files(self, profile_name: str, tool: Tool) -> None:
        """Extract a tool's owned JSON subtrees from live and snapshot them.

        For each ConfigFile on ``tool``: read the live JSON, project the
        ``owned_json_paths`` subtrees via the walker, and atomic-write the
        result under the profile's reserved ``.switcher/config_files/...``
        path. No-op if the tool has no ``config_files``.

        Used by ``save()`` and (later) ``init()`` — both capture from current
        live into a fresh profile snapshot.

        Edge cases (spec §3.6 "missing or malformed live"):
          * Live missing (true non-existence) → snapshot is ``{}``. Refusing
            here would block init on a fresh machine before the user has
            launched the tool once.
          * Live malformed JSON or non-object → ``StorageError`` before any
            mutation. Capturing garbage would silently propagate to the
            apply side of the next ``use()``.
          * Live is a symlink (broken or otherwise) → ``StorageError``.
            ``Path.exists()`` returns False for a broken symlink, so without
            this guard a broken link would be indistinguishable from genuine
            absence and we'd silently overwrite the source profile's last-
            good snapshot with ``{}`` (abby r6). Even for a non-broken
            symlink, capturing through the link and then applying back
            atomic-renames the link into a regular file — the same shape
            ``_plan_config_file_applies`` rejects on the apply side.
        """
        for cf in tool.config_files:
            live_path = self._resolver.expand(cf.windows_path if IS_WINDOWS else cf.posix_path)
            if live_path.is_symlink():
                raise StorageError(
                    f"refusing to capture ConfigFile through symlink at "
                    f"{live_path}; a broken symlink would otherwise be read "
                    f"as 'missing' and silently overwrite the snapshot with "
                    f"{{}}. Resolve the symlink (or remove it so the "
                    f"underlying path is read/writable) and re-run."
                )
            if live_path.exists() and not live_path.is_file():
                # Directory / FIFO / device at the configured live path —
                # read_text() would raise IsADirectoryError or similar
                # past the service boundary (abby r12). Reject explicitly.
                kind = "directory" if live_path.is_dir() else "non-regular file"
                raise StorageError(f"expected regular file at {live_path}, got {kind}")
            if live_path.exists():
                try:
                    live_text = live_path.read_text(encoding="utf-8")
                except UnicodeDecodeError as e:
                    # Catch alongside JSONDecodeError below: bad encoding is
                    # as realistic as bad JSON for user-controlled live files
                    # and must surface as StorageError, not a raw decode
                    # traceback (abby r10).
                    raise StorageError(f"non-UTF-8 bytes at {live_path}: {e}") from e
                try:
                    live_data = json.loads(live_text)
                except json.JSONDecodeError as e:
                    raise StorageError(
                        f"malformed JSON at {live_path}: {e.msg} (line {e.lineno})"
                    ) from e
                if not isinstance(live_data, dict):
                    raise StorageError(
                        f"expected JSON object at {live_path}, got {type(live_data).__name__}"
                    )
                try:
                    snapshot = extract_owned_paths(live_data, cf.owned_json_paths)
                except UnsupportedWalkTargetError as e:
                    # e.g., live has {"projects": []} but owned path is
                    # ``.projects[].mcpServers`` — iter expects a JSON object,
                    # gets a list. This is live-data drift, not a registry
                    # config bug, so surface it consistently with the other
                    # live-validation errors above (abby r9).
                    raise StorageError(
                        f"owned path walks into a non-object value at {live_path}: {e}"
                    ) from e
            else:
                snapshot = {}

            snap_path = self._store.config_file_snapshot_path(
                profile_name, cf.profile_subdir, cf.profile_filename
            )
            atomic_write_file(
                snap_path,
                json.dumps(snapshot, indent=2, sort_keys=True).encode("utf-8"),
            )

    def _extract_and_write_config_file_snapshot(
        self,
        *,
        profile_name: str,
        live_path: Path,
        profile_subdir: str,
        profile_filename: str,
        tool_id: str,
        owned_json_paths: tuple[str, ...],
    ) -> None:
        """Compensation-path counterpart to ``_capture_config_files``.

        Re-extracts a single ConfigFile snapshot during op-log
        ``--continue`` replay. Three differences from the capture path:

        (a) ``live_path`` is the journal record's stored value, not
            the registry-derived path. The journal is the authoritative
            source for "which file was the op going to read" — if the
            registry path changed between intent-write and recovery,
            the rescan/init that the user is finishing should still
            read the same file it intended.

        (b) ``owned_json_paths`` comes from the journal too (passed in
            by the caller from ``_ConfigFileMappingIntent``). The
            registry's current owned-paths list might have changed
            between intent-write and recovery — switcher upgrade,
            registry override edit — and using the current list would
            silently extract a different shape than the original op
            intended. abby r-batch4 blocker: idempotent recovery
            requires the walker contract to come from the journal,
            not from whatever the registry says today.

        (c) Spec §3.7 runtime check: the tool's CURRENT registry must
            still carry a ConfigFile entry whose
            ``(profile_subdir, profile_filename)`` matches the journal.
            Registry drift on the tuple identity surfaces here as
            ``OpLogCorruptError`` — distinct from owned-paths drift
            (handled by journaling, above) because a missing tuple
            means the user removed the file from management entirely;
            compensation can't reason about whether to extract,
            unlink, or refuse without that anchor.

        Live-side validation mirrors ``_capture_config_files`` byte-
        for-byte. Keeping the rules identical means a clean init's
        snapshot and a ``--continue`` replay's snapshot are
        bit-identical for the same live state — exactly what idempotent
        recovery needs.
        """
        tool = find_tool(self._registry, tool_id)
        if tool is None:
            raise OpLogCorruptError(
                f"interrupted op continue: journal references unknown tool "
                f"{tool_id!r}; restore the registry TOML or hand-edit the "
                f"journal to remove the entry. Manual recovery required."
            )
        matching = [
            cf
            for cf in tool.config_files
            if cf.profile_subdir == profile_subdir and cf.profile_filename == profile_filename
        ]
        if not matching:
            raise OpLogCorruptError(
                f"interrupted op continue: journal references config_file "
                f"({profile_subdir!r}, {profile_filename!r}) for tool "
                f"{tool_id!r}, but the current registry has no such mapping. "
                f"Manual recovery required."
            )
        # v0.1.5: the Tool validator caps ``config_files`` at 1, so the
        # match must be unique. A future cap lift (or a hand-edited
        # registry that bypassed the validator) could let duplicates
        # slip past — silently extracting against the first match
        # would leave the "tuple anchor" ambiguous, and the journal's
        # storage-path uniqueness contract would not be enforceable
        # at the registry side. Defense in depth: refuse on any
        # ambiguity rather than guess (abby r-batch4 round 4).
        if len(matching) > 1:
            raise OpLogCorruptError(
                f"interrupted op continue: registry has {len(matching)} "
                f"ConfigFile entries for tool {tool_id!r} matching "
                f"({profile_subdir!r}, {profile_filename!r}); the tuple "
                f"anchor must be unique. Manual recovery required."
            )

        if live_path.is_symlink():
            raise StorageError(
                f"refusing to recapture ConfigFile through symlink at "
                f"{live_path}; resolve the symlink (or remove it so the "
                f"underlying path is readable) and re-run."
            )
        if live_path.exists() and not live_path.is_file():
            kind = "directory" if live_path.is_dir() else "non-regular file"
            raise StorageError(f"expected regular file at {live_path}, got {kind}")
        if live_path.exists():
            try:
                live_text = live_path.read_text(encoding="utf-8")
            except UnicodeDecodeError as e:
                raise StorageError(f"non-UTF-8 bytes at {live_path}: {e}") from e
            try:
                live_data = json.loads(live_text)
            except json.JSONDecodeError as e:
                raise StorageError(
                    f"malformed JSON at {live_path}: {e.msg} (line {e.lineno})"
                ) from e
            if not isinstance(live_data, dict):
                raise StorageError(
                    f"expected JSON object at {live_path}, got {type(live_data).__name__}"
                )
            try:
                snapshot = extract_owned_paths(live_data, owned_json_paths)
            except UnsupportedWalkTargetError as e:
                raise StorageError(
                    f"owned path walks into a non-object value at {live_path}: {e}"
                ) from e
        else:
            snapshot = {}

        snap_path = self._store.config_file_snapshot_path(
            profile_name, profile_subdir, profile_filename
        )
        atomic_write_file(
            snap_path,
            json.dumps(snapshot, indent=2, sort_keys=True).encode("utf-8"),
        )

    def _plan_config_file_applies(self, profile_name: str, tool: Tool) -> list[tuple[Path, bytes]]:
        """Read-only: validate and plan each ConfigFile's apply.

        Returns a list of ``(live_path, content_bytes)`` pairs the caller can
        atomic-write to commit the apply. Pure reads + an in-memory walker;
        does not touch the filesystem state on disk. Emits the snapshot-
        missing warning during this phase (it's a non-fatal observation).

        Raises ``StorageError`` on any of:
          * malformed snapshot JSON / non-object snapshot
          * malformed live JSON / non-object live
          * symlink at live_path (atomic_write_file would refuse at commit
            time, post-swap — see r5 fix)
          * ``UnsupportedWalkTargetError`` from the walker (live or snapshot has
            a shape — typically a JSON array — under an ``iter`` segment).
            Re-raised as ``StorageError`` with file context (abby r9). The
            unwalkable shape is a live-data or snapshot-data issue, not a
            registry config bug; surfacing it as ``StorageError`` keeps
            the service-boundary error contract consistent with the
            other live-validation errors above.

        Used by ``use()`` as a pre-flight ahead of any ``swap_link`` or
        write, so a parse-time failure on any tool's ConfigFile aborts the
        switch before the filesystem is mutated (abby r4 finding 1).

        The Tool validator caps ``config_files`` at 1 in v0.1.5, so the
        list this returns is 0 or 1 entries; the loop shape is kept general
        for forward-compat with the future work that would lift the cap.

        The read of live happens immediately before the atomic rename in
        the caller — a concurrent Claude write between read and rename is
        at worst a microsecond-wide race, and Claude does not touch owned
        paths during routine session activity (spec §1).
        """
        plans: list[tuple[Path, bytes]] = []
        for cf in tool.config_files:
            live_path = self._resolver.expand(cf.windows_path if IS_WINDOWS else cf.posix_path)
            snap_path = self._store.config_file_snapshot_path(
                profile_name, cf.profile_subdir, cf.profile_filename
            )

            # Symlink at live_path is fatal: atomic_write_file would refuse
            # the write at commit time, *after* swap_link had already flipped
            # config_dirs (abby r5 concrete case: a broken symlink at
            # ~/.claude.json passes the `live_path.exists()` check as False,
            # so we synthesize a plan; then the commit fails post-swap).
            # Reject here to keep "any post-swap commit failure" out of the
            # mutation phase. atomic_write_file's own check stays as
            # defense-in-depth for the TOCTOU window between pre-flight and
            # commit.
            if live_path.is_symlink():
                raise StorageError(
                    f"refusing to apply ConfigFile through symlink at "
                    f"{live_path}; atomic rename would replace the link "
                    f"with a regular file. Resolve the symlink (or remove "
                    f"it so the underlying path is writable) and re-run."
                )

            # Refuse symlinks at the snapshot path BEFORE the missing-
            # snapshot warn-and-skip. ``Path.exists()`` returns False for a
            # broken (dangling) symlink, so without this guard a corrupted
            # snapshot path with a stale symlink would degrade to "missing
            # → warn-and-skip" and leave the previous profile's owned
            # subtree in live AFTER the config_dirs swap_link had already
            # flipped — half-switched state: ``active`` claims the new
            # profile but ``~/.claude.json`` still carries the previous
            # profile's data. The classifier already treats any snapshot-
            # path link shape as AMBIGUOUS for the same reason; mirror
            # that on the apply side so reserved-state corruption fails
            # loud instead of degrading silently (Hermes + CR pass-PR-1).
            if snap_path.is_symlink():
                raise StorageError(
                    f"refusing to read config_file snapshot through symlink at "
                    f"{snap_path}; resolve the symlink (or remove it so the "
                    f"underlying path is readable) and re-run."
                )
            if not snap_path.exists():
                # Stderr matches the project's existing warning convention
                # (see `_warn_migration`). caplog won't pick this up; tests
                # use `capsys`.
                #
                # No remediation hint: in v0.1.5 PR4 the snapshot is created
                # by `save()`, and the next switch onto the source profile's
                # capture phase also creates one. The plan's init/rescan
                # integration (later PRs) closes the legacy-profile case.
                # Promising a specific command here would mis-direct users
                # while those paths are still being landed.
                print(
                    f"warning: config_file snapshot missing for {tool.id!r} at "
                    f"{snap_path}; skipping apply to preserve current live state.",
                    file=sys.stderr,
                )
                continue
            if not snap_path.is_file():
                # Directory / FIFO / device at the snapshot path. Switcher
                # owns this subtree so the case shouldn't arise from normal
                # use, but external tampering could create it (abby r12).
                kind = "directory" if snap_path.is_dir() else "non-regular file"
                raise StorageError(f"expected regular file at snapshot {snap_path}, got {kind}")

            try:
                snap_text = snap_path.read_text(encoding="utf-8")
            except UnicodeDecodeError as e:
                # Snapshots are switcher-written and therefore always UTF-8
                # in our normal flow; this branch only fires on concurrent
                # external corruption of the snapshot file. Re-raise as
                # StorageError to keep the boundary consistent (abby r10).
                raise StorageError(f"non-UTF-8 bytes at snapshot {snap_path}: {e}") from e
            try:
                snapshot = json.loads(snap_text)
            except json.JSONDecodeError as e:
                raise StorageError(
                    f"malformed snapshot JSON at {snap_path}: {e.msg} (line {e.lineno})"
                ) from e
            if not isinstance(snapshot, dict):
                raise StorageError(f"snapshot at {snap_path} is not a JSON object")

            if live_path.exists() and not live_path.is_file():
                # Mirrors the capture-side regular-file gate (abby r12):
                # without this, read_text would raise IsADirectoryError or
                # similar past the service boundary.
                kind = "directory" if live_path.is_dir() else "non-regular file"
                raise StorageError(f"expected regular file at {live_path}, got {kind}")
            if live_path.exists():
                try:
                    live_text = live_path.read_text(encoding="utf-8")
                except UnicodeDecodeError as e:
                    raise StorageError(f"non-UTF-8 bytes at {live_path}: {e}") from e
                try:
                    live_data = json.loads(live_text)
                except json.JSONDecodeError as e:
                    raise StorageError(
                        f"malformed live JSON at {live_path}: {e.msg} (line {e.lineno})"
                    ) from e
                if not isinstance(live_data, dict):
                    raise StorageError(f"live file at {live_path} is not a JSON object")
            else:
                live_data = {}

            try:
                merged = apply_owned_paths(live_data, snapshot, cf.owned_json_paths)
            except UnsupportedWalkTargetError as e:
                # e.g., snapshot or live has {"projects": []} but the owned
                # path is ``.projects[].mcpServers`` — iter expects a JSON
                # object, gets a list. The walker can't tell us which side
                # (live vs. snapshot) is the problem, so cite both in the
                # message for actionability. Re-raise as StorageError to
                # keep the service-boundary contract consistent with the
                # other live/snapshot-validation errors above (abby r9).
                raise StorageError(
                    f"owned path walks into a non-object value when "
                    f"applying snapshot {snap_path} onto {live_path}: {e}"
                ) from e
            plans.append(
                (
                    live_path,
                    json.dumps(merged, indent=2, sort_keys=True).encode("utf-8"),
                )
            )
        return plans

    def _capture_tool(self, profile: str, tool: Tool) -> list[str]:
        """Move every live dir for `tool` into `profile`, then link back.

        Returns the list of live-path strings (one per DirMapping) — the
        symlink locations themselves, not their resolved targets — for the
        v0.1.3 active_live_paths cache.
        """
        paths: list[str] = []
        for i, dm in enumerate(tool.config_dirs):
            live = self._resolver.tool_dir(tool, i)
            target = self._store.profile_dir(profile) / dm.profile_subdir
            move_or_seed_dir(live, target)
            swap_link(target, live)
            paths.append(str(live))
        return paths

    def get_active_live_paths(self) -> dict[str, list[str]]:
        """Public accessor — for CLI/status (§2.6 layer ownership).

        Returns the post-migration view: cached entries from the store, plus
        any entries derivable via strict validation against the CURRENT
        on-disk active map. Does NOT write back; flushing is the caller's
        responsibility (use `_derive_cache_for_active(active)` and pass to
        `store.set_active_state(active, cache)` in one atomic write).
        """
        return self._derive_cache_for_active(self._store.get_active())

    def _derive_cache_for_active(self, active: Mapping[str, str]) -> dict[str, list[str]]:
        """Compute the live_paths cache for a hypothetical active map.

        Used internally during state-mutating operations (`use`, `rename`,
        `init`, `uninstall`, `rescan`) where `active` is being changed in
        the same transaction. Validation is performed against the proposed
        active map (which determines the expected profile_subdir target),
        not the on-disk one.

        v0.1.4: tries filesystem-truth discovery first (handles legacy drift
        where the current registry no longer mentions paths still managed on
        disk). Falls back to the v0.1.3 strict registry-derived path only
        when discovery returns empty for a registered tool. Orphan tools
        with empty discovery emit a migration warning (matches v0.1.3
        no-derivation-possible behavior, but now visible to the user).
        """
        cached_on_disk = self._store.get_active_live_paths()
        result: dict[str, list[str]] = {}
        for tool_id, profile_name in active.items():
            existing = cached_on_disk.get(tool_id)
            if existing:
                # Trust an already-validated cache entry.
                result[tool_id] = existing
                continue

            # 1. FS-truth discovery (spec §4.2).
            discovered = self._discover_live_paths_for_active(tool_id, profile_name)
            if discovered:
                result[tool_id] = discovered
                # Consistency warning: registry promises N config_dirs but
                # discovery returned M ≠ N — suggests user-side reconciliation.
                tool = find_tool(self._registry, tool_id)
                if tool is not None and len(tool.config_dirs) != len(discovered):
                    self._warn_migration(
                        tool_id,
                        (
                            f"registry has {len(tool.config_dirs)} config_dir(s), "
                            f"but {len(discovered)} live path(s) resolve into the "
                            f"profile dir. Recorded all discovered paths — "
                            f"consider 'switcher unmanage {tool_id}' then "
                            f"'switcher rescan --only {tool_id}' to reconcile."
                        ),
                    )
                continue

            # 2. Fallback: v0.1.3 registry-strict derivation. Reached when
            # discovery found nothing AND the tool is still registered.
            tool = find_tool(self._registry, tool_id)
            if tool is None:
                self._warn_migration(tool_id, "no registry entry and no live symlinks found")
                continue
            try:
                result[tool_id] = self._derive_live_paths_strict(tool, profile_name)
            except _MigrationValidationError as e:
                self._warn_migration(tool_id, str(e))
        return result

    def _derive_live_paths_strict(self, tool: Tool, profile_name: str) -> list[str]:
        """Derive a tool's live paths and verify each one points where expected.

        Three checks per DirMapping (spec §2.3): exists, is a link, resolves
        into the expected `<profile>/<config_subdir>/`. Any failure raises
        and the caller treats the whole tool as unmigrated.
        """
        derived: list[str] = []
        for i, dm in enumerate(tool.config_dirs):
            live = self._resolver.tool_dir(tool, i)
            if not self._resolver.exists(live):
                raise _MigrationValidationError(f"live path {live} does not exist")
            if not self._resolver.is_link(live):
                raise _MigrationValidationError(f"live path {live} is not a link")
            expected_target = self._store.profile_dir(profile_name) / dm.profile_subdir
            # Use Path.resolve() uniformly for both POSIX symlinks and Windows
            # junctions — cross-platform, no os.readlink() branch needed since
            # is_link() above already covers junction-vs-symlink dispatch.
            actual_target = live.resolve()
            if actual_target != expected_target.resolve():
                raise _MigrationValidationError(
                    f"live path {live} points to {actual_target}, expected {expected_target}"
                )
            derived.append(str(live))
        return derived

    def _expected_subdirs_for(self, tool_id: str) -> frozenset[str]:
        """Profile-subdir names this tool may legitimately own on disk.

        Union of (a) the tool's current registry subdirs and (b) historical
        subdirs from prior switcher versions. Used by FS-truth migration
        discovery (spec §4.2) to bound which symlinks count for which tool
        and prevent cross-tool misattribution in multi-tool profiles.
        """
        tool = find_tool(self._registry, tool_id)
        current: frozenset[str] = (
            frozenset(dm.profile_subdir for dm in tool.config_dirs)
            if tool is not None
            else frozenset()
        )
        historical = _HISTORICAL_PROFILE_SUBDIRS.get(tool_id, frozenset())
        return current | historical

    def _candidate_live_paths_for(self, tool_id: str) -> set[Path]:
        """EXACT live paths this tool may legitimately own on disk.

        Union of (a) the tool's current registry tool_dirs and (b) historical
        full-path templates expanded through the resolver. Returns concrete
        Path objects — NOT a (parents x basenames) cross-product.

        The cross-product approach the v0.1.4 RC originally shipped allowed
        phantom paths like `~/github-copilot` (parent ~ from registry,
        basename github-copilot from history) to be classified as managed.
        Hermes / CodeRabbit flagged that as a destructive-false-positive
        path: a user-created symlink at one of those phantom paths whose
        target happened to resolve into an owned profile subdir would be
        recorded in active_live_paths and later mutated by uninstall.

        Switching to exact paths eliminates that class of false positive
        entirely while preserving legacy migration coverage — the
        historical paths still come along, just as concrete entries
        instead of as (parent, basename) factors.
        """
        paths: set[Path] = set()
        tool = find_tool(self._registry, tool_id)
        if tool is not None:
            for i in range(len(tool.config_dirs)):
                paths.add(self._resolver.tool_dir(tool, i))
        historical_pairs = (
            _HISTORICAL_LIVE_PATH_PAIRS_WINDOWS if IS_WINDOWS else _HISTORICAL_LIVE_PATH_PAIRS_POSIX
        )
        for raw in historical_pairs.get(tool_id, {}):
            # resolver.expand handles `~` (POSIX) and %ENV% (Windows) and
            # routes ~ against the resolver's configured home so injected
            # tmp_home in tests works correctly.
            paths.add(self._resolver.expand(raw))
        return paths

    def _subdir_for_historical_live_path(self, tool_id: str, live: Path) -> str | None:
        """Recover a `profile_subdir` for a cached live path that is no
        longer in the current registry (registry-drift resume path).

        Used by `_classify_uninstall_mappings` when a cached live path
        is a real dir / missing (i.e., not a link anymore) AND the current
        registry doesn't mention it — typically a partial uninstall
        followed by a registry rewrite that removed the path's config_dir.
        Without this fallback we'd misclassify the already-restored
        mapping as CORRUPT and refuse the resume (Hermes blocker
        post-b621f02).

        Returns the historical subdir if the live path matches a
        historical entry for the tool, else None.
        """
        historical_pairs = (
            _HISTORICAL_LIVE_PATH_PAIRS_WINDOWS if IS_WINDOWS else _HISTORICAL_LIVE_PATH_PAIRS_POSIX
        )
        for raw, subdir in historical_pairs.get(tool_id, {}).items():
            if self._resolver.expand(raw) == live:
                return subdir
        return None

    def _discover_live_paths_for_active(self, tool_id: str, profile_name: str) -> list[str]:
        """Walk the FS for symlinks resolving into this tool's owned subdirs.

        Trusts the filesystem over the registry. Used during legacy migration
        when active_live_paths is unpopulated (spec §4.2). The per-tool subdir
        map prevents misattribution in multi-tool profiles.

        Each candidate path comes from either (a) the current registry's
        tool_dirs or (b) the historical exact-path table — never a
        cross-product. The path must (a) exist as a link, (b) resolve into
        an owned profile subdir for THIS tool. User-created symlinks at
        unrelated paths (`~/copilot-backup`) and at phantom cross-product
        paths (`~/github-copilot` after the v0.1.4 builtin rewrite) are
        both excluded by construction.

        Returns the discovered live paths as string-form absolute paths.
        Empty list means "no symlinks resolve into a subdir this tool owns";
        callers fall back to _derive_live_paths_strict or emit a migration
        warning per the orphan-tool path.
        """
        profile_dir = self._store.profile_dir(profile_name)
        if not profile_dir.is_dir():
            return []

        expected_subdirs = self._expected_subdirs_for(tool_id)
        if not expected_subdirs:
            return []

        # Resolve only those expected subdirs that actually exist on disk;
        # non-existent ones can't be the target of any symlink anyway.
        owned_targets: set[Path] = set()
        for sub in expected_subdirs:
            candidate = profile_dir / sub
            if candidate.is_dir():
                owned_targets.add(candidate.resolve())
        if not owned_targets:
            return []

        discovered: list[Path] = []
        for live in self._candidate_live_paths_for(tool_id):
            if not self._resolver.is_link(live):
                # Real dir, regular file, or absent — not a managed link.
                continue
            try:
                resolved = live.resolve()
            except (OSError, RuntimeError):
                # Broken/dangling/cyclic link — skip silently.
                continue
            if resolved in owned_targets:
                discovered.append(live)
        # Deterministic order: cached + warnings should be platform-stable.
        return sorted(str(p) for p in discovered)

    @staticmethod
    def _warn_migration(tool_id: str, reason: str) -> None:
        """One-line stderr warning when migration can't derive a tool's cache entry."""
        print(
            f"warning: could not derive live_paths for {tool_id!r}: {reason}",
            file=sys.stderr,
        )

    def _classify_uninstall_mappings(self) -> list[_UninstallMapping]:
        """Pure-read classifier feeding uninstall's pre-flight (spec §3.2).

        Iterates active map → DirMapping list per tool, returns one
        _UninstallMapping per DirMapping with its classified state. For
        orphan tools (no registry entry) but cached live_paths, the subdir
        for each cached path is derived by reading the link target — NOT
        by zipping with sorted profile_dir contents (which would mis-pair
        a multi-tool profile).
        """
        active = self._store.get_active()
        live_paths_cache = self.get_active_live_paths()
        result: list[_UninstallMapping] = []

        for tool_id, profile_name in active.items():
            tool = find_tool(self._registry, tool_id)
            cached_paths = live_paths_cache.get(tool_id, [])
            profile_dir = self._store.profile_dir(profile_name)

            # Reconstruct the (subdir, live_path) pairs we need.
            if cached_paths:
                # Cache is the authoritative record of what's actually
                # managed on disk; derive each mapping's subdir from its
                # symlink target so uninstall is resilient to ANY shape of
                # TOML drift (reorder, add, remove, rename). CodeRabbit
                # round 4: a prior count-match shortcut paired cached paths
                # by index against tool.config_dirs, which mispaired subdirs
                # whenever the user reordered config_dirs in the registry
                # post-init and incorrectly classified healthy mappings as
                # CORRUPT.
                pairs = []
                for cached_str in cached_paths:
                    live = Path(cached_str)
                    if self._resolver.is_link(live):
                        # Derive the subdir relative to the profile dir, not via
                        # `target.name`. The current data model validates
                        # profile_subdir as a single safe-name segment, so the
                        # two are equivalent today — but `relative_to` (a) is
                        # forward-compatible if v0.2+ relaxes the constraint and
                        # (b) explicitly fails when the resolved target is
                        # outside the profile dir (a corrupt-cache symptom we
                        # otherwise silently masked into a basename).
                        try:
                            target = live.resolve()
                            subdir = str(target.relative_to(profile_dir.resolve()))
                        except (OSError, ValueError) as e:
                            result.append(
                                _UninstallMapping(
                                    tool_id=tool_id,
                                    profile_subdir="<unresolvable>",
                                    live_path=live,
                                    profile_dir_subdir=profile_dir,
                                    state=UninstallMappingState.CORRUPT,
                                    corruption_reason=(
                                        f"cached live path {live} resolves outside "
                                        f"the profile dir or cannot be resolved: {e}"
                                    ),
                                )
                            )
                            continue
                        pairs.append((subdir, live))
                    else:
                        # Resume case: live is missing or a real dir.
                        # Match the cached path against a current registry
                        # config_dir by exact path — works for in-registry
                        # resume even when other config_dirs in the same tool
                        # drifted. Falls back to historical pair table for
                        # registry-drift resume (Hermes blocker post-b621f02:
                        # a partial uninstall already restored this path,
                        # then the user shrank the registry to drop the
                        # config_dir → without the historical fallback the
                        # mapping mis-classifies as CORRUPT and the resume
                        # wedges).
                        matched_subdir: str | None = None
                        if tool is not None:
                            for i, dm in enumerate(tool.config_dirs):
                                if self._resolver.tool_dir(tool, i) == live:
                                    matched_subdir = dm.profile_subdir
                                    break
                        if matched_subdir is None:
                            matched_subdir = self._subdir_for_historical_live_path(tool_id, live)
                        if matched_subdir is None:
                            result.append(
                                _UninstallMapping(
                                    tool_id=tool_id,
                                    profile_subdir="<unknown>",
                                    live_path=live,
                                    profile_dir_subdir=profile_dir,
                                    state=UninstallMappingState.CORRUPT,
                                    corruption_reason=(
                                        f"cached live path {live} is not a link "
                                        f"and no matching registry config_dir was "
                                        f"found; cannot derive profile subdir. "
                                        f"Restore the registry TOML or clear the "
                                        f"stale cache entry."
                                    ),
                                )
                            )
                            continue
                        pairs.append((matched_subdir, live))
                if not pairs:
                    continue
            elif tool is not None:
                # No cache (legacy v0.1.0/v0.1.1 state): rebuild from registry.
                pairs = [
                    (dm.profile_subdir, self._resolver.tool_dir(tool, i))
                    for i, dm in enumerate(tool.config_dirs)
                ]
            else:
                # No registry, no cache — flag a single CORRUPT entry per tool.
                result.append(
                    _UninstallMapping(
                        tool_id=tool_id,
                        profile_subdir="<unknown>",
                        live_path=Path("<unknown>"),
                        profile_dir_subdir=profile_dir,
                        state=UninstallMappingState.CORRUPT,
                        corruption_reason="orphan tool: no registry entry and no cached live_paths",
                    )
                )
                continue

            for subdir, live in pairs:
                target = profile_dir / subdir
                state, reason = self._classify_one_mapping(live, target)
                result.append(
                    _UninstallMapping(
                        tool_id=tool_id,
                        profile_subdir=subdir,
                        live_path=live,
                        profile_dir_subdir=target,
                        state=state,
                        corruption_reason=reason,
                    )
                )
        return result

    def _classify_one_mapping(
        self, live: Path, profile_target: Path
    ) -> tuple[UninstallMappingState, str]:
        """Classify a single DirMapping. See spec §3.2."""
        # SYMLINK? Verify link target matches expected profile subdir.
        if self._resolver.is_link(live):
            if not profile_target.is_dir():
                # `is_dir` is stricter than `exists` — a regular file at
                # `profile_target` would have passed `exists()` but then
                # crashed `copytree()` mid-uninstall (CodeRabbit Major).
                # Either way (missing OR not a directory) the mapping is
                # unsafe to execute; classify as CORRUPT in pre-flight.
                return (
                    UninstallMappingState.CORRUPT,
                    f"link target profile dir {profile_target} missing or not a directory",
                )
            try:
                actual = live.resolve()
            except OSError as e:
                return (
                    UninstallMappingState.CORRUPT,
                    f"could not resolve link {live}: {e}",
                )
            if actual != profile_target.resolve():
                return (
                    UninstallMappingState.CORRUPT,
                    f"live link {live} points to {actual}, expected {profile_target}",
                )
            # Pre-flight collision check: a sibling temp dir at this point
            # is dangerous because we'd overwrite it during the copy step.
            # MISSING_LIVE_TEMP_PRESENT requires the live path to be missing,
            # so any temp present alongside a live link is unrelated — refuse.
            temp = _temp_dir_for_uninstall(live)
            if temp.exists():
                return (
                    UninstallMappingState.CORRUPT,
                    f"sibling temp dir {temp} exists alongside live link "
                    f"{live}; refusing to overwrite. Inspect/remove {temp} manually.",
                )
            return (UninstallMappingState.SYMLINK, "")

        # MISSING_LIVE_TEMP_PRESENT?
        if not live.exists():
            temp = _temp_dir_for_uninstall(live)
            # `Path.is_dir()` follows symlinks/junctions, so a link at temp would
            # otherwise pass and execute()'s temp.rename() would just rename the
            # link itself into live_path — leaving live as a link instead of the
            # real-dir restore the command promises. Mirror restore_real_dir's
            # guard: real directory only.
            temp_is_link = temp.is_symlink() or (IS_WINDOWS and os.path.isjunction(temp))
            if temp.is_dir() and not temp_is_link and self._dirs_match(temp, profile_target):
                return (UninstallMappingState.MISSING_LIVE_TEMP_PRESENT, "")
            return (
                UninstallMappingState.CORRUPT,
                f"live path missing and no recoverable temp dir at {temp}",
            )

        # ALREADY_RESTORED?
        if live.is_dir():
            if self._dirs_match(live, profile_target):
                return (UninstallMappingState.ALREADY_RESTORED, "")
            return (
                UninstallMappingState.CORRUPT,
                f"real dir at {live} does not match profile contents at {profile_target}",
            )

        # Regular file or other — CORRUPT.
        return (
            UninstallMappingState.CORRUPT,
            f"unexpected non-link non-dir entry at {live}",
        )

    @staticmethod
    def _dirs_match(a: Path, b: Path) -> bool:
        """Shallow content match: same set of relative paths, same file sizes.

        Spec §3.2 explicitly chose size-equality over byte-equality (too
        expensive at MB scale). Empty directories also have to match — a
        files-only comparison would falsely accept a profile with an empty
        subdir against a live tree that's missing it (or vice versa) as
        ALREADY_RESTORED, skipping the unwind on an incomplete restore.
        Sentinel values disambiguate the two entry kinds: a non-negative
        size for files (st_size), -1 for directories.

        Any transient OSError (PermissionError, FileNotFoundError,
        sharing-violation on Windows) during the walk is normalized to a
        non-match. The caller (`_classify_one_mapping`) treats a non-match
        as CORRUPT, which is the right surfacing for pre-flight: a tree
        we can't fully read can't be confirmed as a clean restore.
        """
        if not a.is_dir() or not b.is_dir():
            return False
        try:
            a_entries = ProfileService._walk_for_match(a)
            b_entries = ProfileService._walk_for_match(b)
        except OSError:
            return False
        return a_entries == b_entries

    @staticmethod
    def _walk_for_match(root_path: Path) -> dict[str, int]:
        entries: dict[str, int] = {}
        for root, dirs, files in os.walk(root_path):
            for d in dirs:
                p = Path(root) / d
                entries[str(p.relative_to(root_path)) + "/"] = -1
            for f in files:
                p = Path(root) / f
                entries[str(p.relative_to(root_path))] = p.stat().st_size
        return entries

    # Operations ------------------------------------------------------------

    # v0.1.5: callers using the default (compensation-flag-free) shape
    # still get a non-None InitReport. The compensation branches return
    # one of three shapes:
    #   - InitAlreadyCompletedReport — short-circuit fired (journal
    #     was committed but log-unmarked; mark_completed ran; no
    #     compensation needed).
    #   - None — compensation actually ran (continue replayed mappings
    #     and step 6-7, or abort reversed mappings and cleaned up).
    # The overload narrowing keeps existing tests like
    # `service.init().profile_name` typing cleanly without per-site
    # `assert is not None` noise; recovery callers (continue_=True /
    # abort=True) get the three-shape union and must handle it.
    @overload
    def init(
        self,
        target_ids: Sequence[str] | None = None,
        *,
        requested_but_not_installed: Sequence[str] = (),
        skipped_via_skip_flag: Sequence[str] = (),
        skipped_via_interactive: Sequence[str] = (),
        continue_: Literal[False] = False,
        abort: Literal[False] = False,
    ) -> InitReport: ...

    @overload
    def init(
        self,
        target_ids: Sequence[str] | None = None,
        *,
        requested_but_not_installed: Sequence[str] = (),
        skipped_via_skip_flag: Sequence[str] = (),
        skipped_via_interactive: Sequence[str] = (),
        continue_: bool = False,
        abort: bool = False,
    ) -> InitReport | InitAlreadyCompletedReport | None: ...

    def init(
        self,
        target_ids: Sequence[str] | None = None,
        *,
        requested_but_not_installed: Sequence[str] = (),
        skipped_via_skip_flag: Sequence[str] = (),
        skipped_via_interactive: Sequence[str] = (),
        continue_: bool = False,
        abort: bool = False,
    ) -> InitReport | InitAlreadyCompletedReport | None:
        """v0.1.4: target_ids filters which detected tools to capture.

        **Return-shape note (v0.1.5):** the default path returns an
        ``InitReport`` as before. The recovery path
        (``continue_=True`` / ``abort=True``) returns one of:

          - ``InitAlreadyCompletedReport`` — short-circuit fired
            (journal was committed but log-unmarked; mark_completed
            ran without invoking compensation). ``kind`` distinguishes
            continue vs abort so the CLI can tailor the message.
          - ``None`` — compensation actually ran (continue replayed
            mappings and steps 6-7, or abort reversed mappings and
            cleaned up active/cache).

        The ``@overload`` above keeps the default-args call site
        typed as ``-> InitReport``; direct callers that pass either
        flag get ``-> InitReport | InitAlreadyCompletedReport | None``
        and must handle the three-shape union explicitly.

        target_ids:
          None — capture every detected tool (v0.1.3 default-path behavior,
            including the empty-detect-warn case).
          Non-empty list — capture only the intersection of target_ids and
            detect_installed(). If intersection is empty AND the caller
            asked for a specific filter, raise NothingToInitializeError.

        The three keyword-only diff lists are pass-through informational
        fields populated by the CLI (which knows user intent). The service
        layer doesn't compute them; it echoes them back in the report.

        v0.1.5: ``continue_`` / ``abort`` drive op-log compensation (spec
        §2.2). Mutually exclusive; the CLI enforces the mutex AND the
        mutex with ``--only`` / ``--skip`` / ``--interactive`` before
        reaching the service. When either flag is set, the body reads
        the in-flight ``_InitOp`` from the journal and dispatches to
        ``_compensate_init_continue`` / ``_compensate_init_abort``.
        The normal path writes an ``_InitOp`` intent record BEFORE
        ``_store.create`` and marks it completed after the final
        ``set_active_state``. A crash anywhere between leaves a record
        the next CLI command's detection hook surfaces as
        ``InitInProgressError`` (mutating) / exit 3 (read-only); the
        user resolves via ``switcher init --continue`` / ``--abort``.

        Type-routing for the recovery dispatch (continue/abort with
        an in-flight op of a different kind):
          - ``_RescanOp`` in flight → ``RescanInProgressError`` (route
            user to ``switcher rescan --continue/--abort``).
          - ``_RenameOp`` in flight → ``OpLogCorruptError`` (the CLI
            detection hook auto-compensates rename on every other
            command; reaching here means the hook never got to drain
            it, e.g. a stale binary or hand-edited journal).
        """
        # Defense-in-depth mutex (CLI enforces the same invariant ahead
        # of get_deps; this guard catches direct callers — tests,
        # alternative front-ends — that bypass the CLI layer). Silently
        # falling through to the `continue_` branch would let a confused
        # caller compensate-forward when they thought they were aborting.
        # ValueError matches the "in-memory caller bug" shape, same as
        # OpLogIO.append_record's completed-record rejection.
        if continue_ and abort:
            raise ValueError("init: continue_ and abort are mutually exclusive")
        # Recovery scope comes from the in-flight journal record, NOT
        # the call site — combining recovery flags with target_ids
        # filtering or the informational diff lists would silently
        # ignore the call-site args, leading a caller to believe
        # recovery was scoped when it actually compensates the full
        # journal entry. Refuse loudly. (CLI enforces this through
        # typer's BadParameter; the service-layer guard is for direct
        # callers — tests, alternate front-ends — that bypass the CLI.)
        if (continue_ or abort) and (
            target_ids is not None
            or requested_but_not_installed
            or skipped_via_skip_flag
            or skipped_via_interactive
        ):
            raise ValueError(
                "init: continue_/abort cannot be combined with target_ids, "
                "requested_but_not_installed, skipped_via_skip_flag, or "
                "skipped_via_interactive — recovery scope is taken from "
                "the in-flight journal record, not the call site"
            )

        oplog = OpLogIO(self._store.state_dir())

        if continue_ or abort:
            in_flight = oplog.read_in_flight()
            if in_flight is None:
                raise NoInProgressInitError(
                    "no interrupted init detected; nothing to continue/abort"
                )
            # Route the user to the matching command if the in-flight op
            # is rescan rather than init — `switcher init --continue` on
            # a rescan-in-flight would otherwise be silently rejected as
            # "wrong type" without telling the user how to actually recover.
            # v0.1.5 PR5 ships rescan's matching flags, so the message
            # NAMES them directly (symmetric to the init hint wording).
            if isinstance(in_flight, _RescanOp):
                raise RescanInProgressError(
                    "an interrupted rescan is in flight; run "
                    "`switcher rescan --continue` to finish it or "
                    "`switcher rescan --abort` to reverse pre-rescan state."
                )
            # `_RenameOp` is auto-compensated by the CLI detection hook on
            # every other command; reaching here with one in flight means
            # the user invoked `switcher init --continue/--abort` against
            # a journal the hook never got to drain (e.g. a stale binary).
            # Refusing loudly is safer than running init compensation
            # against a rename's residue.
            if not isinstance(in_flight, _InitOp):
                raise OpLogCorruptError(
                    f"unexpected in-flight op type {type(in_flight).__name__}; "
                    f"manual recovery required"
                )
            if self._check_init_already_completed(in_flight):
                # Spec §2.2 "committed but log-unmarked" — the original
                # init's work is fully visible on disk; mark the record
                # completed without running any per-mapping mutation.
                # Surface the short-circuit as a distinct return shape
                # so the CLI can disambiguate between actual
                # compensation (None) and journal-cleanup-only
                # (InitAlreadyCompletedReport). Critical for the
                # abort path: without this, `init --abort` on an
                # already-committed init looks identical to a
                # successful rollback while the init is actually still
                # in place — the user would need `switcher uninstall`
                # to reverse it.
                oplog.mark_completed(in_flight)
                return InitAlreadyCompletedReport(
                    profile_name=in_flight.profile_name,
                    kind="continue" if continue_ else "abort",
                )
            if continue_:
                self._compensate_init_continue(in_flight)
            else:
                self._compensate_init_abort(in_flight)
            oplog.mark_completed(in_flight)
            return None

        if self._store.list():
            raise StateAlreadyInitializedError("switcher is already initialized")
        installed = self.detect_installed()
        if target_ids is not None:
            requested = set(target_ids)
            installed = [t for t in installed if t.id in requested]
            if not installed:
                raise NothingToInitializeError(
                    "no requested tools are installed; nothing to initialize"
                )
        elif not installed:
            # Bare init with empty detect: preserve v0.1.3 warn-and-empty.
            print(
                "warning: no installed tools detected; switcher initialized "
                "with empty profiles. Run 'switcher rescan' after installing "
                "a managed tool.",
                file=sys.stderr,
            )
        # Pre-flight: every detected live path must be a (real) directory or a
        # plain non-existent path. Two pathological shapes need to fail BEFORE
        # the first _store.create() call — otherwise the dated profile gets
        # persisted, _capture_tool fails mid-loop, and a retry is blocked by
        # StateAlreadyInitializedError:
        #   - already a link/junction → AlreadyLinkedError
        #   - exists but is a regular file → PathNotADirectoryError
        # The link case is detect_installed-aware (resolver.is_link); the file
        # case mirrors save()'s pre-flight and move_or_seed_dir's stance.
        for tool in installed:
            for i in range(len(tool.config_dirs)):
                live = self._resolver.tool_dir(tool, i)
                if self._resolver.is_link(live):
                    raise AlreadyLinkedError(f"{live} is already a link; refusing to initialize")
                if live.exists() and not live.is_dir():
                    raise PathNotADirectoryError(
                        f"{live} exists but is not a directory; cannot initialize"
                    )
        current_name = now().strftime("%Y-%m-%d") + "-current"

        # v0.1.5: build the intent record BEFORE any FS mutation. live_path
        # goes through `os.path.normpath` after `PathResolver.expand()` so
        # `..` segments fold out — the journal's `AbsolutePath` validator
        # rejects them, and a registry-side path with `..` would otherwise
        # only surface as a load-time corruption error on the recovery
        # pass. original_kind derives from the same pre-flight observations
        # the loop above just validated (link/file shapes are impossible
        # here, so the assertion at the tail of the branch is a
        # defense-in-depth barrier against future pre-flight drift).
        mappings: list[_MappingIntent] = []
        for tool in installed:
            for i, dm in enumerate(tool.config_dirs):
                live = self._resolver.tool_dir(tool, i)
                if live.is_dir() and not self._resolver.is_link(live):
                    original_kind = "real-dir"
                elif not live.exists():
                    original_kind = "missing"
                else:
                    raise AssertionError(
                        f"unexpected pre-flight state for {live}: "
                        f"is_link={self._resolver.is_link(live)}, "
                        f"exists={live.exists()}, is_dir={live.is_dir()}"
                    )
                mappings.append(
                    _MappingIntent.model_validate(
                        {
                            "tool_id": tool.id,
                            "mapping_index": i,
                            "live_path": os.path.normpath(str(live)),
                            "profile_subdir": dm.profile_subdir,
                            "original_kind": original_kind,
                        }
                    )
                )
        # ConfigFile intents — same canonicalization story as the dir
        # mappings above: ``expand()`` resolves ``~`` / env vars, then
        # ``os.path.normpath`` folds out any ``..`` segments so the
        # journal's ``AbsolutePath`` validator accepts the string. A
        # registry-side path with ``..`` would otherwise pass capture
        # and only surface as load-time corruption on the recovery pass.
        config_file_mappings: list[_ConfigFileMappingIntent] = []
        for tool in installed:
            for cf in tool.config_files:
                live_cf = self._resolver.expand(cf.windows_path if IS_WINDOWS else cf.posix_path)
                config_file_mappings.append(
                    _ConfigFileMappingIntent.model_validate(
                        {
                            "tool_id": tool.id,
                            "profile_subdir": cf.profile_subdir,
                            "profile_filename": cf.profile_filename,
                            "live_path": os.path.normpath(str(live_cf)),
                            "owned_json_paths": tuple(cf.owned_json_paths),
                        }
                    )
                )
        intent = _InitOp.model_validate(
            {
                "op": "init",
                "started_at": now(),
                "target_ids": [t.id for t in installed],
                "profile_name": current_name,
                "mappings": mappings,
                "config_file_mappings": config_file_mappings,
            }
        )
        oplog.append_record(intent)

        # Pre-mutation cancel scope (mirrors service.rename): the first
        # `_store.create(current_name, ...)` is the entry point to all FS
        # mutation. The catch is narrow on purpose — ProfileExistsError
        # is the ONLY exception we can prove leaves the disk
        # untouched, so it's the only one safe to interpret as "the
        # init never started":
        #
        #   - ProfileExistsError (CAUGHT): _store.create's first line
        #     is `if self.profile_dir(name).exists(): raise`, before
        #     any Profile model construction or mkdir. The disk is
        #     untouched; the intent must be canceled or the next
        #     command forces "compensate the init that never started".
        #   - ValueError from Profile(name=current_name, ...) (NOT
        #     caught): unreachable in practice — current_name is
        #     `now().strftime("%Y-%m-%d") + "-current"` which always
        #     passes Profile.name validation. Catching defensively
        #     would obscure the unreachable-branch property.
        #   - OSError from `d.mkdir(parents=True, exist_ok=True)`
        #     (NOT caught): mid-mutation. `parents=True` means mkdir
        #     can succeed at intermediate dirs before failing at the
        #     target, leaving partial FS state. Cancelling the intent
        #     here would drop the recovery record for a state that
        #     genuinely needs it. Mirrors rename's `OSError` posture
        #     (set_active failure inside store.rename preserves the
        #     intent for the same reason).
        #   - OSError from _atomic_write inside _store.create (NOT
        #     caught): create's own try/finally runs shutil.rmtree(d,
        #     ignore_errors=True) on this path — `ignore_errors=True`
        #     means d MAY remain on partial cleanup failure. Treat
        #     as potentially-mid-mutation and preserve intent.
        #
        # Suppression scope matches rename's: best-effort on
        # StorageError (if the same FS error broke both store.create
        # and cancel_intent's tmp+rename write, prefer the original
        # ProfileExistsError so the user sees the root cause);
        # OpLogCorruptError propagates as a race-on-the-journal-itself
        # signal that must surface loudly.
        try:
            self._store.create(current_name, {t.id: True for t in installed})
        except ProfileExistsError:
            with contextlib.suppress(StorageError):
                oplog.cancel_intent(intent)
            raise
        live_paths_cache: dict[str, list[str]] = {}
        for tool in installed:
            live_paths_cache[tool.id] = self._capture_tool(current_name, tool)
            # Mirror save()'s capture sequence so the initial profile owns
            # the same data shape every later capture will produce — without
            # this, the first `switcher use vanilla` would hit snapshot-
            # missing on every tool that declares config_files. No-op for
            # tools without config_files. Lives in the per-tool loop (not
            # after) so a StorageError from the file capture is attributable
            # to the same tool whose dir capture just succeeded.
            self._capture_config_files(current_name, tool)
        self._store.create("vanilla", {t.id: True for t in installed})
        for tool in installed:
            self._seed_credentials(current_name, "vanilla", tool)
            # vanilla represents factory-fresh state — write an empty
            # owned-subtree snapshot for each ConfigFile so the first
            # ``switcher use vanilla`` actually clears the owned
            # subtrees on live (CR r-batch4 major). Without this,
            # _plan_config_file_applies hits the snapshot-missing
            # warn-and-skip branch and leaves the user's MCPs / oauth
            # in live — silently defeating the isolation contract for
            # the vanilla profile that the rest of init enforces.
            for cf in tool.config_files:
                snap_path = self._store.config_file_snapshot_path(
                    "vanilla", cf.profile_subdir, cf.profile_filename
                )
                atomic_write_file(
                    snap_path,
                    json.dumps({}, indent=2, sort_keys=True).encode("utf-8"),
                )
        # Single atomic write of both keys — never set_active then
        # set_active_live_paths separately (crash window).
        active = {t.id: current_name for t in installed}
        self._store.set_active_state(active, live_paths_cache)

        # v0.1.5: mark the intent record completed. The next CLI command's
        # vacuum drops it.
        oplog.mark_completed(intent)

        return InitReport(
            profile_name=current_name,
            captured=[t.id for t in installed],
            requested_but_not_installed=list(requested_but_not_installed),
            skipped_via_skip_flag=list(skipped_via_skip_flag),
            skipped_via_interactive=list(skipped_via_interactive),
        )

    @staticmethod
    def _expected_nonempty_live_paths(record: _InitOp) -> dict[str, list[str]]:
        """Group ``record.mappings`` by tool_id and produce the
        sorted-by-mapping_index live_path list per tool — the journal's
        expected non-empty cache slice. Shared between two callers
        whose invariants must stay aligned:

          - ``_check_init_already_completed`` compares this against
            ``get_active_live_paths`` (which normalizes [] to absent,
            so the dict-equality check matches exactly).
          - ``_compensate_init_continue`` overlays this on top of a
            ``{tid: [] for tid in target_ids}`` seed to build the full
            cache for ``set_active_state``.

        Extracted (abby-review pass-10 readability note) so the
        recovery invariants stay readable and the two call sites
        can't drift out of sync.
        """
        by_tool: dict[str, list[tuple[int, str]]] = {}
        for mapping in record.mappings:
            by_tool.setdefault(mapping.tool_id, []).append(
                (mapping.mapping_index, mapping.live_path)
            )
        return {tid: [p for _, p in sorted(entries)] for tid, entries in by_tool.items()}

    def _check_init_already_completed(self, record: _InitOp) -> bool:
        """Return True ONLY if every observable post-init invariant
        holds on disk: both profile dirs are real directories, every
        mapping classifies COMPLETE, ``active`` covers target_ids,
        AND ``active_live_paths`` matches the journal's expected
        shape per-target.

        Spec §2.2 "committed but log-unmarked" path — a crash between
        the final ``set_active_state`` and ``mark_completed`` leaves
        the disk consistent but the journal stale. Continue/abort
        against this state should be a no-op apart from marking the
        record completed.

        Vanilla is part of the init workflow (step 6 in the normal
        path), so a missing-or-corrupt vanilla profile means the work
        ISN'T fully complete — the short-circuit must NOT fire there,
        or continue would mark the journal completed and skip the
        vanilla-recovery step. Falling through to
        ``_compensate_init_continue`` re-creates a missing vanilla and
        re-runs credential seeding; falling through to
        ``_compensate_init_abort`` surfaces a corrupt vanilla as
        OpLogCorruptError in its validation pass.

        ``active_live_paths`` is verified against the journal's
        per-target expectations too — without that check, a missing
        / stale cache entry would short-circuit and permanently skip
        the cache-rebuild step ``_compensate_init_continue`` would
        run. ``[]`` cache entries are normalized to "absent" at
        ``get_active_live_paths`` read time, so the comparison is
        against the post-normalization view (non-empty entries only,
        matching the journal's grouped live_paths_by_tool).

        The profile-dir checks reject link shapes too: ``is_dir()``
        follows symlinks, so a symlink-to-dir at either profile path
        satisfies it alone — without the explicit ``is_link`` refusal
        we'd bless an externally-mutated profile shape as healthy.

        Metadata readability for BOTH profiles (CR pass-5/6 + abby
        pass-7 blocker carry-forward): ``_store.get`` must return a
        valid Profile for ``record.profile_name`` AND ``vanilla``.
        Without this check, a rmtree-silent-failure window during
        ``_store.create("...")`` could leave a dir without metadata
        while every OTHER invariant holds (mapping classifier sees
        COMPLETE, set_active_state succeeded), so the short-circuit
        would mark_completed and clear the recovery record on a
        secretly-broken state.

        Vanilla AND dated-current both checked here. The empty-
        leftover repair path in ``_compensate_init_continue`` handles
        vanilla recovery (``_store.delete + _store.create``). The
        dated-current profile holds user data captured from live
        dirs and CANNOT be regenerated by compensation, but failing
        the short-circuit still helps: it keeps the journal record
        in flight so the user can manually delete the broken dir
        and re-run init. Without the short-circuit gate, the journal
        would clear and the user would hit ``UnknownProfileError``
        on the next ``switcher use`` with no recovery path left.
        """
        expected_tools = dict.fromkeys(record.target_ids, True)
        for name in (record.profile_name, "vanilla"):
            p = self._store.profile_dir(name)
            if self._resolver.is_link(p) or not p.is_dir():
                return False
            try:
                profile = self._store.get(name)
            except (UnknownProfileError, StorageError):
                return False
            # Tools-equality gate (Hermes pass-PR-3 blocker): the
            # compensation paths refuse on foreign profiles, but
            # bypassing them via the short-circuit would let the
            # journal clear while persisted ``.tools`` stays foreign.
            # Mirror the check both compensation paths apply: a
            # mismatch means this directory is NOT the journal-owned
            # profile, so short-circuit can't fire — falling through
            # to compensation lets the user see the same loud refusal
            # they'd get on any other recovery path.
            if profile.tools != expected_tools:
                return False
        profile_dir = self._store.profile_dir(record.profile_name)
        for intent in record.mappings:
            if classify_mapping(intent, profile_dir) is not MappingDiskState.COMPLETE:
                return False
        # ConfigFile snapshots must also be COMPLETE for the dated-current
        # AND vanilla profiles. Without both checks, a crash AFTER the
        # active-map write but BEFORE one of the snapshot writes would
        # leave the short-circuit firing — the journal would mark
        # completed and the vanilla / current snapshot would remain
        # missing (or AMBIGUOUS), defeating the isolation contract on
        # the next ``switcher use`` (CR pass-PR-2 major).
        for cf_entry in record.config_file_mappings:
            if (
                classify_config_file_mapping(cf_entry, profile_dir)
                is not ConfigFileDiskState.COMPLETE
            ):
                return False
            vanilla_dir = self._store.profile_dir("vanilla")
            if (
                classify_config_file_mapping(cf_entry, vanilla_dir)
                is not ConfigFileDiskState.COMPLETE
            ):
                return False
        # Active invariant: dict equality, not subset. A clean init's
        # `set_active_state(active, ...)` REPLACES the active map with
        # `{tid: profile_name for tid in target_ids}` — no leftover
        # keys. An extra entry on disk (e.g., a stale entry from a
        # previous init that didn't get cleaned up, or external
        # mutation since intent) is drift; short-circuiting would mark
        # the journal completed and let the stale entry survive
        # indefinitely. The check mirrors the cache invariant below;
        # both maps come out of the same atomic set_active_state
        # write, so they should have the same exactness contract.
        expected_active = dict.fromkeys(record.target_ids, record.profile_name)
        if self._store.get_active() != expected_active:
            return False
        # Cache invariant: actual_cache must equal the journal's
        # expected non-empty slice exactly. Dict equality catches both
        # missing-key and stale-value drift (a zero-mapping tool with
        # a stale non-empty cache value would otherwise short-circuit
        # under a per-key subset check). `get_active_live_paths`
        # normalizes [] to absent at read time, so the post-read view
        # of a clean init's cache equals exactly the non-empty slice.
        expected_nonempty = self._expected_nonempty_live_paths(record)
        return self._store.get_active_live_paths() == expected_nonempty

    def _compensate_init_continue(self, record: _InitOp) -> None:
        """Replay any non-COMPLETE mapping per the §2.1.1 continue
        dispatch table, then finish steps 6-7 (vanilla profile +
        active map). See spec §2.2.

        First pass: refuse on any AMBIGUOUS mapping. The classifier's
        AMBIGUOUS state covers data-in-two-places, link-to-wrong-target,
        and shape drift relative to the recorded ``original_kind`` —
        anything we'd be guessing at. Refusing keeps the corruption
        boundary fail-fast.

        Second pass dispatches per mapping: COMPLETE skips,
        MOVE_DONE_LINK_MISSING runs only swap_link, UNTOUCHED runs the
        full move_or_seed_dir + swap_link pair.

        Steps 6-7 always run unconditionally because the
        "already-completed" short-circuit (handled by
        ``_check_init_already_completed``) ran before this method was
        called. Reaching this method means at least one mapping needed
        forward progress, or the active map didn't yet cover
        target_ids; running the per-tool seed + active-map writes is
        idempotent on already-complete data anyway.

        **Active/cache REPLACE contract.** Step 7's
        ``set_active_state`` writes the FULL active map and live-paths
        cache derived from ``record.target_ids`` — same shape a clean
        init produces. Any unrelated existing entries in ``active`` or
        ``active_live_paths`` (drift from external mutation since
        intent, residue from a prior partially-cleaned run, etc.) are
        REPLACED, not merged. This intentional asymmetry with
        ``_compensate_init_abort`` — which clears only
        ``record.target_ids``-owned entries and preserves external
        state — reflects the spec's "abort surfaces externally-mutated
        state for inspection" vs. "continue commits the journal's
        recorded intent" stance.
        """
        profile_dir = self._store.profile_dir(record.profile_name)
        vanilla_dir = self._store.profile_dir("vanilla")

        # Preflight config.json BEFORE any FS mutation (Hermes pass-PR-1
        # blocker, symmetric to _compensate_init_abort's pass-7 fix).
        # Compensation's tail-end ``set_active_state`` loads config.json
        # via ``_load_config``; a malformed config.json would raise
        # StorageError there, but only AFTER replaying mappings
        # (move_or_seed_dir, swap_link), creating/regenerating vanilla,
        # and reseeding credentials. The half-applied state (live
        # converted to a managed link, journal still in flight) is
        # exactly what the abort-side preflight was designed to avoid.
        # An early read forces the parse so corruption surfaces before
        # any destructive work runs.
        self._store.get_active()

        # First-pass validation (no FS mutation). Two checks, both
        # MUST run before any per-mapping mutation — otherwise a
        # corrupt vanilla profile dir would only surface AFTER live
        # symlinks had already been swapped, leaving partial state.
        # Profile-dir invariant: every profile path is either a real
        # directory or absent. A symlink / junction / file at a
        # profile path is corruption (would let _seed_credentials or
        # _store.create write into / through external state).
        for name, p in ((record.profile_name, profile_dir), ("vanilla", vanilla_dir)):
            if self._resolver.is_link(p):
                raise OpLogCorruptError(
                    f"interrupted init continue: {name!r} profile at {p} is a "
                    f"symlink or junction, not a real profile directory; "
                    f"manual recovery required"
                )
            if p.exists() and not p.is_dir():
                raise OpLogCorruptError(
                    f"interrupted init continue: {name!r} profile at {p} exists "
                    f"but is not a directory; manual recovery required"
                )
        # Current-profile metadata refusal (abby pass-10 blocker).
        # Without this, a state where mappings classify COMPLETE but
        # record.profile_name's metadata.json is missing/corrupt
        # would run idempotent no-op compensation and mark_completed,
        # clearing the journal and leaving the user dead-ended on
        # the next `switcher use` (UnknownProfileError, no recovery
        # record left). Refuse loudly so the journal stays in flight
        # and the user can manually delete the broken dir + retry.
        # Vanilla's metadata IS handled by the empty-leftover repair
        # path below — we can regenerate vanilla (no user data); we
        # CANNOT regenerate the dated-current profile (user data
        # captured from live dirs).
        if profile_dir.is_dir():
            try:
                existing_profile = self._store.get(record.profile_name)
            except (UnknownProfileError, StorageError) as e:
                raise OpLogCorruptError(
                    f"interrupted init continue: profile {record.profile_name!r} "
                    f"at {profile_dir} is present but metadata.json is missing "
                    f"or unreadable ({e}); compensation cannot regenerate the "
                    f"profile (it captured user data from live dirs). "
                    f"Manual recovery required: delete {profile_dir} and "
                    f"re-run `switcher init --continue`."
                ) from e
            # Foreign-profile refusal (Hermes pass-PR-2 blocker): a
            # readable profile dir at the recovery pathname is only
            # journal-owned if its .tools matches dict.fromkeys(
            # record.target_ids, True) — what a clean init would have
            # written. Otherwise this directory belongs to some other
            # init / hand-edited journal / race scenario; silently
            # adopting it would write active = {tid: profile_name}
            # while store.get(profile_name).tools says otherwise.
            expected_tools = dict.fromkeys(record.target_ids, True)
            if existing_profile.tools != expected_tools:
                raise OpLogCorruptError(
                    f"interrupted init continue: profile {record.profile_name!r} "
                    f"at {profile_dir} has tools={existing_profile.tools!r} but "
                    f"the journal expects {expected_tools!r}; refusing to "
                    f"adopt a foreign profile. Manual recovery required."
                )
        for intent in record.mappings:
            target = profile_dir / intent.profile_subdir
            # Per-target shape refusal. `classify_mapping` returns
            # AMBIGUOUS for link-shape targets (target_is_real_dir
            # excludes them, target_missing excludes them, so the
            # classifier falls to its final AMBIGUOUS branch) — the
            # explicit check below gives a clearer error for that
            # case and documents the corruption boundary at the
            # mutation site rather than relying on a fall-through.
            # Without this, a future refactor that adds a new
            # classifier state could miss the AMBIGUOUS catch and
            # let `move_or_seed_dir(target, ...)` or `swap_link(target,
            # live)` operate on a path that escapes the profile store.
            if self._resolver.is_link(target):
                raise OpLogCorruptError(
                    f"interrupted init continue: target {target} for "
                    f"mapping {intent.tool_id!r}.{intent.mapping_index} is "
                    f"a symlink or junction, not a real profile "
                    f"subdirectory; manual recovery required"
                )
            if target.exists() and not target.is_dir():
                raise OpLogCorruptError(
                    f"interrupted init continue: target {target} for "
                    f"mapping {intent.tool_id!r}.{intent.mapping_index} "
                    f"exists but is not a directory; manual recovery required"
                )
            state = classify_mapping(intent, profile_dir)
            if state is MappingDiskState.AMBIGUOUS:
                raise OpLogCorruptError(
                    f"interrupted init: mapping {intent.tool_id!r}."
                    f"{intent.mapping_index} at {intent.live_path!r}: "
                    f"on-disk state is ambiguous; manual recovery "
                    f"required — see docs/RELEASE.md"
                )

        # Deferred profile-dir create (Hermes pass-PR-2 blocker):
        # the earliest crash window (intent landed, first _store.create
        # never ran) leaves profile_dir absent. Recreating BEFORE the
        # per-mapping validation loop above would let a refused
        # validation (e.g., AMBIGUOUS mapping due to externally-
        # recreated live) leave a fresh metadata.json behind, breaking
        # the "validate then mutate" guarantee. Defer until the
        # validation loop has cleared every mapping; this is the
        # last gate before the mutation pass below.
        if not profile_dir.is_dir():
            self._store.create(record.profile_name, dict.fromkeys(record.target_ids, True))

        # Second pass: per-mapping dispatch.
        for intent in record.mappings:
            state = classify_mapping(intent, profile_dir)
            target = profile_dir / intent.profile_subdir
            live = Path(intent.live_path)
            if state is MappingDiskState.COMPLETE:
                continue
            if state is MappingDiskState.MOVE_DONE_LINK_MISSING:
                swap_link(target, live)
                continue
            if state is MappingDiskState.UNTOUCHED:
                move_or_seed_dir(live, target)
                swap_link(target, live)
                continue
            # AMBIGUOUS was caught in the first-pass scan; reaching here
            # would mean the classifier's state set drifted out from
            # under us. Defensive AssertionError surfaces the bug
            # loudly rather than letting compensation continue against
            # an unknown state.
            raise AssertionError(f"unhandled mapping state {state}")

        # ConfigFile replay (spec §3.7). Two-pass discipline mirrors
        # the dir-mapping shape above: validate every entry first,
        # then mutate. AMBIGUOUS snapshot (corrupt shape — directory,
        # symlink, junction, or malformed JSON) refuses BEFORE any
        # snapshot write, so we never partially overwrite a recoverable
        # state. COMPLETE skips (idempotent); UNTOUCHED re-extracts
        # from the journal's live_path via the registry-validated
        # helper, which folds in the §3.7 runtime check.
        for cf_entry in record.config_file_mappings:
            state = classify_config_file_mapping(cf_entry, profile_dir)
            if state is ConfigFileDiskState.AMBIGUOUS:
                raise OpLogCorruptError(
                    f"interrupted init: config_file snapshot for tool "
                    f"{cf_entry.tool_id!r} at "
                    f"{profile_dir / '.switcher' / 'config_files' / cf_entry.profile_subdir / cf_entry.profile_filename}: "
                    f"on-disk state is ambiguous; manual recovery "
                    f"required — see docs/RELEASE.md"
                )
        for cf_entry in record.config_file_mappings:
            state = classify_config_file_mapping(cf_entry, profile_dir)
            if state is ConfigFileDiskState.COMPLETE:
                continue
            if state is ConfigFileDiskState.UNTOUCHED:
                self._extract_and_write_config_file_snapshot(
                    profile_name=record.profile_name,
                    live_path=Path(cf_entry.live_path),
                    profile_subdir=cf_entry.profile_subdir,
                    profile_filename=cf_entry.profile_filename,
                    tool_id=cf_entry.tool_id,
                    owned_json_paths=cf_entry.owned_json_paths,
                )
                continue
            raise AssertionError(f"unhandled config_file state {state}")

        # Step 6: vanilla profile + credential seeding. Shape of
        # vanilla_dir was already validated in the first-pass scan; a
        # symlink / file at profiles/vanilla raised OpLogCorruptError
        # before any mutation ran. Idempotent on a vanilla profile
        # that already exists (e.g. init crashed AFTER vanilla
        # creation but BEFORE set_active_state).
        #
        # Empty-leftover repair (CR pass-3 major): a mkdir-only
        # leftover from a ``_store.create("vanilla", ...)`` failure
        # whose ``shutil.rmtree(d, ignore_errors=True)`` rollback was
        # silenced (rare Windows file-lock window) leaves the dir
        # without metadata.json. Treat empty dirs as "needs
        # regeneration" — delete + recreate through the canonical
        # store API. Dirs with content but no metadata are too risky
        # to overwrite (could nuke user data); surface those as
        # OpLogCorruptError so the user manually inspects. Validating
        # vanilla as a profile (not just a directory) is the narrower
        # form of CR's pass-2 metadata concern — scoped here because
        # compensation CAN heal vanilla (we own its content) but
        # CANNOT regenerate the dated-current profile (holds user
        # data captured from live dirs).
        if not vanilla_dir.is_dir():
            self._store.create("vanilla", dict.fromkeys(record.target_ids, True))
        else:
            try:
                existing_vanilla = self._store.get("vanilla")
            except (UnknownProfileError, StorageError) as e:
                # Empty leftover: safe to delete + recreate.
                if not any(vanilla_dir.iterdir()):
                    self._store.delete("vanilla")
                    self._store.create("vanilla", dict.fromkeys(record.target_ids, True))
                else:
                    raise OpLogCorruptError(
                        f"interrupted init continue: 'vanilla' profile at "
                        f"{vanilla_dir} has no readable metadata.json but "
                        f"contains other files; refusing to overwrite. "
                        f"Manual recovery required: inspect {vanilla_dir} "
                        f"and either delete it or restore metadata.json "
                        f"before re-running `switcher init --continue`."
                    ) from e
            else:
                # Foreign-vanilla refusal (CR pass-PR-3 major): mirror
                # the dated-current branch's tools-equality check.
                # Without it, _seed_credentials would write into a
                # vanilla whose .tools doesn't list this init's
                # target_ids — clobbering foreign credentials and
                # leaving metadata that disagrees with the seeded
                # content. Abort's symmetric refusal is in
                # _compensate_init_abort's validation loop.
                expected_vanilla_tools = dict.fromkeys(record.target_ids, True)
                if existing_vanilla.tools != expected_vanilla_tools:
                    raise OpLogCorruptError(
                        f"interrupted init continue: 'vanilla' profile at "
                        f"{vanilla_dir} has tools={existing_vanilla.tools!r} "
                        f"but the journal expects {expected_vanilla_tools!r}; "
                        f"refusing to adopt a foreign profile. Manual "
                        f"recovery required."
                    )
        for tid in record.target_ids:
            tool = find_tool(self._registry, tid)
            if tool is None:
                continue
            self._seed_credentials(record.profile_name, "vanilla", tool)

        # Vanilla-snapshot write driven from the JOURNAL, not from the
        # current registry (CR r-batch4 major + abby r-batch4 round 3
        # follow-up). A registry edit between intent-write and recovery
        # that removed the ``[[config_files]]`` block would otherwise
        # let continue succeed without writing vanilla's snapshot —
        # next ``switcher use vanilla`` falls into the snapshot-missing
        # warn-and-skip branch and silently leaves user MCPs in live.
        # Reading from ``record.config_file_mappings`` keeps recovery
        # faithful to the interrupted init regardless of later
        # registry drift.
        #
        # Vanilla content is structurally ``{}`` (factory-fresh: no
        # MCPs, no oauth account, no per-project state), so no
        # owned_json_paths walker is needed — only the (subdir,
        # filename) location, which the journal carries.
        for cf_entry in record.config_file_mappings:
            snap_path = self._store.config_file_snapshot_path(
                "vanilla", cf_entry.profile_subdir, cf_entry.profile_filename
            )
            atomic_write_file(
                snap_path,
                json.dumps({}, indent=2, sort_keys=True).encode("utf-8"),
            )

        # Step 7: active map + live-paths cache, one atomic write. Both
        # maps cover the SAME set — `record.target_ids` — so the
        # serialized state matches the shape a clean init would write:
        #
        #   - `active` carries every target_id (orphans whose tool
        #     registration vanished between intent and recovery
        #     included). The on-disk symlink the compensation step
        #     already swapped exists regardless of registry state;
        #     the active map must stay consistent with that observation.
        #     Same orphan-tolerance contract rename uses.
        #
        #   - `live_paths_cache` is keyed by every target_id with `[]`
        #     defaults so a registry-only tool (zero ``config_dirs``,
        #     zero mappings) serializes as ``cache[tid] = []`` —
        #     matching the normal init path where ``_capture_tool``
        #     returns ``[]`` for those tools. Per-mapping live_paths
        #     come from the journal (not the resolver) so orphans keep
        #     the live_path snapshot the resolver can no longer
        #     reproduce, sorted by ``mapping_index`` to match
        #     ``tool.config_dirs`` ordering.
        #
        # Mismatched-coverage (active populated, cache key missing) was
        # the asymmetric-state failure mode for both the orphan-tool
        # and the zero-config_dirs cases — keep both maps anchored on
        # ``target_ids``.
        live_paths_cache: dict[str, list[str]] = {tid: [] for tid in record.target_ids}
        live_paths_cache.update(self._expected_nonempty_live_paths(record))
        active = dict.fromkeys(record.target_ids, record.profile_name)
        self._store.set_active_state(active, live_paths_cache)

    def _compensate_init_abort(self, record: _InitOp) -> None:
        """Reverse every COMPLETE / MOVE_DONE_LINK_MISSING mapping per
        the §2.1.1 abort-dispatch table, then drop the partial profile
        dirs. See spec §2.2.

        Two-pass model. The VALIDATION pass classifies every mapping
        AND checks every abort precondition (AMBIGUOUS refusal,
        ``original_kind`` in {"link","file"} defensive refusal,
        ``original_kind == "missing"`` empty-target write-through
        guard); no filesystem mutation runs in this pass. The MUTATION
        pass only starts after every mapping cleared validation.

        The two-pass discipline is the key correctness invariant: if
        mapping[0] is reversible and mapping[1] is
        ``original_kind="missing"`` with a non-empty target
        (write-through scenario), the empty-target check on mapping[1]
        must fail BEFORE mapping[0]'s reversal runs. A naive
        classify-and-mutate loop would partially mutate before
        discovering the ambiguity — exactly the failure mode the
        consultant flagged in spec §2.1.1.

        Profile-delete runs only after the validation+mutation passes
        both completed cleanly. ``vanilla`` is dropped too: init's
        pre-flight requires ``_store.list()`` to be empty, so any
        ``vanilla`` profile on disk at abort time was created by this
        crashed run.
        """
        profile_dir = self._store.profile_dir(record.profile_name)
        vanilla_dir = self._store.profile_dir("vanilla")

        # Preflight config.json BEFORE any FS mutation (CR pass-7
        # major). The active-map / cache cleanup at the tail reads
        # these maps to strip target_ids and rewrite. If config.json
        # is malformed, that read raises StorageError — but at the
        # tail position, abort would already have restored live dirs
        # and deleted profile dirs, leaving the disk half-recovered
        # and the journal still in flight. Reading early surfaces the
        # corruption BEFORE any destructive work runs, so the user
        # can fix config.json and re-run cleanly.
        active_snapshot = self._store.get_active()
        cache_snapshot = self._store.get_active_live_paths_raw()

        # VALIDATION pass. No FS mutation; states cached by index.
        # Profile-dir shape check runs FIRST so a corrupt vanilla /
        # dated-current path can't slip through to the per-mapping
        # mutation pass. The profile-store invariant is "profile_dir
        # is a real directory"; a symlink / junction / file at a
        # profile path would let `_store.delete`'s `shutil.rmtree`
        # follow the symlink and trash unrelated data.
        for name, p in ((record.profile_name, profile_dir), ("vanilla", vanilla_dir)):
            if self._resolver.is_link(p):
                raise OpLogCorruptError(
                    f"interrupted init abort: {name!r} profile at {p} is a "
                    f"symlink or junction, not a real profile directory; "
                    f"manual recovery required"
                )
            if p.exists() and not p.is_dir():
                raise OpLogCorruptError(
                    f"interrupted init abort: {name!r} profile at {p} exists "
                    f"but is not a directory; manual recovery required"
                )
            # Foreign-profile refusal (Hermes pass-PR-2 blocker): abort
            # mutation deletes both profile dirs via ``_store.delete``
            # (= ``shutil.rmtree``). If a foreign profile occupies
            # either pathname (different .tools from what this init
            # would have written), the rmtree would be a data-loss
            # path. A clean init writes both dated-current AND vanilla
            # with tools=dict.fromkeys(record.target_ids, True), so
            # any mismatch indicates a hand-edited journal / race /
            # cancel_intent failure. Refuse loudly so the user
            # manually inspects and decides whether to delete.
            if p.is_dir():
                try:
                    existing_profile = self._store.get(name)
                except (UnknownProfileError, StorageError) as e:
                    raise OpLogCorruptError(
                        f"interrupted init abort: {name!r} profile at {p} is "
                        f"present but metadata.json is missing or unreadable "
                        f"({e}); refusing to rmtree without verifying ownership. "
                        f"Manual recovery required."
                    ) from e
                expected_tools = dict.fromkeys(record.target_ids, True)
                if existing_profile.tools != expected_tools:
                    raise OpLogCorruptError(
                        f"interrupted init abort: {name!r} profile at {p} has "
                        f"tools={existing_profile.tools!r} but the journal "
                        f"expects {expected_tools!r}; refusing to delete a "
                        f"foreign profile. Manual recovery required."
                    )
        states: dict[int, MappingDiskState] = {}
        for i, intent in enumerate(record.mappings):
            target = profile_dir / intent.profile_subdir
            # Per-target shape refusal — same defense-in-depth as
            # _compensate_init_continue's first-pass. `shutil.rmtree`
            # raises on a symlink at the top level (Python docs:
            # "If path is a symbolic link, an OSError is raised"),
            # but documenting the corruption boundary explicitly
            # protects against future primitive substitutions.
            if self._resolver.is_link(target):
                raise OpLogCorruptError(
                    f"interrupted init abort: target {target} for "
                    f"mapping {intent.tool_id!r}.{intent.mapping_index} is "
                    f"a symlink or junction, not a real profile "
                    f"subdirectory; manual recovery required"
                )
            if target.exists() and not target.is_dir():
                raise OpLogCorruptError(
                    f"interrupted init abort: target {target} for "
                    f"mapping {intent.tool_id!r}.{intent.mapping_index} "
                    f"exists but is not a directory; manual recovery required"
                )
            state = classify_mapping(intent, profile_dir)
            if state is MappingDiskState.AMBIGUOUS:
                raise OpLogCorruptError(
                    f"interrupted init abort: mapping {intent.tool_id!r}."
                    f"{intent.mapping_index} is ambiguous; refusing to "
                    f"abort. Manual recovery required."
                )
            if state is MappingDiskState.UNTOUCHED:
                states[i] = state
                continue
            # COMPLETE or MOVE_DONE_LINK_MISSING — check original_kind.
            # _MappingIntent.original_kind is narrowed to
            # {"missing","real-dir"} at the type level, but a
            # hand-edited journal could carry "link" / "file" via
            # model_construct (Pydantic's escape hatch) or a future
            # schema bump — refuse defensively rather than mutate on
            # an unrecognized value.
            if intent.original_kind not in ("missing", "real-dir"):
                raise AbortPreflightError(
                    f"mapping {intent.tool_id!r}.{intent.mapping_index}: "
                    f"pre-init state was {intent.original_kind!r}; "
                    f"init pre-flight rejects this shape, so reaching it "
                    f"at abort time means external drift since intent. "
                    f"Refusing to abort defensively. Manual recovery required."
                )
            if intent.original_kind == "missing":
                # Empty-target guard: a non-empty target subdir whose
                # original live was "missing" means data was written
                # through the symlink (or directly into target, in
                # MOVE_DONE_LINK_MISSING state) between init and abort.
                # Reclassify as corruption and refuse — silently
                # deleting that data would defeat the journal's
                # correctness role.
                target = profile_dir / intent.profile_subdir
                if target.is_dir() and any(target.iterdir()):
                    raise OpLogCorruptError(
                        f"abort would delete non-empty target {target} for "
                        f"mapping {intent.tool_id!r}.{intent.mapping_index} "
                        f"(original_kind='missing'): data may have been "
                        f"written through the symlink. Manual recovery required."
                    )
            states[i] = state

        # MUTATION pass. Every mapping cleared validation; safe to mutate.
        for i, intent in enumerate(record.mappings):
            state = states[i]
            target = profile_dir / intent.profile_subdir
            live = Path(intent.live_path)
            if state is MappingDiskState.UNTOUCHED:
                continue
            if state is MappingDiskState.COMPLETE:
                remove_link(live)
                if intent.original_kind == "real-dir":
                    move_or_seed_dir(target, live)
                # Empty-target invariant already verified in the
                # validation pass; `target.exists()` guards against a
                # previous MUTATION-pass iteration removing it
                # (shouldn't happen for distinct profile_subdir
                # entries, defense in depth).
                elif intent.original_kind == "missing" and target.exists():
                    shutil.rmtree(target)
            elif state is MappingDiskState.MOVE_DONE_LINK_MISSING:
                if intent.original_kind == "real-dir":
                    move_or_seed_dir(target, live)
                elif intent.original_kind == "missing" and target.exists():
                    # Defensive exists() guard mirrors the COMPLETE
                    # branch above — protects against a target
                    # disappearing between classification and mutation
                    # (race / external removal). CR pass-10 nit.
                    shutil.rmtree(target)

        # ConfigFile abort (spec §3.7). Validation pass first — refuse
        # on any AMBIGUOUS snapshot shape before any unlink runs (the
        # validate-then-mutate discipline applies to snapshots the
        # same way it does to dir mappings). Init never writes to
        # live for ConfigFile (capture is read-only on live), so abort
        # has no live-side restore to do — only the snapshot side.
        #
        # The profile-delete pass below would rmtree the whole profile
        # dir and take the snapshots with it. The explicit unlink loop
        # is defense in depth: it produces a tighter per-step audit
        # trail, surfaces an unexpected per-snapshot failure as a
        # localized error rather than buried inside rmtree's
        # ignore_errors behaviour, and keeps init's abort shape
        # symmetric with --into rescan abort (which preserves the
        # profile dir and depends on per-snapshot unlink for cleanup).
        for cf_entry in record.config_file_mappings:
            state = classify_config_file_mapping(cf_entry, profile_dir)
            if state is ConfigFileDiskState.AMBIGUOUS:
                raise OpLogCorruptError(
                    f"interrupted init abort: config_file snapshot for "
                    f"tool {cf_entry.tool_id!r} is in an ambiguous "
                    f"state at "
                    f"{profile_dir / '.switcher' / 'config_files' / cf_entry.profile_subdir / cf_entry.profile_filename}; "
                    f"manual recovery required."
                )
        for cf_entry in record.config_file_mappings:
            state = classify_config_file_mapping(cf_entry, profile_dir)
            if state is ConfigFileDiskState.COMPLETE:
                self._store.config_file_snapshot_path(
                    record.profile_name,
                    cf_entry.profile_subdir,
                    cf_entry.profile_filename,
                ).unlink(missing_ok=True)

        # Profile-delete. Both dated-current and vanilla — init
        # pre-flight requires `_store.list()` to be empty, so any
        # `vanilla` profile on disk at abort time was created by this
        # crashed run. Shape of each profile dir was already validated
        # in the VALIDATION pass; we only delete real-dir paths here
        # (absent paths skip — a crash that never reached
        # `_store.create("vanilla", ...)` doesn't have vanilla on disk).
        if profile_dir.is_dir():
            self._store.delete(record.profile_name)
        if vanilla_dir.is_dir():
            self._store.delete("vanilla")

        # Active-map / cache cleanup. If init had progressed far
        # enough to call `set_active_state`, the active map and the
        # live_paths cache reference `record.profile_name` (and may
        # have entries for every target_id). After abort restores
        # the per-mapping live state and deletes the profile dirs,
        # those active/cache entries would dangle: active claims
        # management of a deleted profile, the cache holds a path
        # that's no longer a switcher symlink. Clear ONLY the
        # target_ids this run owns — external state outside the
        # journal's target_ids snapshot stays untouched (mirrors
        # rename's orphan-tolerance contract and respects the spec's
        # "abort surfaces externally-mutated state for inspection"
        # stance). `.pop(_, None)` is the safe idempotent form:
        # tids that never landed in active/cache (crash before
        # `set_active_state`) skip cleanly.
        #
        # Use the validation-pass snapshots (read BEFORE any FS
        # mutation — CR pass-7 major). The raw cache reader preserves
        # serialized `[]` entries that the normalizing reader would
        # drop, so unrelated zero-mapping tools survive the rewrite —
        # mirrors the spec's "abort surfaces externally-mutated state
        # for inspection" stance.
        for tid in record.target_ids:
            active_snapshot.pop(tid, None)
            cache_snapshot.pop(tid, None)
        self._store.set_active_state(active_snapshot, cache_snapshot)

    # -- rescan compensation (spec §2.4) -----------------------------------

    @staticmethod
    def _is_valid_rescan_into_metadata_state(
        record: _RescanOp, profile_name: str, existing_tools: Mapping[str, bool]
    ) -> bool:
        """Return True if ``existing_tools`` is a valid mid-op shape
        for the ``--into`` target ``profile_name``.

        A multi-tool ``--into`` rescan writes ``metadata.tools``
        progressively: each per-tool ``_capture_tool_for_rescan``
        ends with ``update_profile_tools`` containing
        ``previous_tools | {tool.id: True}`` for the captured tools
        so far. A crash mid-loop leaves
        ``previous_tools | subset(captured_here)`` — neither the
        pre-write snapshot nor the fully-finalized expected state
        (CR pass-PR major).

        Accept any state where:
          - The pre-existing tools (those NOT captured by this
            rescan) equal ``previous_tools`` exactly.
          - The captured-here tools are each either present (True)
            or absent. Partial subset OK.

        Anything else means the profile's metadata has been
        externally mutated since intent — refuse.
        """
        pre_write = dict((record.previous_tools or {}).get(profile_name, {}))
        captured_here_keys = {
            tid for tid in record.target_ids if record.target_profiles.get(tid) == profile_name
        }
        # External (non-captured-here) keys must match pre_write exactly.
        external_actual = {k: v for k, v in existing_tools.items() if k not in captured_here_keys}
        external_expected = {k: v for k, v in pre_write.items() if k not in captured_here_keys}
        if external_actual != external_expected:
            return False
        # Captured-here keys: each must be either True (added) or
        # absent (not yet added). Both accepted.
        for tid in captured_here_keys:
            if tid in existing_tools and existing_tools[tid] is not True:
                return False
        return True

    @staticmethod
    def _expected_tools_for_rescan_target(record: _RescanOp, profile_name: str) -> dict[str, bool]:
        """Compute the metadata.tools value a clean rescan would have
        produced for ``profile_name`` after the deferred-metadata
        write committed.

        - Fresh-profile mode: ``{tid: True for tid in target_ids if
          target_profiles[tid] == profile_name}`` — only the tools
          this rescan-allocated profile owns.
        - --into mode: ``previous_tools[profile_name] | {tid: True
          for tid in target_ids if target_profiles[tid] == profile_name}``
          — pre-rescan metadata merged with the captured tool set.

        Shared between ``_check_rescan_already_completed`` (whose
        equality test gates the short-circuit) and
        ``_compensate_rescan_continue`` (which uses it to drive the
        deferred ``update_profile_tools`` write). Aligns the two sites
        so a regression in one can't drift from the other.
        """
        captured_here = {
            tid: True
            for tid in record.target_ids
            if record.target_profiles.get(tid) == profile_name
        }
        if record.into_mode:
            base = dict((record.previous_tools or {}).get(profile_name, {}))
            base.update(captured_here)
            return base
        return captured_here

    @staticmethod
    def _validate_rescan_record_invariants(record: _RescanOp) -> None:
        """Enforce spec §2.4 journal-shape invariants upfront.

        Three layers of check:

        1. **General self-consistency** (abby pass-3 blocker, defense-
           in-depth). The compensation paths iterate
           ``record.target_profiles.items()`` for active-map checks
           and ``record.mappings`` per-mapping; a journal where
           ``target_ids`` and ``target_profiles.keys()`` disagree, or
           where a mapping references a tool_id outside ``target_ids``,
           would leak through ``_check_rescan_already_completed``
           silently (the orphan ``target_id`` never gets an active-map
           check). _RescanOp's Pydantic validators already enforce this
           at disk-read time, so reaching the violating branch implies
           ``model_construct`` was used to bypass validation (test
           defensive paths, future schema relaxation). Surface it
           loudly rather than relying on the disk-read invariant alone.

        2. **--into singleton target** (CR pass-2 major). ``--into``
           targets exactly ONE pre-existing profile per spec §2.4. A
           hand-edited journal with multiple distinct
           ``target_profiles`` values would otherwise drive mutations
           across multiple profiles a clean ``--into`` never would.

        3. **--into previous_tools coverage**. _RescanOp's validator
           chain already enforces this for parsed records; the in-
           method check is defense-in-depth against model_construct.

        Fresh-profile mode is intentionally NOT constrained on
        target-profile cardinality — rescan creates a separate profile
        per tool by design, so the multi-target shape is the normal
        case.
        """
        # General self-consistency (abby pass-3 blocker).
        target_id_set = set(record.target_ids)
        target_profile_keys = set(record.target_profiles.keys())
        if target_id_set != target_profile_keys:
            raise OpLogCorruptError(
                f"interrupted rescan record: target_ids "
                f"{sorted(target_id_set)!r} and target_profiles keys "
                f"{sorted(target_profile_keys)!r} disagree. Manual "
                f"recovery required."
            )
        for mapping in record.mappings:
            if mapping.tool_id not in target_id_set:
                raise OpLogCorruptError(
                    f"interrupted rescan record: mapping references "
                    f"tool_id={mapping.tool_id!r} not in target_ids="
                    f"{sorted(target_id_set)!r}. Manual recovery required."
                )

        if not record.into_mode:
            # Fresh-profile mode (abby pass-5 blocker): a clean rescan
            # allocates a UNIQUE ``<today>-rescan-N`` per tool (spec
            # §2.4 + service.py's intent-write loop increments N per
            # tool). Duplicate values in ``target_profiles`` are only
            # producible by a hand-edited journal, and would otherwise
            # let continue merge multiple tools into one profile via
            # _expected_tools_for_rescan_target's aggregation, then
            # abort would delete that merged profile in one shot —
            # destroying data from a tool that was never captured into
            # it. _RescanOp's Pydantic validators don't enforce values-
            # uniqueness, so this case IS reachable via the journal
            # lifecycle (unlike the pass-3/pass-4 defensive checks).
            values = list(record.target_profiles.values())
            if len(set(values)) != len(values):
                seen: dict[str, int] = {}
                for v in values:
                    seen[v] = seen.get(v, 0) + 1
                duplicates = sorted(name for name, count in seen.items() if count > 1)
                raise OpLogCorruptError(
                    f"interrupted rescan record (fresh-profile mode) has "
                    f"duplicate target_profiles values {duplicates!r}; spec "
                    f"§2.4 requires a unique profile per tool. Manual "
                    f"recovery required."
                )
            return
        unique_profiles = set(record.target_profiles.values())
        if len(unique_profiles) != 1:
            raise OpLogCorruptError(
                f"interrupted rescan record (--into mode) has "
                f"{len(unique_profiles)} distinct target profiles "
                f"({sorted(unique_profiles)!r}); spec §2.4 requires "
                f"exactly one. Manual recovery required."
            )
        (singleton,) = unique_profiles
        previous = record.previous_tools or {}
        # Exact-key-equality (abby pass-4 blocker). _RescanOp's Pydantic
        # validator already enforces ``set(previous_tools.keys()) ==
        # set(target_profiles.values())``; combined with the singleton
        # check above that means ``set(previous.keys()) == {singleton}``
        # is automatic for parsed records. The exact-equality check
        # here is defense-in-depth against model_construct bypass —
        # surfaces an extra-keys corruption clearly rather than letting
        # the snapshot's extra entries get silently ignored downstream.
        if set(previous.keys()) != {singleton}:
            raise OpLogCorruptError(
                f"interrupted rescan record (--into mode, target "
                f"{singleton!r}) has previous_tools keys "
                f"{sorted(previous.keys())!r} but expected exactly "
                f"{{{singleton!r}}}; abort cannot trust pre-rescan "
                f"metadata. Manual recovery required."
            )

    def _validate_rescan_target_ownership(
        self,
        record: _RescanOp,
        active: Mapping[str, str],
    ) -> None:
        """Refuse if any target_id's ``active`` entry has been rebound
        to a foreign profile since the crash (Hermes pass-PR blocker).

        Spec §2.4 has compensation own only the target_ids the
        journal records. The short-circuit's
        ``_check_rescan_already_completed`` already requires
        ``active[tid] == record.target_profiles[tid]`` to fire; the
        compensation paths don't run only because they CAN'T
        short-circuit (mapping not COMPLETE yet, etc.). Without this
        guard, an ``active[tid] = "other"`` drift between intent and
        recovery lets ``--continue`` rewrite the active entry to the
        rescan profile (silently stealing ownership) or ``--abort``
        pop it (silently deleting an unrelated active entry).

        Per target_id, allow:
          - ``active[tid]`` is absent (clean rescan case: tool was
            unmanaged at intent time).
          - ``active[tid] == record.target_profiles[tid]`` (this
            interrupted rescan already wrote the active entry, or
            recovery is being re-run after partial progress).

        Active in a third profile is drift — refuse with
        OpLogCorruptError.

        The ``active_live_paths`` cache is NOT checked: cache is a
        derived view that compensation legitimately overwrites with
        the journal's authoritative live_path values (see
        ``test_continue_does_not_short_circuit_when_cache_is_stale``
        — compensation's job there is to rebuild a stale cache from
        the journal, not refuse on it).
        """
        for tid in record.target_ids:
            expected_profile = record.target_profiles.get(tid)
            actual_profile = active.get(tid)
            if actual_profile is not None and actual_profile != expected_profile:
                raise OpLogCorruptError(
                    f"interrupted rescan: target {tid!r} is now active in "
                    f"profile {actual_profile!r} but the journal expects "
                    f"{expected_profile!r}. External state has drifted "
                    f"since intent (manual repair, hand-edited config, or "
                    f"a competing op). Refusing to overwrite. Manual "
                    f"recovery required."
                )

    def _check_rescan_already_completed(self, record: _RescanOp) -> bool:
        """Return True ONLY if every observable post-rescan invariant
        holds on disk: each unique target profile is a real directory
        with the expected metadata.tools, every mapping classifies
        COMPLETE, AND ``active`` covers every (tool_id → target_profile)
        entry the journal recorded.

        Spec §2.4 "committed but log-unmarked" path (symmetric to
        init's variant). A crash between the final ``set_active_state``
        and ``mark_completed`` leaves disk consistent but journal
        stale — continue/abort should be no-ops apart from marking
        the record completed.

        Unlike init, rescan's ``set_active_state`` ADDS to the active
        map rather than replacing it (multiple tools, one per
        ``set_active_state`` call). The active-map check is therefore
        a per-target subset, not full equality.
        """
        unique_profiles = sorted(set(record.target_profiles.values()))
        for name in unique_profiles:
            p = self._store.profile_dir(name)
            if self._resolver.is_link(p) or not p.is_dir():
                return False
            try:
                profile = self._store.get(name)
            except (UnknownProfileError, StorageError):
                return False
            expected_tools = self._expected_tools_for_rescan_target(record, name)
            if profile.tools != expected_tools:
                return False
            # Journal-id ownership proof (Hermes pass-PR-3 blocker):
            # the .tools/active/cache invariants alone are not strong
            # enough — an external process could race in the
            # reservation-vs-capture window and create a profile with
            # coincidentally matching shape at one of our reserved
            # names. Without the journal_id check here, the
            # short-circuit would fire on that foreign profile and
            # silently mark_completed the journal, dropping recovery
            # for a rescan that never actually finished. Fresh-mode
            # only — --into targets pre-exist and carry no journal_id.
            if not record.into_mode and profile.journal_id != record.rescan_id:
                return False
        profile_dir_by_tool = {
            tid: self._store.profile_dir(record.target_profiles[tid])
            for tid in record.target_ids
            if tid in record.target_profiles
        }
        for intent in record.mappings:
            profile_dir = profile_dir_by_tool.get(intent.tool_id)
            if profile_dir is None:
                # journal-corruption: mapping refers to a tool_id with no
                # target profile assignment. Refuse the short-circuit so
                # compensation surfaces a clean refusal.
                return False
            if classify_mapping(intent, profile_dir) is not MappingDiskState.COMPLETE:
                return False
        # Per-ConfigFile snapshots must classify COMPLETE on the tool's
        # target profile. Mirrors the init-side check: a crash AFTER
        # set_active_state but BEFORE a snapshot landed would otherwise
        # short-circuit, mark_completed, and leave a future ``switcher
        # use`` on the rescan profile hitting snapshot-missing
        # warn-and-skip (CR pass-PR-2 major).
        for cf_entry in record.config_file_mappings:
            cf_profile_dir = profile_dir_by_tool.get(cf_entry.tool_id)
            if cf_profile_dir is None:
                return False
            if (
                classify_config_file_mapping(cf_entry, cf_profile_dir)
                is not ConfigFileDiskState.COMPLETE
            ):
                return False
        active = self._store.get_active()
        for tid, profile_name in record.target_profiles.items():
            if active.get(tid) != profile_name:
                return False
        # Cache invariant (abby pass-1 blocker): the short-circuit would
        # otherwise mark_completed and clear the journal while
        # ``active_live_paths`` was stale or missing. Init's symmetric
        # check is stricter (full dict equality) because init REPLACES
        # the cache; rescan ADDS, so we verify per-target only — every
        # target_id must have its expected per-mapping live_paths
        # (sorted by mapping_index), or ``[]`` for zero-mapping tools.
        # ``get_active_live_paths`` normalizes ``[]`` to absent at read
        # time, so we compare against the post-normalization view.
        cache = self._store.get_active_live_paths()
        by_tool: dict[str, list[tuple[int, str]]] = {}
        for intent in record.mappings:
            by_tool.setdefault(intent.tool_id, []).append((intent.mapping_index, intent.live_path))
        for tid in record.target_ids:
            expected_paths = [p for _, p in sorted(by_tool.get(tid, []))]
            if expected_paths:
                if cache.get(tid) != expected_paths:
                    return False
            # Zero-mapping tool: clean rescan writes [] which
            # get_active_live_paths normalizes to absent. Any present
            # entry would be drift.
            elif tid in cache:
                return False
        return True

    def _compensate_rescan_continue(self, record: _RescanOp) -> None:
        """Replay any non-COMPLETE mapping per the §2.1.1 continue
        dispatch table, then finalize per-profile metadata (--into
        mode) + ``set_active_state``. See spec §2.4.

        First-pass validation: every target profile shape must check
        out, every mapping must classify into a handleable state.
        Refuses on AMBIGUOUS or foreign-tools / shape-corrupt
        profiles BEFORE any mutation runs.

        Second-pass mutation: per-mapping dispatch (COMPLETE skip,
        MOVE_DONE_LINK_MISSING swap_link only, UNTOUCHED full pair),
        then per-profile metadata reconciliation (--into mode's
        deferred ``update_profile_tools``), then a single
        ``set_active_state`` add of the journal's target_ids →
        target_profiles entries.
        """
        # Preflight config.json BEFORE any FS mutation (symmetric to
        # _compensate_init_continue's preflight). Surface a malformed
        # config as StorageError so the user can fix it before any
        # destructive work runs.
        active_snapshot = self._store.get_active()
        live_paths_cache = self._store.get_active_live_paths_raw()

        # Per-target ownership check BEFORE any mutation (Hermes
        # pass-PR blocker). External drift on any target_id's active
        # entry would otherwise let continue silently steal an
        # unrelated active mapping.
        self._validate_rescan_target_ownership(record, active_snapshot)

        unique_profiles = sorted(set(record.target_profiles.values()))

        # First-pass validation. No FS mutation.
        # Profile-dir shape check + foreign-profile refusal per target.
        for name in unique_profiles:
            p = self._store.profile_dir(name)
            if self._resolver.is_link(p):
                raise OpLogCorruptError(
                    f"interrupted rescan continue: {name!r} profile at {p} is a "
                    f"symlink or junction, not a real profile directory; "
                    f"manual recovery required"
                )
            if p.exists() and not p.is_dir():
                raise OpLogCorruptError(
                    f"interrupted rescan continue: {name!r} profile at {p} exists "
                    f"but is not a directory; manual recovery required"
                )
            if p.is_dir():
                try:
                    existing_profile = self._store.get(name)
                except (UnknownProfileError, StorageError) as e:
                    raise OpLogCorruptError(
                        f"interrupted rescan continue: profile {name!r} at {p} "
                        f"is present but metadata.json is missing or unreadable "
                        f"({e}); manual recovery required."
                    ) from e
                # Foreign-profile refusal: a clean rescan would write
                # tools = previous_tools (--into) | captured-here, or just
                # captured-here (fresh-profile). Anything else is foreign.
                # In --into mode, the journal-time previous_tools snapshot
                # might equal the CURRENT metadata if the deferred write
                # never ran — that's the same "previous_tools" we expect
                # and counts as healthy.
                expected_tools = self._expected_tools_for_rescan_target(record, name)
                if record.into_mode:
                    # --into mode: accept any partial-mid-write state
                    # between previous_tools and expected_tools. The
                    # per-tool _capture_tool_for_rescan writes
                    # update_profile_tools progressively, so a
                    # multi-tool crash mid-loop leaves
                    # ``previous_tools | subset(captured_here)`` —
                    # neither the pre-write nor the fully-finalized
                    # shape (CR pass-PR major).
                    if not self._is_valid_rescan_into_metadata_state(
                        record, name, existing_profile.tools
                    ):
                        pre_write = dict((record.previous_tools or {}).get(name, {}))
                        raise OpLogCorruptError(
                            f"interrupted rescan continue: profile {name!r} at "
                            f"{p} has tools={existing_profile.tools!r}, which is "
                            f"neither {pre_write!r} (pre-write), nor "
                            f"{expected_tools!r} (post-write), nor a valid "
                            f"partial-mid-write between them; refusing to adopt "
                            f"a foreign profile. Manual recovery required."
                        )
                else:
                    # Fresh-profile mode: metadata must match what a clean
                    # rescan would have written (captured-here only).
                    if existing_profile.tools != expected_tools:
                        raise OpLogCorruptError(
                            f"interrupted rescan continue: profile {name!r} at "
                            f"{p} has tools={existing_profile.tools!r} but the "
                            f"journal expects {expected_tools!r}; refusing to "
                            f"adopt a foreign profile. Manual recovery required."
                        )
                    # Journal-id ownership proof (Hermes pass-PR-2
                    # blocker): ``.tools`` equality alone isn't proof
                    # the rescan created this profile — an external
                    # process could race between intent-write and per-
                    # tool capture and create a profile with
                    # coincidentally-matching tools at one of the
                    # reserved names. The rescan's rescan_id was
                    # stamped into the profile's journal_id at create
                    # time; mismatch = not ours = refuse.
                    if existing_profile.journal_id != record.rescan_id:
                        raise OpLogCorruptError(
                            f"interrupted rescan continue: profile {name!r} at "
                            f"{p} has journal_id={existing_profile.journal_id!r} "
                            f"but the in-flight rescan expects "
                            f"{record.rescan_id!r}; this profile was not "
                            f"created by this rescan. Refusing to adopt a "
                            f"foreign profile. Manual recovery required."
                        )

        # Per-mapping shape + AMBIGUOUS refusal.
        for intent in record.mappings:
            profile_name = record.target_profiles.get(intent.tool_id)
            if profile_name is None:
                raise OpLogCorruptError(
                    f"interrupted rescan continue: mapping {intent.tool_id!r}."
                    f"{intent.mapping_index} references tool_id without a "
                    f"target_profiles entry; manual recovery required"
                )
            profile_dir = self._store.profile_dir(profile_name)
            target = profile_dir / intent.profile_subdir
            if self._resolver.is_link(target):
                raise OpLogCorruptError(
                    f"interrupted rescan continue: target {target} for "
                    f"mapping {intent.tool_id!r}.{intent.mapping_index} is a "
                    f"symlink or junction, not a real profile subdirectory; "
                    f"manual recovery required"
                )
            if target.exists() and not target.is_dir():
                raise OpLogCorruptError(
                    f"interrupted rescan continue: target {target} for "
                    f"mapping {intent.tool_id!r}.{intent.mapping_index} exists "
                    f"but is not a directory; manual recovery required"
                )
            state = classify_mapping(intent, profile_dir)
            if state is MappingDiskState.AMBIGUOUS:
                raise OpLogCorruptError(
                    f"interrupted rescan: mapping {intent.tool_id!r}."
                    f"{intent.mapping_index} at {intent.live_path!r}: on-disk "
                    f"state is ambiguous; manual recovery required — see "
                    f"docs/RELEASE.md"
                )

        # Deferred profile-dir create. The earliest crash window leaves
        # a target profile dir absent — recreate via store.create AFTER
        # validation cleared every mapping. In fresh-profile mode the
        # tools value is the captured-here set; in --into mode the
        # target pre-existed, so a missing dir would be its own corruption
        # — refuse rather than fabricate one we don't own.
        for name in unique_profiles:
            p = self._store.profile_dir(name)
            if not p.is_dir():
                if record.into_mode:
                    raise OpLogCorruptError(
                        f"interrupted rescan continue: --into target profile "
                        f"{name!r} at {p} is missing — refusing to recreate a "
                        f"profile the rescan didn't own. Manual recovery required."
                    )
                # Deferred fresh-profile create: stamp the rescan's
                # journal_id so a subsequent recovery still sees this
                # as journal-owned (Hermes pass-PR-2 blocker).
                self._store.create(
                    name,
                    self._expected_tools_for_rescan_target(record, name),
                    journal_id=record.rescan_id,
                )

        # Second pass: per-mapping mutation.
        for intent in record.mappings:
            profile_name = record.target_profiles[intent.tool_id]
            profile_dir = self._store.profile_dir(profile_name)
            target = profile_dir / intent.profile_subdir
            live = Path(intent.live_path)
            state = classify_mapping(intent, profile_dir)
            if state is MappingDiskState.COMPLETE:
                continue
            if state is MappingDiskState.MOVE_DONE_LINK_MISSING:
                swap_link(target, live)
                continue
            if state is MappingDiskState.UNTOUCHED:
                move_or_seed_dir(live, target)
                swap_link(target, live)
                continue
            raise AssertionError(f"unhandled mapping state {state}")

        # ConfigFile replay (spec §3.7). Same two-pass discipline as
        # the init-continue counterpart: validate every entry for
        # AMBIGUOUS shape first, then dispatch per-state. The
        # registry runtime check rides inside
        # ``_extract_and_write_config_file_snapshot`` so a registry
        # that no longer carries the journaled
        # ``(profile_subdir, profile_filename)`` pair surfaces as
        # OpLogCorruptError rather than a silent extract-everything.
        for cf_entry in record.config_file_mappings:
            cf_profile_name = record.target_profiles.get(cf_entry.tool_id)
            if cf_profile_name is None:
                raise OpLogCorruptError(
                    f"interrupted rescan continue: config_file mapping "
                    f"references tool_id {cf_entry.tool_id!r} without a "
                    f"target_profiles entry; manual recovery required"
                )
            cf_profile_dir = self._store.profile_dir(cf_profile_name)
            state = classify_config_file_mapping(cf_entry, cf_profile_dir)
            if state is ConfigFileDiskState.AMBIGUOUS:
                raise OpLogCorruptError(
                    f"interrupted rescan: config_file snapshot for tool "
                    f"{cf_entry.tool_id!r} at "
                    f"{cf_profile_dir / '.switcher' / 'config_files' / cf_entry.profile_subdir / cf_entry.profile_filename}: "
                    f"on-disk state is ambiguous; manual recovery "
                    f"required — see docs/RELEASE.md"
                )
        for cf_entry in record.config_file_mappings:
            cf_profile_name = record.target_profiles[cf_entry.tool_id]
            cf_profile_dir = self._store.profile_dir(cf_profile_name)
            state = classify_config_file_mapping(cf_entry, cf_profile_dir)
            if state is ConfigFileDiskState.COMPLETE:
                continue
            if state is ConfigFileDiskState.UNTOUCHED:
                self._extract_and_write_config_file_snapshot(
                    profile_name=cf_profile_name,
                    live_path=Path(cf_entry.live_path),
                    profile_subdir=cf_entry.profile_subdir,
                    profile_filename=cf_entry.profile_filename,
                    tool_id=cf_entry.tool_id,
                    owned_json_paths=cf_entry.owned_json_paths,
                )
                continue
            raise AssertionError(f"unhandled config_file state {state}")

        # Deferred metadata write for --into mode. Idempotent on the
        # post-write metadata shape; required when crash happened
        # before the original op's deferred update_profile_tools ran.
        if record.into_mode:
            for name in unique_profiles:
                expected_tools = self._expected_tools_for_rescan_target(record, name)
                current_tools = dict(self._store.get(name).tools)
                if current_tools != expected_tools:
                    self._store.update_profile_tools(name, expected_tools)

        # Active-map add. Rescan accumulates active entries rather than
        # replacing the whole map; the journal's target_ids → target_profiles
        # additions go on top of whatever was there before.
        for tid, profile_name in record.target_profiles.items():
            active_snapshot[tid] = profile_name
        # Per-tool live_paths from the journal (mapping_index ordering),
        # grouped by tool_id. Zero-mapping tools serialize as [] to match
        # what a clean rescan would have written.
        by_tool: dict[str, list[tuple[int, str]]] = {}
        for intent in record.mappings:
            by_tool.setdefault(intent.tool_id, []).append((intent.mapping_index, intent.live_path))
        for tid in record.target_ids:
            entries = sorted(by_tool.get(tid, []))
            live_paths_cache[tid] = [p for _, p in entries]
        self._store.set_active_state(active_snapshot, live_paths_cache)

    def _compensate_rescan_abort(self, record: _RescanOp) -> None:
        """Reverse every COMPLETE / MOVE_DONE_LINK_MISSING mapping per
        the §2.1.1 abort-dispatch table, then drop fresh-profile target
        profiles or restore previous_tools metadata for --into. See
        spec §2.4.

        Two-pass discipline (mirrors init's). Validation pass checks
        every mapping AND every abort precondition (AMBIGUOUS refusal,
        ``original_kind`` defensive refusal, empty-target write-through
        guard); mutation pass only runs after every mapping cleared
        validation. Profile-delete (fresh-profile) or
        update_profile_tools (--into) runs only after the validation
        + mutation passes both completed cleanly.
        """
        # Preflight config.json BEFORE any FS mutation. Active-map /
        # cache cleanup at the tail reads these maps; failing the read
        # at tail position would leave the disk half-recovered with
        # the journal still in flight.
        active_snapshot = self._store.get_active()
        cache_snapshot = self._store.get_active_live_paths_raw()

        # Per-target ownership check BEFORE any mutation (Hermes
        # pass-PR blocker, symmetric to continue). External drift on
        # any target_id's active entry would otherwise let abort
        # silently delete an unrelated active mapping via the pop()
        # loop at the tail.
        self._validate_rescan_target_ownership(record, active_snapshot)

        unique_profiles = sorted(set(record.target_profiles.values()))

        # Validation pass: profile-dir shapes + foreign-profile refusal.
        for name in unique_profiles:
            p = self._store.profile_dir(name)
            if self._resolver.is_link(p):
                raise OpLogCorruptError(
                    f"interrupted rescan abort: {name!r} profile at {p} is a "
                    f"symlink or junction, not a real profile directory; "
                    f"manual recovery required"
                )
            if p.exists() and not p.is_dir():
                raise OpLogCorruptError(
                    f"interrupted rescan abort: {name!r} profile at {p} exists "
                    f"but is not a directory; manual recovery required"
                )
            # --into target-profile existence guard (abby pass-2 blocker,
            # symmetric to the continue path's check). A missing --into
            # target at abort time is corruption: the profile pre-existed
            # before rescan, this abort doesn't own recreating it. Without
            # this gate, validation would fall through, per-mapping
            # mutation would run, and the cleanup pass's
            # update_profile_tools would raise UnknownProfileError mid-
            # mutation — exactly the half-applied state the validate-then-
            # mutate discipline exists to prevent.
            if record.into_mode and not p.is_dir():
                raise OpLogCorruptError(
                    f"interrupted rescan abort: --into target profile "
                    f"{name!r} at {p} is missing — refusing to act on a "
                    f"profile the rescan didn't own. Manual recovery required."
                )
            if p.is_dir():
                try:
                    existing_profile = self._store.get(name)
                except (UnknownProfileError, StorageError) as e:
                    raise OpLogCorruptError(
                        f"interrupted rescan abort: {name!r} profile at {p} "
                        f"is present but metadata.json is missing or "
                        f"unreadable ({e}); refusing to act without verifying "
                        f"ownership. Manual recovery required."
                    ) from e
                expected_tools = self._expected_tools_for_rescan_target(record, name)
                if record.into_mode:
                    # --into mode: accept any state between pre_write
                    # and expected_tools, including partial-mid-write
                    # (CR pass-PR major, symmetric to the continue
                    # path). The mutation pass below restores
                    # previous_tools regardless of which mid-state was
                    # reached on disk.
                    if not self._is_valid_rescan_into_metadata_state(
                        record, name, existing_profile.tools
                    ):
                        pre_write = dict((record.previous_tools or {}).get(name, {}))
                        raise OpLogCorruptError(
                            f"interrupted rescan abort: profile {name!r} at "
                            f"{p} has tools={existing_profile.tools!r}, which is "
                            f"neither {pre_write!r} (pre-write), nor "
                            f"{expected_tools!r} (post-write), nor a valid "
                            f"partial-mid-write between them; refusing to act "
                            f"on a foreign profile. Manual recovery required."
                        )
                else:
                    # Fresh-profile mode: a clean rescan would have written
                    # captured-here as the .tools value. Anything else is
                    # foreign and rmtree would be a data-loss path.
                    if existing_profile.tools != expected_tools:
                        raise OpLogCorruptError(
                            f"interrupted rescan abort: profile {name!r} at "
                            f"{p} has tools={existing_profile.tools!r} but "
                            f"the journal expects {expected_tools!r}; "
                            f"refusing to delete a foreign profile. Manual "
                            f"recovery required."
                        )
                    # Journal-id ownership proof (Hermes pass-PR-2
                    # blocker, symmetric to continue). rmtree on a
                    # profile whose journal_id doesn't match would be
                    # the cross-profile data-loss path.
                    if existing_profile.journal_id != record.rescan_id:
                        raise OpLogCorruptError(
                            f"interrupted rescan abort: profile {name!r} at "
                            f"{p} has journal_id={existing_profile.journal_id!r} "
                            f"but the in-flight rescan expects "
                            f"{record.rescan_id!r}; this profile was not "
                            f"created by this rescan. Refusing to delete a "
                            f"foreign profile. Manual recovery required."
                        )

        # Per-mapping shape + AMBIGUOUS / original_kind / write-through
        # validation. No mutation in this pass.
        states: dict[int, MappingDiskState] = {}
        for i, intent in enumerate(record.mappings):
            profile_name = record.target_profiles.get(intent.tool_id)
            if profile_name is None:
                raise OpLogCorruptError(
                    f"interrupted rescan abort: mapping {intent.tool_id!r}."
                    f"{intent.mapping_index} references tool_id without a "
                    f"target_profiles entry; manual recovery required"
                )
            profile_dir = self._store.profile_dir(profile_name)
            target = profile_dir / intent.profile_subdir
            if self._resolver.is_link(target):
                raise OpLogCorruptError(
                    f"interrupted rescan abort: target {target} for "
                    f"mapping {intent.tool_id!r}.{intent.mapping_index} is a "
                    f"symlink or junction, not a real profile subdirectory; "
                    f"manual recovery required"
                )
            if target.exists() and not target.is_dir():
                raise OpLogCorruptError(
                    f"interrupted rescan abort: target {target} for "
                    f"mapping {intent.tool_id!r}.{intent.mapping_index} "
                    f"exists but is not a directory; manual recovery required"
                )
            state = classify_mapping(intent, profile_dir)
            if state is MappingDiskState.AMBIGUOUS:
                raise OpLogCorruptError(
                    f"interrupted rescan abort: mapping {intent.tool_id!r}."
                    f"{intent.mapping_index} is ambiguous; refusing to "
                    f"abort. Manual recovery required."
                )
            if state is MappingDiskState.UNTOUCHED:
                states[i] = state
                continue
            if intent.original_kind not in ("missing", "real-dir"):
                raise AbortPreflightError(
                    f"mapping {intent.tool_id!r}.{intent.mapping_index}: "
                    f"pre-rescan state was {intent.original_kind!r}; rescan "
                    f"pre-flight rejects this shape, so reaching it at "
                    f"abort time means external drift since intent. "
                    f"Refusing to abort defensively. Manual recovery required."
                )
            # Empty-target write-through guard. Symmetric to init's
            # check (§2.1.1 invariant #1).
            if intent.original_kind == "missing" and target.is_dir() and any(target.iterdir()):
                raise OpLogCorruptError(
                    f"abort would delete non-empty target {target} for "
                    f"mapping {intent.tool_id!r}.{intent.mapping_index} "
                    f"(original_kind='missing'): data may have been "
                    f"written through the symlink. Manual recovery required."
                )
            states[i] = state

        # Mutation pass. Every mapping cleared validation.
        for i, intent in enumerate(record.mappings):
            state = states[i]
            profile_name = record.target_profiles[intent.tool_id]
            profile_dir = self._store.profile_dir(profile_name)
            target = profile_dir / intent.profile_subdir
            live = Path(intent.live_path)
            if state is MappingDiskState.UNTOUCHED:
                continue
            if state is MappingDiskState.COMPLETE:
                remove_link(live)
                if intent.original_kind == "real-dir":
                    move_or_seed_dir(target, live)
                elif intent.original_kind == "missing" and target.exists():
                    shutil.rmtree(target)
            elif state is MappingDiskState.MOVE_DONE_LINK_MISSING:
                if intent.original_kind == "real-dir":
                    move_or_seed_dir(target, live)
                elif intent.original_kind == "missing" and target.exists():
                    shutil.rmtree(target)

        # ConfigFile abort (spec §3.7). Validate every entry first
        # (AMBIGUOUS refusal), then unlink COMPLETE snapshots. For
        # fresh-profile mode the cleanup loop below rmtree's the
        # profile dir and would take the snapshots with it; for
        # --into mode the profile dir is preserved, so per-snapshot
        # unlink is load-bearing — without it, the next rescan into
        # the same profile would trip the snapshot-collision check
        # added in Task 10 even though the snapshot is no longer
        # journal-owned.
        for cf_entry in record.config_file_mappings:
            cf_profile_name = record.target_profiles.get(cf_entry.tool_id)
            if cf_profile_name is None:
                raise OpLogCorruptError(
                    f"interrupted rescan abort: config_file mapping "
                    f"references tool_id {cf_entry.tool_id!r} without a "
                    f"target_profiles entry; manual recovery required"
                )
            cf_profile_dir = self._store.profile_dir(cf_profile_name)
            state = classify_config_file_mapping(cf_entry, cf_profile_dir)
            if state is ConfigFileDiskState.AMBIGUOUS:
                raise OpLogCorruptError(
                    f"interrupted rescan abort: config_file snapshot for "
                    f"tool {cf_entry.tool_id!r} is in an ambiguous state "
                    f"at "
                    f"{cf_profile_dir / '.switcher' / 'config_files' / cf_entry.profile_subdir / cf_entry.profile_filename}; "
                    f"manual recovery required."
                )
        for cf_entry in record.config_file_mappings:
            cf_profile_name = record.target_profiles[cf_entry.tool_id]
            cf_profile_dir = self._store.profile_dir(cf_profile_name)
            state = classify_config_file_mapping(cf_entry, cf_profile_dir)
            if state is ConfigFileDiskState.COMPLETE:
                self._store.config_file_snapshot_path(
                    cf_profile_name,
                    cf_entry.profile_subdir,
                    cf_entry.profile_filename,
                ).unlink(missing_ok=True)

        # Per-profile cleanup. Fresh-profile mode deletes; --into mode
        # restores previous_tools.
        if record.into_mode:
            for name in unique_profiles:
                pre_write = dict((record.previous_tools or {}).get(name, {}))
                current_tools = dict(self._store.get(name).tools)
                if current_tools != pre_write:
                    self._store.update_profile_tools(name, pre_write)
        else:
            for name in unique_profiles:
                if self._store.profile_dir(name).is_dir():
                    self._store.delete(name)

        # Active-map / cache cleanup. Only the target_ids this rescan
        # owns get cleared; external state (other tools' active
        # entries from prior init/rescan) stays untouched. Same
        # orphan-tolerance contract init abort uses.
        for tid in record.target_ids:
            active_snapshot.pop(tid, None)
            cache_snapshot.pop(tid, None)
        self._store.set_active_state(active_snapshot, cache_snapshot)

    def use(self, profile_name: str, only: list[str] | None = None) -> None:
        self._require_initialized()
        # v0.1.4 §3.1/§3.5: empty active map → loud failure rather than
        # a silent no-op switch.
        self._require_managed()
        profile = self._store.get(profile_name)
        active = self._store.get_active()
        managed = set(active.keys())
        registered = {t.id for t in self._registry}
        if only is not None:
            for tid in only:
                if tid not in profile.tools:
                    raise ToolNotInProfileError(
                        f"profile {profile_name!r} does not include {tid!r}"
                    )
                if tid not in managed:
                    # v0.1.4 §3.1 durability: --only must intersect the
                    # managed set, or the explicit request quietly re-
                    # adopts an unmanaged tool's live dir.
                    raise ToolNotManagedError(
                        f"tool {tid!r} is not currently managed. "
                        f"To add it: switcher rescan --only {tid}"
                    )
                if tid not in registered:
                    # Hermes blocker: explicit --only of an orphan id (in
                    # active map but no registry entry, e.g. left behind by
                    # `uninstall --force` non-purge) used to surface the
                    # generic "unknown tool" mid-loop. Surface the orphan
                    # framing up-front with the right remediation.
                    raise UnknownToolError(
                        f"tool {tid!r} is in active map but has no registry entry "
                        f"(orphan); cannot switch. Restore the registry TOML, or "
                        f"run 'switcher unmanage {tid} --force' to drop it."
                    )
            target_ids = list(only)
        else:
            # v0.1.4 §3.1 durability: the default switches only the
            # managed subset of the profile, not every tool in
            # profile.tools. Without this filter, use(other_profile)
            # re-activates a tool the user just unmanaged.
            #
            # Hermes blocker: also drop orphan ids (managed but not in
            # registry). create() carries them into profile.tools for
            # consistency, but use()'s find_tool resolve hard-errors on
            # them. Filtering here keeps the default switch usable on
            # an orphan-bearing active map; the user still sees the
            # orphan in `switcher tools` and can fix it via unmanage.
            target_ids = sorted(profile.tools.keys() & managed & registered)
        # v0.1.4 §3.1 consultant finding: _require_managed already
        # passed (active is non-empty) but this profile contributes
        # zero managed tools. Hard-error rather than silent no-op
        # (matches spec's "Hard error. Preferred." decision).
        if not target_ids:
            raise NoToolsManagedError(
                f"profile {profile_name!r} contains no currently managed tools; nothing to switch"
            )
        # Pre-flight 1: resolve every tool BEFORE mutating any link. A stale
        # tool id in profile.tools (registry drift, plugin removed, hand-edited
        # state) would otherwise surface partway through the swap loop and
        # leave the filesystem half-switched.
        resolved: list[tuple[str, Tool]] = []
        for tid in target_ids:
            tool = find_tool(self._registry, tid)
            if tool is None:
                raise UnknownToolError(f"unknown tool {tid!r}")
            resolved.append((tid, tool))
        # Pre-flight 2: every target subdir must exist before any swap. A
        # profile that's missing one tool's profile_subdir (partial create,
        # registry drift renaming subdirs) would otherwise let earlier tools
        # swap successfully before the missing dir surfaced. Same "validate
        # preconditions, then mutate" discipline as move_or_seed_dir / swap_link.
        for tid, tool in resolved:
            for dm in tool.config_dirs:
                target = self._store.profile_dir(profile_name) / dm.profile_subdir
                # Reject link-shaped targets BEFORE the existence check:
                # ``Path.is_dir()`` follows symlinks and Windows junctions,
                # so a profile subdir replaced with a link to an external
                # directory would pass the existence guard and
                # ``swap_link(target, live)`` would then point ``live`` at
                # the foreign location, effectively repointing the
                # tool's config dir outside the state store. Mirror the
                # link-shape refusal the recovery + classifier paths
                # already apply to reserved state (Hermes pass-PR-4).
                if self._resolver.is_link(target):
                    raise PathNotADirectoryError(
                        f"profile {profile_name!r} subdir {dm.profile_subdir!r} "
                        f"for tool {tid!r} is a symlink or junction, not a "
                        f"real directory; refusing to switch through a link "
                        f"that could point outside the state store"
                    )
                if not target.is_dir():
                    raise PathNotADirectoryError(
                        f"profile {profile_name!r} is missing "
                        f"{dm.profile_subdir!r} for tool {tid!r}"
                    )
        # Capture phase first: snapshot the *currently-active* live state
        # into each tool's source profile before anything else. This must
        # precede the plan phase below — for `switcher use <active>`, source
        # and destination are the same profile, so plan needs to read the
        # *post-capture* snapshot (otherwise an in-flight live edit is
        # planned-away with the pre-capture content).
        #
        # Capture only writes to source profile's snapshot, never to live —
        # so a failure here leaves live untouched. Source-snapshot updates
        # are idempotent on retry (re-capturing current live overwrites with
        # the same content); partial-tool capture is benign.
        #
        # No ``source != profile_name`` guard: `switcher use <active>` is a
        # legitimate operation (reload-from-snapshot affordance), but with a
        # guard the apply would overwrite live with the last-saved snapshot
        # and silently discard any in-flight edits. With capture-then-apply,
        # the same call captures-then-no-ops on live, which is data-safe.
        for tid, tool in resolved:
            source = active.get(tid)
            if not source:
                continue
            # Four-way dispatch on where the live config_dirs symlinks
            # actually point (CR pass-PR-3 major + dangling-symlink
            # carve-out for failed-rename recovery):
            #
            # (1) Links match the active-map source → normal pre-switch
            #     capture into source's snapshot. Preserves in-flight
            #     edits the user made while ``source`` was the active
            #     profile.
            #
            # (2) Links match the DESTINATION profile we're switching to.
            #     This is the stale-active drift case from abby r11 —
            #     a prior use() flipped symlinks to ``profile_name`` but
            #     never committed ``set_active_state``. Pre-fix the
            #     branch silently skipped capture; that drops any
            #     in-flight edits the user made while ``profile_name``
            #     was effectively active on disk, because the apply
            #     phase below would overwrite live with profile_name's
            #     pre-edit snapshot. Capture into ``profile_name`` so
            #     the apply is a no-op write of the bytes we just
            #     captured and the user's edits survive.
            #
            # (3) At least one config_dir live path is a DANGLING link
            #     (resolves to nothing — typical post-failed-rename
            #     recovery shape where ``store.rename`` moved the dir
            #     but ``swap_link`` failed to retarget). There's no
            #     live data behind a dangling link to either capture or
            #     overwrite, so skip capture and let the apply +
            #     swap_link below complete the recovery cleanly.
            #
            # (4) Links resolve to neither source nor destination AND
            #     aren't dangling — they point at a third profile or
            #     an external directory. Genuinely ambiguous drift;
            #     refuse loudly rather than silently overwrite that
            #     third profile's data with destination bytes.
            if self._symlink_matches_active_source(tool, source):
                self._capture_config_files(source, tool)
                continue
            if self._symlink_matches_active_source(tool, profile_name):
                self._capture_config_files(profile_name, tool)
                continue
            if self._any_live_dir_dangling(tool):
                continue
            raise StorageError(
                f"live config_dirs for {tid!r} match neither active source "
                f"{source!r} nor destination {profile_name!r}; refusing to "
                f"overwrite ConfigFile state blindly. Manual recovery required."
            )
        # Pre-flight 3: plan every tool's ConfigFile applies. Read-only — no
        # filesystem mutation. Any malformed-JSON / non-object / walker-
        # rejection error raises StorageError here, before swap_link has
        # flipped any symlinks. Closes the "half-applied switch on parse
        # failure" gap abby r4 flagged.
        #
        # Per-tool plans are kept in a dict so the commit phase below can
        # consume each tool's plan immediately after its swap_link loop —
        # preserves the "links first, file overlay second" ordering inside
        # a single tool while keeping all parses upfront across tools.
        config_file_plans: dict[str, list[tuple[Path, bytes]]] = {
            tid: self._plan_config_file_applies(profile_name, tool) for tid, tool in resolved
        }
        for tid, tool in resolved:
            # Within-tool rollback: if any post-swap step (atomic_write_file)
            # raises a runtime failure that pre-flight couldn't catch (disk
            # full, EACCES, EROFS, an unexpected target shape), swap_link
            # back to the source profile so the tool isn't left with
            # config_dirs pointing at the destination but ConfigFile state
            # still at the source. Best-effort: rollback exceptions are
            # swallowed so the user sees the *original* failure that the
            # commit was trying to recover from.
            #
            # Cross-tool rollback (undoing prior tools whose swap+write
            # already succeeded) is intentionally out of scope — symmetric
            # to the existing config_dirs swap_link loop, which has always
            # accepted partial-multi-tool mutation as op-log territory
            # (spec §2.7; the next `switcher init --continue` is the
            # architectural cleanup path). Adding per-loop unwinding here
            # would duplicate op-log work without the journaling that
            # makes it crash-safe.
            # ``active[tid]`` is guaranteed populated for every ``tid`` in
            # resolved by the target_ids filter at the top of use() (both
            # the default path's ``profile.tools.keys() & managed`` and the
            # --only path's explicit ``tid not in managed`` check). Use
            # ``.get()`` defensively so a future refactor that loosens that
            # filter doesn't silently turn an invariant violation into a
            # raw KeyError on the rollback path (abby r8). ``None`` means
            # "no prior profile known for this tool" — skip rollback, since
            # there's no source profile to swap_link back to.
            source_profile = active.get(tid)
            swapped: list[tuple[Path, Path]] = []
            try:
                for i, dm in enumerate(tool.config_dirs):
                    target = self._store.profile_dir(profile_name) / dm.profile_subdir
                    live = self._resolver.tool_dir(tool, i)
                    if source_profile is not None:
                        source_target = self._store.profile_dir(source_profile) / dm.profile_subdir
                        swap_link(target, live)
                        swapped.append((live, source_target))
                    else:
                        swap_link(target, live)
                # Symlink swap first, then file overlay — the dir-mapping
                # flip is visible to the tool before ConfigFile state is
                # reconciled. Tool validator caps config_files at 1, so
                # this is at most one write per tool.
                for live_path, content in config_file_plans[tid]:
                    atomic_write_file(live_path, content)
            except Exception:
                for live, source_target in reversed(swapped):
                    with contextlib.suppress(Exception):
                        swap_link(source_target, live)
                raise
            active[tid] = profile_name
        # Combined write that also flushes any derived migration entries.
        self._store.set_active_state(active, self._derive_cache_for_active(active))

    def save(self, name: str) -> None:
        """Snapshot live config into a new profile.

        Only currently-managed AND currently-installed tools are snapshotted.

        v0.1.4 §3.2: filtered by active.keys(). Two affected scenarios:
          1. Pre-v0.1.4 env_override workaround users stop seeing
             intentionally-excluded tools captured under unrelated
             profile names.
          2. A tool installed live AFTER init (so it's not in active)
             is no longer implicitly added to new snapshots. Run
             `switcher rescan --only <tool>` first to bring it under
             management.

        Tool-in-active-but-no-longer-installed-live is still intentionally
        skipped: save's contract is "snapshot live state", and that data is
        already preserved under the active profile — `use(other_profile)`
        won't disturb it.
        """
        self._require_initialized()
        # v0.1.4 §3.5: empty active map → fail loud rather than create
        # an empty profile silently.
        self._require_managed()
        if self._store.profile_dir(name).exists():
            raise ProfileExistsError(f"profile {name!r} already exists")
        managed = set(self._store.get_active().keys())
        installed = [t for t in self.detect_installed() if t.id in managed]
        # v0.1.4 §3.5: closing the second silent-empty-profile hole.
        # _require_managed already passed (active is non-empty), but if every
        # managed tool was uninstalled outside switcher (or the registry was
        # reshuffled so no managed id resolves), `installed` is empty and the
        # downstream loop would persist a profile with no tools dict entries
        # and no captured data. Fail loud, with a hint that mirrors the spec's
        # advice for the empty-active-map case.
        if not installed:
            raise NoToolsManagedError(
                "no managed tools are currently installed live; nothing to snapshot. "
                "reinstall the tools or run 'switcher uninstall' to drop stale entries"
            )
        # Pre-flight: every live path that detect_installed surfaced must be
        # a real directory. Two pathological shapes slip through if we use
        # plain `live.exists()` here: a regular file (exists True, is_dir
        # False) and a dangling symlink (exists False, is_symlink True — so
        # detect_installed via resolver.exists() includes it, but a naive
        # exists() check would skip it). Routing through resolver.exists()
        # mirrors the detect step exactly: anything detected as installed
        # must validate as a directory or fail loud. Matches move_or_seed_dir's
        # stance in init(): fail loudly rather than silently snapshot an empty
        # subdir behind a profile that looks valid until the user uses it.
        for tool in installed:
            for i in range(len(tool.config_dirs)):
                live = self._resolver.tool_dir(tool, i)
                if not self._resolver.exists(live):
                    continue
                src = live.resolve() if live.is_symlink() else live
                if not src.is_dir():
                    raise PathNotADirectoryError(
                        f"{live} exists but is not a directory; cannot snapshot"
                    )
        self._store.create(name, {t.id: True for t in installed})
        try:
            for tool in installed:
                for i, dm in enumerate(tool.config_dirs):
                    live = self._resolver.tool_dir(tool, i)
                    target = self._store.profile_dir(name) / dm.profile_subdir
                    target.mkdir(parents=True, exist_ok=True)
                    if not live.exists():
                        continue
                    # Resolve through the symlink so we copy the actual data
                    # under the active profile, not the link itself.
                    src = live.resolve() if live.is_symlink() else live
                    shutil.copytree(src, target, dirs_exist_ok=True)
                self._capture_config_files(name, tool)
        except Exception:
            # No-debris discipline: copytree can fail mid-snapshot for
            # runtime reasons that pre-flight can't catch (transient I/O,
            # permissions, concurrent deletion). Without rollback the
            # partial profile would block save() retry with
            # ProfileExistsError. Mirrors store.create()'s own rollback on
            # metadata-write failure.
            shutil.rmtree(self._store.profile_dir(name), ignore_errors=True)
            raise

    def create(self, name: str) -> None:
        """Create a new profile, seeding credentials from each tool's active source.

        The tool set is taken from the *active* map, not from
        ``detect_installed()``: create() is a state-store data copy, not a
        live-config operation. A tool that's currently uninstalled but
        already managed by switcher (e.g., user temporarily removed it)
        must still be carried into the new profile so its credentials
        survive the round-trip. Orphan tool IDs (in active but not in the
        registry) keep their entry in ``profile.tools`` for consistency
        but skip the credential-seeding step (no known config_dirs).

        Unlike init(), no live data is moved — only credential files are
        copied from existing profile dirs. That makes the rollback safe:
        on any failure during the seeding loop, rmtree the half-built
        profile so a retry isn't blocked by ProfileExistsError. Mirrors
        save()'s rollback discipline.
        """
        self._require_initialized()
        # v0.1.4 §3.5: empty active map → fail loud rather than create
        # an empty-tools profile that looks valid until first use.
        self._require_managed()
        if self._store.profile_dir(name).exists():
            raise ProfileExistsError(f"profile {name!r} already exists")
        active = self._store.get_active()
        self._store.create(name, dict.fromkeys(active, True))
        try:
            for tid, src_profile in active.items():
                tool = find_tool(self._registry, tid)
                if tool is None:
                    # Orphan: registry drift since init. Tool entry stays
                    # in profile.tools so the management surface is
                    # accurate, but we can't seed credentials (config_dirs
                    # / credentials list unknown).
                    continue
                self._seed_credentials(src_profile, name, tool)
                self._seed_config_files(src_profile, name, tool)
        except Exception:
            shutil.rmtree(self._store.profile_dir(name), ignore_errors=True)
            raise

    def which(self, tool_id: str) -> str:
        """Return the active profile name for `tool_id`.

        Distinguishes two failure modes the spec keeps separate: a tool ID
        that the registry doesn't know about (``UnknownToolError``) vs. a
        registered tool that just hasn't been activated yet
        (``ToolHasNoActiveProfileError``). Routing both through the latter
        would let typos masquerade as "no profile" answers.
        """
        self._require_initialized()
        if find_tool(self._registry, tool_id) is None:
            raise UnknownToolError(f"unknown tool {tool_id!r}")
        active = self._store.get_active()
        if tool_id not in active:
            raise ToolHasNoActiveProfileError(f"tool {tool_id!r} has no active profile")
        return active[tool_id]

    def rename(self, old: str, new: str) -> None:
        """Rename a profile, re-pointing affected live links to the new name.

        Pre-flight catches the deterministic failure modes (real directory
        at a live path, target name in use). Orphan tool IDs in the active
        map (registry drift since init) are tolerated: their active entry
        gets re-pointed to ``new`` so the map stays consistent with the
        store, but no relinking is attempted since the tool's config_dirs
        aren't known.

        Commit ordering matters for failure recovery:

        1. ``store.rename(old, new)`` — atomic directory move.
        2. ``set_active(updated)`` — atomic tmp+rename of state.json. Once
           this succeeds, the rename is *logically committed*: store and
           active map both reference ``new``, and the work that remains is
           pure link-fixup.
        3. Swap each affected tool's live link to the new target. Any
           failure here leaves *some* live links pointing at a now-missing
           ``old`` path, but the canonical state is consistent. Recovery
           is ``use(<new>)``, which runs swap_link for every tool in the
           profile; it's idempotent on already-relinked tools.

        v0.1.5: an op-log intent record is appended BEFORE store.rename
        runs and marked completed after the final
        ``set_active_live_paths``. A crash in the narrow failure window
        between steps 1 and 2 leaves the in-flight record for the next
        CLI command's detection hook to auto-compensate via
        :meth:`_compensate_rename` (idempotent disk-truth roll-forward;
        no user input). See spec §2.3.
        """
        self._require_initialized()
        if not self._store.profile_dir(old).exists():
            raise UnknownProfileError(f"profile {old!r} not found")
        if self._store.profile_dir(new).exists():
            raise ProfileExistsError(f"profile {new!r} already exists")
        # Pre-flight: every affected tool's live path must be a link (broken
        # or valid) or a non-existent path. A real directory at the live path
        # would let store.rename succeed, then swap_link refuse mid-loop with
        # IsADirectoryError, leaving the rename half-applied (profile dir
        # moved, some live links updated, others stale). Same "validate, then
        # mutate" discipline as use() / save() / init().
        active = self._store.get_active()
        affected_ids = [tid for tid, p in active.items() if p == old]
        for tid in affected_ids:
            tool = find_tool(self._registry, tid)
            if tool is None:
                continue
            for i in range(len(tool.config_dirs)):
                live = self._resolver.tool_dir(tool, i)
                if not self._resolver.is_link(live) and live.exists():
                    if live.is_dir():
                        raise PathNotADirectoryError(
                            f"{live} is a real directory, not a switcher link; "
                            f"refusing to rename {old!r} to {new!r}"
                        )
                    raise PathNotADirectoryError(
                        f"{live} exists but is not a directory; cannot relink"
                    )
        # v0.1.5: record intent BEFORE any FS mutation. affected_ids carries
        # SafeName-validated profile/tool ids (no path canonicalization
        # needed at this entry point — rename does not persist live_paths).
        oplog = OpLogIO(self._store.state_dir())
        intent = _RenameOp.model_validate(
            {
                "op": "rename",
                "started_at": now(),
                "from": old,
                "to": new,
                "affected_ids": list(affected_ids),
            }
        )
        oplog.append_record(intent)
        # Cancellation scope is narrow on purpose. store.rename has explicit
        # pre-mutation guards at the top of its body (UnknownProfileError
        # if `old` vanished; ProfileExistsError if `to` appeared between
        # OUR preflight and store.rename's own check) — both raised before
        # any FS write. For those races, the disk is untouched, so dropping
        # the in-flight intent prevents the next CLI command from
        # "compensating" work that never started. Every OTHER exception
        # (mid-store.rename OSError after the metadata write but before
        # the dir replace, set_active failure after store.rename committed
        # the move, swap_link failure mid-loop, mark_completed itself
        # failing) may have left durable partial state — those are exactly
        # the cases the journal exists to recover, so the intent must
        # survive. abby-review batch-1 pass-3 blocking finding: a broad
        # except Exception here would silently swallow the recovery record
        # for those compensable failures.
        #
        # cancel_intent is best-effort for the I/O class of failures only:
        # if the same FS error that broke the rename also breaks the
        # journal's tmp+rename write (StorageError), prefer the original
        # rename exception so the user sees the root cause, not the
        # follow-on. OpLogCorruptError surfaces a different concern —
        # the journal was externally modified between our append_record
        # and the cancel attempt — and must propagate (the race exception
        # becomes the implicit __context__ so the traceback has both).
        # Suppressing OpLogCorruptError here would hide journal corruption
        # behind a transient race and undermine the "surface corruption
        # loudly" contract cancel_intent itself enforces.
        try:
            self._store.rename(old, new)
        except (UnknownProfileError, ProfileExistsError):
            with contextlib.suppress(StorageError):
                oplog.cancel_intent(intent)
            raise
        # Persist the active-map update IMMEDIATELY after the dir rename:
        # once both succeed, the rename is logically committed and any
        # subsequent swap_link failure is recoverable via `use(<new>)`.
        # Running set_active LAST (after the swap loop) would leave the
        # active map pointing at a name that no longer exists in the store
        # whenever swap_link fails mid-loop — an unrecoverable state.
        # Orphan tool IDs (in active but not in the registry) get
        # re-pointed too, since their active entry must stay consistent
        # with the store regardless of relink-ability.
        for tid in affected_ids:
            active[tid] = new
        # Step 2: commit the active-map update. set_active is the wrapper
        # that round-trips the existing on-disk active_live_paths cache,
        # so an already-populated cache survives. The migration flush
        # comes AFTER swap_link below — at this point the symlinks still
        # point at `<old>` (Path.resolve() returns the stored target
        # verbatim, even when it no longer exists), so the strict
        # validator can't yet produce a usable cache entry.
        self._store.set_active(active)
        for tid in affected_ids:
            tool = find_tool(self._registry, tid)
            if tool is None:
                continue
            for i, dm in enumerate(tool.config_dirs):
                target = self._store.profile_dir(new) / dm.profile_subdir
                live = self._resolver.tool_dir(tool, i)
                swap_link(target, live)
        # Post-relink migration flush: now that every affected symlink
        # points into <new>, _derive_cache_for_active can validate them
        # against the post-rename active map. This is a second atomic
        # write — safe because the canonical state (store dir + active
        # map) is already consistent; the cache is a derived index, not
        # load-bearing for recovery (recovery path is `use(<new>)`
        # regardless).
        self._store.set_active_live_paths(self._derive_cache_for_active(active))

        # v0.1.5: mark the intent record completed.
        oplog.mark_completed(intent)

    def _compensate_rename(self, record: _RenameOp) -> None:
        """Idempotent disk-truth roll-forward of a partial rename (spec §2.3).

        Derives progress from disk on every call:
          - from_dir_exists, to_dir_exists determine whether step 1 (store.rename) ran.
          - Active map contents determine whether step 2 (set_active) ran.
          - swap_link is idempotent on already-correct links, so step 3 is replayed
            unconditionally for every affected_id in the registry.

        Refuses if BOTH `from` and `to` dirs exist (data in two places, ambiguous)
        or NEITHER exists (manual intervention since intent).

        Important: the intent record is written BEFORE store.rename runs. The
        `from_dir_exists AND NOT to_dir_exists` state means: intent was written,
        but store.rename never ran (crash between append_record and store.rename,
        OR inside store.rename before the directory replace). Compensation rolls
        FORWARD by running store.rename ourselves — the user asked for this
        rename and the intent record commits us to completing it.

        On successful return, the in-flight intent is marked completed so the
        next vacuum drops it. Failure paths re-raise without marking; the
        record stays in-flight for the next compensation pass to pick up.
        Mirrors the init/rescan compensation lifecycle (Tasks 6.3 / 7.2):
        the service method that knows the record is fully recovered owns
        the journal transition.
        """
        from_path = self._store.profile_dir(record.from_)
        to_path = self._store.profile_dir(record.to)
        # Path.exists() follows the link/file/dir distinction loosely;
        # a regular file at profiles/<from>/ or profiles/<to>/ would
        # otherwise look like a legitimate rename step state and slip
        # past the dual-existence guards, only to fail later inside
        # store.rename or relinking (CodeRabbit recurring review).
        # is_dir() narrows existence to "actual profile directory";
        # anything else (file, broken link, special) is corruption.
        #
        # is_dir() ALSO follows symlinks / Windows junctions through to
        # their target — so a hand-edited profile dir that's actually a
        # symlink pointing at an unrelated dir would slip past as
        # "valid". Reject any link shape (cross-platform via
        # `_resolver.is_link`) before the .is_dir() narrow (CodeRabbit
        # PR review). The profile store invariant is "profile_dir is a
        # real directory"; anything else came from outside switcher.
        for label, path in (("from", from_path), ("to", to_path)):
            if self._resolver.is_link(path):
                raise OpLogCorruptError(
                    f"interrupted rename {record.from_!r} -> {record.to!r}: "
                    f"{path} ({label}) is a symlink or junction, not a real "
                    f"profile directory; manual recovery required"
                )
            if path.exists() and not path.is_dir():
                raise OpLogCorruptError(
                    f"interrupted rename {record.from_!r} -> {record.to!r}: "
                    f"{path} exists but is not a directory; manual recovery required"
                )
        from_dir_exists = from_path.is_dir()
        to_dir_exists = to_path.is_dir()

        if from_dir_exists and to_dir_exists:
            raise OpLogCorruptError(
                f"interrupted rename {record.from_!r} -> {record.to!r}: both "
                f"profile dirs exist on disk; manual recovery required"
            )
        if not from_dir_exists and not to_dir_exists:
            raise OpLogCorruptError(
                f"interrupted rename {record.from_!r} -> {record.to!r}: "
                f"neither profile dir exists on disk; manual recovery required"
            )

        # Pre-flight (mirrors service.rename): every affected tool's live path
        # must be a link (broken or valid) or non-existent. A real directory
        # or regular file there would let store.rename / set_active run, then
        # swap_link would refuse mid-loop, leaving the active map at `to`
        # while a stale live link still references `from` — exactly the
        # partial-apply shape this compensation is supposed to resolve. The
        # window is real: a user who notices a missing live dir post-crash
        # and runs `mkdir ~/.claude` before re-invoking switcher will land
        # here. Validate before any mutation; same discipline as rename().
        for tid in record.affected_ids:
            tool = find_tool(self._registry, tid)
            if tool is None:
                continue
            for i in range(len(tool.config_dirs)):
                live = self._resolver.tool_dir(tool, i)
                if not self._resolver.is_link(live) and live.exists():
                    if live.is_dir():
                        raise PathNotADirectoryError(
                            f"interrupted rename {record.from_!r} -> {record.to!r}: "
                            f"{live} is a real directory, not a switcher link; "
                            f"remove it (or restore the original symlink) before "
                            f"compensation can complete"
                        )
                    raise PathNotADirectoryError(
                        f"interrupted rename {record.from_!r} -> {record.to!r}: "
                        f"{live} exists but is not a directory; cannot relink"
                    )

        # Drift guards (run BEFORE the store.rename replay so a corrupt
        # journal doesn't trigger further canonical-state mutation):
        # the active map's affected slice must be in a coherent rename
        # phase, and no UNEXPECTED active entries may reference either
        # endpoint of the rename. The real `service.rename` sequence is
        #
        #   (1) store.rename(old, new)                  # profile dir move
        #   (2) for tid in affected_ids: active[tid]=new  # in-memory rewrite
        #   (3) store.set_active(active)                # atomic persist
        #   (4) swap_link loop                          # live link relink
        #
        # The legitimate post-crash phases are therefore:
        #   A. pre-(1): from dir exists, all affected at `from_`
        #   B. post-(1) pre-(3): to dir exists, all affected at `from_`
        #   C. post-(3): to dir exists, all affected at `to_`
        # Anything else (mixed affected, affected at `to_` while from
        # dir still exists, third-profile drift, unexpected extra refs
        # to `from_/to_` outside affected_ids) cannot come from the
        # real rename sequence. Auto-healing it would silently drop the
        # user's original intent or bless externally-mutated state as
        # recovered. Refuse loudly and leave the intent in flight.
        # Hermes PR review convergence with CodeRabbit's earlier
        # plateau on the same surface.
        active = dict(self._store.get_active())
        affected_set = set(record.affected_ids)

        # 1. Affected slice must live entirely within {from_, to}.
        drifted = {
            tid: active.get(tid)
            for tid in record.affected_ids
            if active.get(tid) not in {record.from_, record.to}
        }
        if drifted:
            raise OpLogCorruptError(
                f"interrupted rename {record.from_!r} -> {record.to!r}: "
                f"affected active-map entries drifted outside the expected "
                f"{{{record.from_!r}, {record.to!r}}} set: {drifted!r}. "
                f"Manual recovery required."
            )

        # 2. Phase coherence: every affected_id must be at the SAME
        #    endpoint — `set_active` is atomic, so a split affected
        #    set cannot be produced by the real rename sequence.
        affected_phase = {active[tid] for tid in record.affected_ids}
        if len(affected_phase) > 1:
            raise OpLogCorruptError(
                f"interrupted rename {record.from_!r} -> {record.to!r}: "
                f"affected active-map entries are split across both endpoints "
                f"{affected_phase!r} — `set_active` is atomic so this state "
                f"cannot come from the real rename sequence. Manual recovery "
                f"required."
            )

        # 3. from_dir_exists implies pre-(1): no affected entry should
        #    already point at `to`. Affected-at-`to_` means step (2)
        #    ran, which in turn means step (1) ran first — so `from`
        #    dir must be gone. Seeing both is externally-mutated state.
        if from_dir_exists and record.to in affected_phase:
            raise OpLogCorruptError(
                f"interrupted rename {record.from_!r} -> {record.to!r}: "
                f"source dir {from_path} still exists, yet affected active-map "
                f"entries already reference {record.to!r}. step (2) of the "
                f"rename runs after step (1); this phase is unreachable from "
                f"the real sequence. Manual recovery required."
            )

        # 4. No tool outside `affected_ids` may reference either rename
        #    endpoint. Such an entry was not part of the journal's
        #    "what to recover" snapshot: rolling the rename forward
        #    would leave it pointing at `from_` (a profile name that's
        #    about to disappear) or pre-bless a `to`-reference that
        #    came from somewhere else. Either way, the journal would
        #    get marked completed while canonical state remains
        #    inconsistent (Hermes PR review robustness gap; CodeRabbit
        #    recurring).
        extra_refs = {
            tid: profile
            for tid, profile in active.items()
            if tid not in affected_set and profile in {record.from_, record.to}
        }
        if extra_refs:
            raise OpLogCorruptError(
                f"interrupted rename {record.from_!r} -> {record.to!r}: "
                f"active-map entries outside record.affected_ids still "
                f"reference one of the rename endpoints: {extra_refs!r}. "
                f"Manual recovery required."
            )

        if from_dir_exists:
            # Intent written, store.rename never completed (or completed
            # only its first step — the metadata rewrite — before the
            # dir replace failed). Roll FORWARD by re-running store.rename:
            # the user committed to this rename when the intent record
            # landed, and a no-op interpretation would silently drop it.
            #
            # Replay is safe because FileProfileStore.rename is documented
            # idempotent for retry (store.py:162-167): the metadata
            # rewrite is the same content on a second pass, and a
            # half-completed prior call where metadata.name already says
            # `to` is reconciled by store.get's name/dir mismatch repair
            # (store.py:131-138) so the read returns a clean Profile.
            # If the underlying failure that broke the first attempt has
            # cleared (transient FS error, race resolved), the second
            # pass commits cleanly; if it persists, the user sees the
            # same error and can investigate.
            self._store.rename(record.from_, record.to)

        # Step 2: re-point any affected entry that still references `from`.
        affected_still_at_old = [
            tid for tid in record.affected_ids if active.get(tid) == record.from_
        ]
        if affected_still_at_old:
            for tid in affected_still_at_old:
                active[tid] = record.to
            self._store.set_active(active)

        # Step 3: swap_link for every affected tool whose active entry is `to`
        # and is in the registry (orphans skip link-fixup — same as the
        # original rename method).
        for tid in record.affected_ids:
            if active.get(tid) != record.to:
                continue
            tool = find_tool(self._registry, tid)
            if tool is None:
                continue
            for i, dm in enumerate(tool.config_dirs):
                target = self._store.profile_dir(record.to) / dm.profile_subdir
                live = self._resolver.tool_dir(tool, i)
                swap_link(target, live)

        # Refresh the live-paths cache (the original rename does this too).
        self._store.set_active_live_paths(self._derive_cache_for_active(active))

        # v0.1.5: mark the in-flight intent completed so the next vacuum
        # drops it. Locating the journal here (not as a constructor field)
        # mirrors the rename / init / rescan write paths and keeps the
        # _compensate_rename surface a pure (state-dir + record) function
        # of the service it was constructed against.
        OpLogIO(self._store.state_dir()).mark_completed(record)

    def delete(self, name: str) -> None:
        """Delete a profile, refusing if it's active for any tool.

        The active-profile check is the safety net against orphaning live
        links: deleting a profile that some tool is currently using would
        leave the live link pointing at a now-missing target. The user
        must explicitly switch off the profile (via ``use(<other>)``)
        before deletion is permitted.
        """
        self._require_initialized()
        if not self._store.profile_dir(name).exists():
            raise UnknownProfileError(f"profile {name!r} not found")
        active = self._store.get_active()
        active_for = sorted(tid for tid, p in active.items() if p == name)
        if active_for:
            raise ProfileIsActiveError(
                f"profile {name!r} is active for: {', '.join(active_for)}. "
                "Switch the active tools to a different profile before deleting."
            )
        self._store.delete(name)

    def uninstall(
        self,
        *,
        purge: bool = False,
        yes: bool = False,
        dry_run: bool = False,
        force: bool = False,
    ) -> UninstallReport:
        """Inverse of init: every active symlink → real dir; optionally rm -rf state.

        See spec §3 for full semantics. Dry-run bypasses non-TTY and skipped-tool
        guards (§3.1: "shows both phases without mutating or prompting").

        ConfigFile invariant (spec §3.6): non-purge mode does NOT touch the
        live config file (no symlink to break, unlike DirMapping) and
        preserves per-profile ConfigFile snapshots alongside dir snapshots
        — the asymmetric alternative is silent data loss. Purge mode's
        ``shutil.rmtree(state_dir)`` carries snapshots away with the rest
        of state.
        """
        self._require_initialized()

        # Pre-flight step 2: --purge non-TTY guard. Skipped during dry-run.
        if purge and not dry_run and not yes and not sys.stdin.isatty():
            raise UninstallPreflightError("refusing to purge without --yes in non-interactive mode")

        # Pre-flight step 3: classify every DirMapping.
        mappings = self._classify_uninstall_mappings()

        # Pre-flight step 4: per-tool source-of-info check.
        active = self._store.get_active()
        live_paths_cache = self.get_active_live_paths()
        skipped_tools: list[tuple[str, str]] = []
        for tool_id in active:
            has_cache = bool(live_paths_cache.get(tool_id))
            has_registry = find_tool(self._registry, tool_id) is not None
            if not has_cache and not has_registry:
                if not force:
                    raise UninstallPreflightError(
                        f"orphan tool {tool_id!r}: no registry entry and no cached "
                        f"live_paths. Restore the registry TOML, or pass --force "
                        f"to skip this tool (its symlinks will remain in place)."
                    )
                skipped_tools.append((tool_id, "no registry entry and no cached live_paths"))

        # Pre-flight step 5: --purge + skipped-tools refusal. Skipped during dry-run.
        if purge and not dry_run and skipped_tools:
            tids = ", ".join(t for t, _ in skipped_tools)
            raise UninstallPreflightError(
                f"--purge refused: tool(s) {tids} would be skipped, leaving symlinks "
                f"dangling into a wiped state dir. Either restore registry TOML(s), "
                f"or run uninstall (no purge) first, then prune separately."
            )

        # Pre-flight step 3 follow-up: reject any CORRUPT classification (not skippable
        # via --force per §3.4) for tools that aren't in skipped_tools.
        skipped_ids = {t for t, _ in skipped_tools}
        for m in mappings:
            if m.state == UninstallMappingState.CORRUPT and m.tool_id not in skipped_ids:
                raise UninstallPreflightError(
                    f"tool {m.tool_id!r} mapping {m.profile_subdir!r}: {m.corruption_reason}"
                )

        report = UninstallReport(skipped=skipped_tools, mappings=[], purged=False)

        if dry_run:
            for m in mappings:
                if m.tool_id in skipped_ids:
                    continue
                report.mappings.append(m)
            return report

        # Execute per-DirMapping unwind for every non-skipped mapping.
        for m in mappings:
            if m.tool_id in skipped_ids:
                continue
            self._execute_uninstall_mapping(m)
            report.mappings.append(m)

        # Compute the post-unwind state-clear: skipped tools keep their entries
        # (their symlinks stayed in place), everything else is cleared.
        kept_active = {tid: p for tid, p in active.items() if tid in skipped_ids}
        kept_cache = {tid: paths for tid, paths in live_paths_cache.items() if tid in skipped_ids}

        # Non-purge: clear + return.
        if not purge:
            self._store.set_active_state(kept_active, kept_cache)
            return report

        # Purge phase.
        if not yes:
            try:
                answer = (
                    input(
                        f"This will permanently delete {self._store.state_dir()}. "
                        f"Type 'yes' to confirm: "
                    )
                    .strip()
                    .lower()
                )
            except (EOFError, KeyboardInterrupt):
                # Aborted prompt (stdin closed, Ctrl-C, etc.) — the unwind
                # already ran, so clear the active state before propagating
                # the abort. Otherwise config.json would still claim tools
                # are managed even though their live dirs are real again.
                # Hermes review: previously the raw input() exception
                # bypassed the cleanup that the "declined" branch runs.
                self._store.set_active_state(kept_active, kept_cache)
                raise
            if answer != "yes":
                # User declined. Unwind is already done — clear active state
                # so subsequent `status` correctly reports "no tools managed."
                # Equivalent to a non-purge run that the user explicitly chose.
                self._store.set_active_state(kept_active, kept_cache)
                return report
        # Clear active state BEFORE the destructive rmtree. Pre-flight step 5
        # already refused purge if any tools would be skipped, so kept_active
        # and kept_cache are guaranteed empty here. If rmtree subsequently
        # fails partway, the on-disk config still reads "no tools managed" —
        # matching the already-restored live dirs — rather than lying about
        # active entries whose symlinks no longer exist.
        self._store.set_active_state(kept_active, kept_cache)
        shutil.rmtree(self._store.state_dir())
        report.purged = True
        return report

    # Unmanage ---------------------------------------------------------------

    def unmanage(
        self,
        tool_id: str,
        *,
        dry_run: bool = False,
        force: bool = False,
    ) -> UnmanageReport:
        """Single-tool uninstall. Spec §2.2.

        Restores the tool's live path(s) and removes it from active +
        active_live_paths. Mirrors v0.1.3 uninstall's per-DirMapping
        atomicity (classification + automatic resume via
        _execute_uninstall_mapping). CORRUPT mappings always refuse
        regardless of --force; --force handles only the orphan-no-cache
        case (matches uninstall --force at service.py's pre-flight).

        ConfigFile invariant: live config files are intentionally
        untouched. There is no symlink to break (unlike
        DirMapping/restore_real_dir), and writing the snapshot back
        would destroy any drift the tool produced since the last
        switch. Per-profile snapshots also stay on disk — same
        preservation discipline as dir snapshots.
        """
        self._require_initialized()
        self._require_managed()
        active = self._store.get_active()
        if tool_id not in active:
            raise ToolNotManagedError(
                f"tool {tool_id!r} is not managed; nothing to unmanage. "
                f"To add it: switcher rescan --only {tool_id}"
            )

        # Orphan-no-cache check (mirrors uninstall pre-flight).
        live_paths_cache = self.get_active_live_paths()
        has_cache = bool(live_paths_cache.get(tool_id))
        has_registry = find_tool(self._registry, tool_id) is not None
        skipped_orphan = False
        if not has_cache and not has_registry:
            if not force:
                raise UninstallPreflightError(
                    f"orphan tool {tool_id!r}: no registry entry and no cached "
                    f"live_paths. Restore the registry TOML, or pass --force "
                    f"to skip this tool (its symlinks will remain in place)."
                )
            # --force on orphan-no-cache: dropping the active entry while
            # owned profile data still exists on disk lets a later
            # `uninstall --purge` silently destroy that data — the tool is
            # no longer in `active`, so its purge-time skipped-tool guard
            # at the uninstall pre-flight never fires (Hermes blocker
            # post-PR-#5).
            #
            # Refuse if any owned subdir for this tool exists in the
            # profile dir (current registry subdirs union historical ones
            # via `_expected_subdirs_for`). The user's escape: restore
            # the registry TOML and re-run normal `unmanage`, which
            # cleans up properly; or delete the subdirs manually first.
            #
            # Note on symlinks: derive-on-read in `get_active_live_paths()`
            # above already catches the case where a live symlink at one of
            # the tool's known paths points into an owned subdir — that
            # populates the cache and `has_cache` becomes True, so we
            # don't even reach here. (For symlinks whose targets DON'T
            # resolve into owned subdirs anymore, the purge concern shifts
            # back to the data inside the subdirs, which is what the
            # subdir check above defends.) For tools / paths entirely
            # outside our historical tables no auto-detection is possible
            # and the user is responsible — same as pre-fix.
            profile_name = active[tool_id]
            profile_dir = self._store.profile_dir(profile_name)
            expected_subdirs = self._expected_subdirs_for(tool_id)
            existing_subdirs = sorted(
                sub for sub in expected_subdirs if (profile_dir / sub).is_dir()
            )
            # Same protection applied to ConfigFile snapshots under
            # ``.switcher/config_files/<subdir>/``. Pre-fix, the orphan-
            # force path only checked owned config_dir subtrees; an
            # orphan tool with a stranded snapshot (e.g., post-init
            # vanilla snapshot whose registry entry was later removed)
            # would still be silently purgeable by a later
            # ``uninstall --purge``. Treat snapshot subdirs as owned
            # state too (Hermes + CR pass-PR-4 blocker). Subdirs share
            # the same identifier between config_dirs and config_files
            # (Tool validator enforces ``cf.profile_subdir in
            # config_dirs subdirs``), so the historical-subdir union
            # already bounds both sides.
            config_file_root = profile_dir / ".switcher" / "config_files"
            existing_config_file_subdirs = sorted(
                sub for sub in expected_subdirs if (config_file_root / sub).is_dir()
            )
            if existing_subdirs or existing_config_file_subdirs:
                owned_locations: list[str] = []
                if existing_subdirs:
                    owned_locations.append(
                        f"config_dir subdir(s) {existing_subdirs} under {profile_name!r}"
                    )
                if existing_config_file_subdirs:
                    owned_locations.append(
                        f"ConfigFile snapshot subdir(s) "
                        f"{existing_config_file_subdirs} under "
                        f"{profile_name!r}/.switcher/config_files"
                    )
                raise UninstallPreflightError(
                    f"orphan tool {tool_id!r} still has profile data on disk "
                    f"({'; '.join(owned_locations)}). Refusing to drop from "
                    f"active map — that data would be silently lost on a "
                    f"later `switcher uninstall --purge`. Restore the "
                    f"registry TOML and re-run, or delete the owned data "
                    f"manually first."
                )
            skipped_orphan = True

        # Classify mappings; filter to just this tool.
        all_mappings = self._classify_uninstall_mappings()
        mappings = [m for m in all_mappings if m.tool_id == tool_id]

        # CORRUPT mappings always refuse, even with --force.
        # (Matches uninstall behavior at service.py's pre-flight step 5.)
        if not skipped_orphan:
            for m in mappings:
                if m.state == UninstallMappingState.CORRUPT:
                    raise UninstallPreflightError(
                        f"tool {tool_id!r} mapping {m.profile_subdir!r}: {m.corruption_reason}"
                    )

        if dry_run:
            return UnmanageReport(tool_id=tool_id, mappings=mappings, skipped_orphan=skipped_orphan)

        # Execute per-DirMapping unwind, unless skipping orphan.
        if not skipped_orphan:
            for m in mappings:
                self._execute_uninstall_mapping(m)

        # Atomic state mutation: drop tool_id from active + cache in a
        # single set_active_state call.
        new_active = {k: v for k, v in active.items() if k != tool_id}
        new_cache = {k: v for k, v in live_paths_cache.items() if k != tool_id}
        self._store.set_active_state(new_active, new_cache)

        return UnmanageReport(tool_id=tool_id, mappings=mappings, skipped_orphan=skipped_orphan)

    # Rescan -----------------------------------------------------------------

    @overload
    def rescan(
        self,
        *,
        only: list[str] | None = None,
        into: str | None = None,
        dry_run: bool = False,
        continue_: Literal[False] = False,
        abort: Literal[False] = False,
    ) -> RescanReport: ...

    @overload
    def rescan(
        self,
        *,
        only: list[str] | None = None,
        into: str | None = None,
        dry_run: bool = False,
        continue_: bool = False,
        abort: bool = False,
    ) -> RescanReport | RescanAlreadyCompletedReport | None: ...

    def rescan(
        self,
        *,
        only: list[str] | None = None,
        into: str | None = None,
        dry_run: bool = False,
        continue_: bool = False,
        abort: bool = False,
    ) -> RescanReport | RescanAlreadyCompletedReport | None:
        """Capture newly-installed tools (spec §4).

        v0.1.5: ``continue_`` / ``abort`` drive op-log compensation
        (spec §2.4). Mutually exclusive with each other AND with
        ``only`` / ``into`` / ``dry_run`` (recovery scope is taken from
        the in-flight journal record, not the call site). When either
        flag is set, the body reads the in-flight ``_RescanOp`` from
        the journal and dispatches to ``_compensate_rescan_continue``
        / ``_compensate_rescan_abort``.

        Normal path writes a ``_RescanOp`` intent BEFORE the capture
        loop and marks it completed after the loop finishes. A crash
        anywhere between leaves a record the next CLI command's
        detection hook surfaces as ``RescanInProgressError`` (mutating)
        / exit 3 (read-only); the user resolves via
        ``switcher rescan --continue`` / ``--abort``.

        Type-routing for the recovery dispatch (continue/abort with
        an in-flight op of a different kind):
          - ``_InitOp`` in flight → ``InitInProgressError`` (route the
            user to ``switcher init --continue/--abort``).
          - ``_RenameOp`` in flight → ``OpLogCorruptError``: the CLI
            detection hook auto-compensates rename on every command;
            reaching here means the hook never drained it (stale
            binary or hand-edited journal).
        """
        # Defense-in-depth mutex (CLI enforces the same invariant ahead
        # of get_deps; this guard catches direct callers — tests,
        # alternate front-ends — that bypass the CLI layer). Silently
        # falling through to ``continue_`` on the both-set case would
        # let a confused caller compensate-forward when they thought
        # they were aborting. ValueError matches init's symmetric guard.
        if continue_ and abort:
            raise ValueError("rescan: continue_ and abort are mutually exclusive")
        # Recovery scope comes from the in-flight journal record, NOT
        # the call site — combining recovery flags with only / into /
        # dry_run would silently ignore the call-site args.
        if (continue_ or abort) and (only is not None or into is not None or dry_run):
            raise ValueError(
                "rescan: continue_/abort cannot be combined with only, into, "
                "or dry_run — recovery scope is taken from the in-flight "
                "journal record, not the call site"
            )

        oplog = OpLogIO(self._store.state_dir())

        if continue_ or abort:
            in_flight = oplog.read_in_flight()
            if in_flight is None:
                raise NoInProgressRescanError(
                    "no interrupted rescan detected; nothing to continue/abort"
                )
            # Symmetric to init's routing: an in-flight _InitOp wants
            # init's recovery surface, not rescan's. Name the matching
            # command + flags so the user can self-correct.
            if isinstance(in_flight, _InitOp):
                raise InitInProgressError(
                    "an interrupted init is in flight; run "
                    "`switcher init --continue` to finish it or "
                    "`switcher init --abort` to reverse pre-init state."
                )
            # _RenameOp is auto-compensated by the CLI detection hook on
            # every other command; reaching here with one in flight means
            # the user invoked `switcher rescan --continue/--abort`
            # against a journal the hook never got to drain (stale
            # binary). Refuse loudly rather than run rescan compensation
            # against a rename's residue.
            if not isinstance(in_flight, _RescanOp):
                raise OpLogCorruptError(
                    f"unexpected in-flight op type {type(in_flight).__name__}; "
                    f"manual recovery required"
                )
            # Validate spec §2.4 --into invariants BEFORE the short-
            # circuit or compensation dispatch (CR pass-2 major). A
            # corrupt journal with multiple --into target profiles or a
            # missing previous_tools snapshot would otherwise drive
            # mutations across multiple profiles or silently bless a
            # missing snapshot.
            self._validate_rescan_record_invariants(in_flight)
            if self._check_rescan_already_completed(in_flight):
                # Spec §2.4 "committed but log-unmarked" — the original
                # rescan's work is fully visible on disk; mark the
                # record completed without running any per-mapping
                # mutation. Surface the short-circuit as a distinct
                # return shape so the CLI can disambiguate between
                # actual compensation (None) and journal-cleanup-only
                # (RescanAlreadyCompletedReport).
                oplog.mark_completed(in_flight)
                return RescanAlreadyCompletedReport(
                    target_profiles=dict(in_flight.target_profiles),
                    kind="continue" if continue_ else "abort",
                )
            if continue_:
                self._compensate_rescan_continue(in_flight)
            else:
                self._compensate_rescan_abort(in_flight)
            oplog.mark_completed(in_flight)
            return None

        self._require_initialized()
        active = self._store.get_active()

        # Detection: tool in registry, not in active, first config dir exists at live.
        candidates: list[Tool] = []
        for tool in self._registry:
            if tool.id in active:
                continue
            if not tool.config_dirs:
                continue
            first_live = self._resolver.tool_dir(tool, 0)
            if not self._resolver.exists(first_live):
                continue
            candidates.append(tool)

        if only is not None:
            if not only:
                raise RescanCaptureError("--only requires at least one tool id")
            allow = set(only)
            unknown = allow - {t.id for t in self._registry}
            if unknown:
                raise UnknownToolError(f"unknown tool(s): {sorted(unknown)}")
            already_managed = allow & set(active)
            if already_managed:
                raise RescanCaptureError(f"already managed: {sorted(already_managed)}")
            not_detected = allow - {t.id for t in candidates} - already_managed
            if not_detected:
                raise RescanCaptureError(f"not detected at expected path: {sorted(not_detected)}")
            candidates = [t for t in candidates if t.id in allow]

        if not candidates:
            return RescanReport(captured=[])

        # Pre-flight per discovered tool — AlreadyLinkedError vs
        # PathNotADirectoryError per spec §4.3. Missing secondary dirs are
        # ALLOWED (will be seeded by move_or_seed_dir at capture time).
        for tool in candidates:
            for i in range(len(tool.config_dirs)):
                live = self._resolver.tool_dir(tool, i)
                if self._resolver.is_link(live):
                    raise AlreadyLinkedError(f"{live} is already a link")
                if live.exists() and not live.is_dir():
                    raise PathNotADirectoryError(f"{live} exists but is not a directory")

        # Resolve target profile name(s).
        if into is not None:
            if not self._store.profile_dir(into).exists():
                raise UnknownProfileError(f"profile {into!r} not found")
            for tool in candidates:
                for dm in tool.config_dirs:
                    if (self._store.profile_dir(into) / dm.profile_subdir).exists():
                        raise RescanCaptureError(
                            f"profile {into!r} already has {dm.profile_subdir!r} (would overwrite)"
                        )
                # Same defensive rule for ConfigFile snapshots: a target
                # profile that already has a snapshot at the canonical
                # path is mid-state from a different switcher operation
                # (an older save / a partial uninstall) — overwriting it
                # would silently destroy the previously-captured MCP /
                # oauth subtree (spec §3.8).
                #
                # ``exists() or is_symlink()`` so a broken (dangling)
                # symlink at the snapshot path doesn't slip past the
                # check — Path.exists() returns False for a broken
                # symlink, but the classifier already treats any link
                # shape at the snapshot path as AMBIGUOUS corruption.
                # Refusing here keeps preflight consistent with the
                # compensation rules (abby r-batch4 nit).
                for cf in tool.config_files:
                    snap = self._store.config_file_snapshot_path(
                        into, cf.profile_subdir, cf.profile_filename
                    )
                    if snap.exists() or snap.is_symlink():
                        raise RescanCaptureError(
                            f"profile {into!r} already has a config_file "
                            f"snapshot at {snap} (would overwrite)"
                        )
            targets = {tool.id: into for tool in candidates}
        else:
            today = now().strftime("%Y-%m-%d")
            n = 1
            targets = {}
            for tool in candidates:
                while self._store.profile_dir(f"{today}-rescan-{n}").exists():
                    n += 1
                targets[tool.id] = f"{today}-rescan-{n}"
                n += 1

        if dry_run:
            return RescanReport(captured=[(t.id, targets[t.id]) for t in candidates])

        # v0.1.5: build the intent record BEFORE any FS mutation. mappings
        # carry per-(tool,index) live_path + profile_subdir + original_kind
        # so abort knows how to reverse safely. previous_tools snapshot
        # for --into mode captures pre-rescan metadata so abort can restore
        # it exactly; fresh-profile mode leaves previous_tools=None (abort
        # deletes the target profiles outright). The unique-profile loop
        # for previous_tools mirrors the spec §2.4 "target_profiles maps
        # every tool_id to the SAME profile in --into mode" invariant.
        rescan_mappings: list[_MappingIntent] = []
        for tool in candidates:
            for i, dm in enumerate(tool.config_dirs):
                live = self._resolver.tool_dir(tool, i)
                if live.is_dir() and not self._resolver.is_link(live):
                    original_kind = "real-dir"
                elif not live.exists():
                    original_kind = "missing"
                else:
                    # Pre-flight (a few dozen lines up) already rejected
                    # link / non-dir-file shapes; this is a defense-in-
                    # depth assertion mirroring init's pre-flight contract.
                    raise AssertionError(
                        f"unexpected pre-flight state for {live}: "
                        f"is_link={self._resolver.is_link(live)}, "
                        f"exists={live.exists()}, is_dir={live.is_dir()}"
                    )
                rescan_mappings.append(
                    _MappingIntent.model_validate(
                        {
                            "tool_id": tool.id,
                            "mapping_index": i,
                            "live_path": os.path.normpath(str(live)),
                            "profile_subdir": dm.profile_subdir,
                            "original_kind": original_kind,
                        }
                    )
                )
        previous_tools_snapshot: dict[str, dict[str, bool]] | None = None
        if into is not None:
            previous_tools_snapshot = {into: dict(self._store.get(into).tools)}
        # ConfigFile intents for rescan, parallel to the init branch.
        # Canonicalize live_path via expand() + normpath so the
        # AbsolutePath validator accepts the string and recovery sees
        # the same canonical form a fresh rescan would compute.
        rescan_config_file_mappings: list[_ConfigFileMappingIntent] = []
        for tool in candidates:
            for cf in tool.config_files:
                live_cf = self._resolver.expand(cf.windows_path if IS_WINDOWS else cf.posix_path)
                rescan_config_file_mappings.append(
                    _ConfigFileMappingIntent.model_validate(
                        {
                            "tool_id": tool.id,
                            "profile_subdir": cf.profile_subdir,
                            "profile_filename": cf.profile_filename,
                            "live_path": os.path.normpath(str(live_cf)),
                            "owned_json_paths": tuple(cf.owned_json_paths),
                        }
                    )
                )
        intent = _RescanOp.model_validate(
            {
                "op": "rescan",
                "started_at": now(),
                "target_ids": [t.id for t in candidates],
                "target_profiles": dict(targets),
                "into_mode": into is not None,
                "previous_tools": previous_tools_snapshot,
                "mappings": rescan_mappings,
                "config_file_mappings": rescan_config_file_mappings,
            }
        )
        oplog.append_record(intent)

        # Capture per tool, with rollback on partial failure. The per-tool
        # state write (active + cache, atomic per spec §4.4) lives INSIDE
        # the rollback try/except: a config-write failure after a successful
        # capture would otherwise leave live dirs symlinked into a profile
        # the persisted active map doesn't reference, causing retries to
        # hit AlreadyLinkedError instead of recovering cleanly.
        report = RescanReport(captured=[])
        live_paths_cache = self.get_active_live_paths()
        first_tool = True
        for tool in candidates:
            target = targets[tool.id]
            new_paths = [
                str(self._resolver.tool_dir(tool, i)) for i in range(len(tool.config_dirs))
            ]
            previous_tools: dict[str, bool] | None = None
            try:
                previous_tools = self._capture_tool_for_rescan(
                    tool,
                    target,
                    into=into is not None,
                    # Stamp the rescan's id into fresh-mode profiles so
                    # recovery can prove ownership and refuse races
                    # (Hermes pass-PR-2 blocker). --into mode targets
                    # pre-exist; their ownership comes from previous_tools.
                    journal_id=intent.rescan_id if into is None else None,
                )
                active[tool.id] = target
                live_paths_cache[tool.id] = new_paths
                self._store.set_active_state(active, live_paths_cache)
            except ProfileExistsError:
                # Pre-mutation cancel scope: ProfileExistsError fires
                # before _capture_tool_for_rescan touches the live dirs
                # (store.create's first line is the existence check).
                # Disk is untouched for this tool; if this is the FIRST
                # tool, no prior tool has mutated either. The intent
                # must be canceled or the next command forces "compensate
                # the rescan that never started". After the first tool
                # has set_active_state'd, the rescan is partially
                # committed — preserve the intent for compensation
                # (mirrors init's narrow cancel scope).
                if first_tool:
                    with contextlib.suppress(StorageError):
                        oplog.cancel_intent(intent)
                raise
            except Exception as e:
                # Revert in-memory dict mutations so a rollback after the
                # state-write step starts cleanly; the on-disk active map
                # is unaffected (set_active_state is the only writer).
                active.pop(tool.id, None)
                live_paths_cache.pop(tool.id, None)
                # The per-tool capture method already attempted in-loop rollback;
                # this catch is the outer safety net for the post-loop cleanup
                # (profile dir / metadata revert) that an in-loop rollback might
                # not cover. `previous_tools` is the snapshot from
                # `_capture_tool_for_rescan` — populated only if the capture
                # already committed the metadata update — so the rollback can
                # revert metadata.json on --into state-write failure.
                leftovers, rollback_errors = self._rollback_partial_rescan(
                    tool, target, into=into is not None, previous_tools=previous_tools
                )
                msg = f"capture failed for {tool.id!r}: {e}"
                if leftovers:
                    msg += (
                        f"; rollback could not restore captured data — "
                        f"user data left at: {', '.join(leftovers)}"
                    )
                if rollback_errors:
                    msg += f"; rollback steps also failed: {'; '.join(rollback_errors)}"
                raise RescanCaptureError(msg) from e
            report.captured.append((tool.id, target))
            first_tool = False

        # v0.1.5: entire multi-tool capture loop succeeded. mark_completed
        # ONLY after the last tool's set_active_state landed — running it
        # mid-loop would clear the recovery surface for a partially-
        # captured rescan (the spec's correctness invariant).
        oplog.mark_completed(intent)
        return report

    def _capture_tool_for_rescan(
        self,
        tool: Tool,
        target: str,
        *,
        into: bool,
        journal_id: str | None = None,
    ) -> dict[str, bool] | None:
        """Per-tool capture with metadata semantics from §4.4.

        For `--into` mode: defer the metadata update until AFTER the capture
        loop succeeds. The previous order (mutate metadata first, revert on
        failure) had a silent-inconsistency window — if the capture failed
        AND the suppress'd revert also failed (e.g. transient FS error), the
        on-disk metadata claimed the new tool was added even though no live
        capture happened. Deferring eliminates the failure window entirely.

        Returns the previous metadata.tools snapshot (for --into mode) so the
        caller can hand it to `_rollback_partial_rescan` if a later step
        (e.g. `set_active_state`) fails after this method already updated
        metadata.json. Default mode returns None because its rollback
        rmtree's the whole partial profile dir.

        ``journal_id`` (fresh-mode only): stamps the rescan's identifier
        into the created profile's metadata so recovery can refuse
        fresh-mode targets whose ``.tools`` match by coincidence but
        weren't actually created by this rescan (Hermes pass-PR-2
        blocker — closes the cross-profile data-loss race from a
        reservation-vs-capture name collision).
        """
        target_dir = self._store.profile_dir(target)
        previous_tools: dict[str, bool] | None = None
        updated_tools: dict[str, bool] | None = None  # for post-capture --into write

        if not into:
            self._store.create(target, {tool.id: True}, journal_id=journal_id)
        else:
            existing = self._store.get(target)
            previous_tools = dict(existing.tools)
            updated_tools = dict(previous_tools)
            updated_tools[tool.id] = True

        # Track per-mapping whether capture seeded an originally-missing live
        # path (mkdir'd an empty target) vs moved a real live dir into the
        # target. Rollback for "seeded" mappings must NOT recreate live, or
        # we corrupt the user's pre-rescan filesystem state (empty dir at a
        # path that was originally missing → next rescan's detection sees a
        # stale "tool installed" signal).
        completed: list[tuple[Path, Path, bool]] = []  # (live, tgt, was_seeded)
        try:
            for i, dm in enumerate(tool.config_dirs):
                live = self._resolver.tool_dir(tool, i)
                tgt = target_dir / dm.profile_subdir
                live_is_link = live.is_symlink() or (IS_WINDOWS and os.path.isjunction(live))
                was_seeded = not live.exists() and not live_is_link
                move_or_seed_dir(live, tgt)
                swap_link(tgt, live)
                completed.append((live, tgt, was_seeded))
        except Exception:
            # In-loop rollback: undo each completed mapping in reverse.
            # Metadata never mutated for --into (deferred), so no revert needed.
            for live, tgt, was_seeded in reversed(completed):
                if self._resolver.is_link(live):
                    with contextlib.suppress(Exception):
                        remove_link(live)
                if tgt.exists():
                    if was_seeded:
                        # Original state: live missing. Drop the empty seeded
                        # tgt; do NOT recreate live.
                        with contextlib.suppress(Exception):
                            shutil.rmtree(tgt)
                    else:
                        with contextlib.suppress(Exception):
                            move_or_seed_dir(tgt, live)
            raise

        # Mirror init's capture sequence: the rescan profile must own the
        # same data shape every later capture will produce, or the first
        # `switcher use <rescan-profile>` would hit snapshot-missing on
        # every ConfigFile-equipped tool — warn-and-skip at best, silent
        # apply of `{}` over user MCPs at worst (the snapshot-missing
        # branch in _plan_config_file_applies). No-op for tools without
        # config_files.
        #
        # Placement is post-dir-loop and post-metadata-update so a
        # snapshot-capture failure (StorageError on malformed live JSON,
        # symlink at live_path, etc.) doesn't strand a half-built
        # snapshot in the partial profile dir. The inner except cleans
        # the snapshot debris explicitly; the outer rescan() rollback
        # (_rollback_partial_rescan) handles the dir + metadata side.
        try:
            self._capture_config_files(target, tool)
        except Exception:
            # Unlink any partial snapshot debris. For fresh-mode the
            # outer rmtree of target_dir would catch this too, but
            # --into mode leaves target_dir alone — the dir-rollback
            # only touches per-tool subdirs, so an orphan snapshot
            # under .switcher/config_files/ would survive without
            # this explicit cleanup. Belt-and-suspenders for fresh
            # mode; load-bearing for --into.
            for cf in tool.config_files:
                self._store.config_file_snapshot_path(
                    target, cf.profile_subdir, cf.profile_filename
                ).unlink(missing_ok=True)
            raise

        # Capture succeeded — commit the metadata update for --into. If this
        # write fails after a successful capture, the live links exist but
        # metadata doesn't reflect them; that's a narrower window than the
        # original "mutate first, suppress revert" shape and is recoverable
        # by re-running rescan (which is idempotent on already-linked tools).
        if into and updated_tools is not None:
            self._store.update_profile_tools(target, updated_tools)
        return previous_tools

    def _rollback_partial_rescan(
        self,
        tool: Tool,
        target: str,
        *,
        into: bool,
        previous_tools: dict[str, bool] | None = None,
    ) -> tuple[list[str], list[str]]:
        """Outer-loop cleanup if `_capture_tool_for_rescan` raised.

        For default mode: rmtree the partial profile dir (it was created by
        store.create just before the capture loop and contains only this
        tool's mappings).

        For --into mode: metadata is now deferred until after the capture
        loop succeeds, so there's nothing to revert. This outer cleanup
        must restore captured live dirs (move sub back to live) before
        removing leftover per-tool subdirs — otherwise a swap_link failure
        between move_or_seed_dir and completed.append silently destroys
        user data.

        Fail-closed: if `move_or_seed_dir(sub, live)` raises, leave `sub` on
        disk and record its path. The rmtree of the partial profile dir (or
        of leftover subs in --into mode) only runs if every sub was either
        restored or never had real content.

        Returns `(leftovers, rollback_errors)`:
          - `leftovers`: sub paths that still hold user data and need manual
            recovery.
          - `rollback_errors`: human-readable descriptions of rollback steps
            that failed *without* leaving recoverable artefacts on disk
            (e.g. metadata.json revert failed → on-disk metadata still
            claims the tool was added even though state was never updated).

        Both lists are surfaced in the RescanCaptureError message so a
        suppressed exception never silently leaves the repo inconsistent.
        """
        target_dir = self._store.profile_dir(target)
        leftovers: list[str] = []
        rollback_errors: list[str] = []

        def _is_empty_dir(p: Path) -> bool:
            """An empty sub means the inner phase seeded it (live was missing)
            rather than moved real content into it. Restoring an empty seed
            back to live would mkdir at a path that was originally missing —
            corrupting detection on the next rescan run."""
            try:
                return not any(p.iterdir())
            except OSError:
                return False

        if not into:
            # Drop any leftover live symlinks (defensive — in-loop rollback
            # should have handled these already).
            for i, dm in enumerate(tool.config_dirs):
                live = self._resolver.tool_dir(tool, i)
                if self._resolver.is_link(live):
                    with contextlib.suppress(Exception):
                        remove_link(live)
                sub = target_dir / dm.profile_subdir
                if sub.is_dir() and not _is_empty_dir(sub):
                    try:
                        move_or_seed_dir(sub, live)
                    except Exception:
                        leftovers.append(str(sub))
            # Only wipe the partial profile dir if every sub is either
            # already restored to live or was an empty seed. Any leftover
            # with real user data must stay on disk.
            if not leftovers:
                shutil.rmtree(target_dir, ignore_errors=True)
            return leftovers, rollback_errors

        # --into mode: same link-aware restore as default mode for THIS
        # tool's subdirs; leave the rest of the existing profile alone.
        # If `_capture_tool_for_rescan` already committed the metadata
        # update for this tool (cache populated → `previous_tools` set)
        # and a LATER step failed (e.g. `set_active_state`), revert
        # `metadata.json` so it doesn't keep claiming the tool was added.
        # The filesystem restore below is the higher-priority guarantee,
        # so we still attempt it even if the metadata revert fails — but
        # the failure is surfaced via rollback_errors instead of silently
        # suppressed. CodeRabbit round 4: previously suppress'd here, so
        # metadata.json could continue claiming the tool was added after
        # a state-write failure without the caller learning about it.
        if previous_tools is not None:
            try:
                self._store.update_profile_tools(target, previous_tools)
            except Exception as meta_err:
                rollback_errors.append(
                    f"profile {target!r} metadata.json revert failed "
                    f"(still lists tool {tool.id!r}): {meta_err}"
                )

        # Unlink any ConfigFile snapshots written into the --into target
        # for this tool. The inner snapshot cleanup in
        # ``_capture_tool_for_rescan`` only fires when
        # ``_capture_config_files`` itself raises; if that succeeds and
        # the LATER ``update_profile_tools`` (or ``set_active_state`` in
        # the outer rescan loop) fails, the snapshot survives. For
        # --into mode, target_dir is NOT rmtree'd by this rollback —
        # the snapshot would leak under
        # ``<target>/.switcher/config_files/...`` and trip the
        # ``rescan --into`` collision pre-flight on the next retry
        # (Hermes pass-PR-2). Cleanup is link-aware so a corrupt
        # snapshot-path shape also gets removed.
        for cf in tool.config_files:
            snap_path = self._store.config_file_snapshot_path(
                target, cf.profile_subdir, cf.profile_filename
            )
            if snap_path.is_symlink() or snap_path.exists():
                with contextlib.suppress(Exception):
                    snap_path.unlink(missing_ok=True)
        for i, dm in enumerate(tool.config_dirs):
            live = self._resolver.tool_dir(tool, i)
            if self._resolver.is_link(live):
                with contextlib.suppress(Exception):
                    remove_link(live)
            sub = target_dir / dm.profile_subdir
            if not sub.is_dir():
                continue
            if live.exists() or self._resolver.is_link(live):
                # Live already restored (e.g. by in-loop rollback) —
                # just drop the leftover sub.
                shutil.rmtree(sub, ignore_errors=True)
                continue
            if _is_empty_dir(sub):
                # Seed leftover; live was originally missing. Don't recreate.
                shutil.rmtree(sub, ignore_errors=True)
                continue
            try:
                move_or_seed_dir(sub, live)
            except Exception:
                # Leave sub on disk with user data; surface for manual cleanup.
                leftovers.append(str(sub))
                continue
            if sub.exists():
                shutil.rmtree(sub, ignore_errors=True)
        return leftovers, rollback_errors

    # Prune ------------------------------------------------------------------

    def prune(self, *, force: bool = False, dry_run: bool = False) -> PruneReport:
        """Delete orphan profiles (spec §5). Calls ProfileService.delete for each."""
        self._require_initialized()
        # Surface filesystem failures (permission denied, transient ENOENT,
        # broken entries) as PruneError rather than leaking raw OSError —
        # mirrors the StorageError / UninstallPreflightError contract.
        try:
            orphans = self._compute_orphans()
            sizes = {name: self._profile_size_bytes(name) for name in orphans}
        except OSError as e:
            raise PruneError(f"prune failed during orphan walk: {e}") from e

        report = PruneReport(deleted=[], sizes_bytes=sizes)
        if not orphans:
            return report
        if dry_run:
            return report

        if not force:
            if not sys.stdin.isatty():
                raise PruneError("refusing to delete without --force in non-interactive mode")
            # Prompt is the CLI's responsibility; service trusts force=True if
            # CLI confirmed. Programmatic callers must confirm and pass
            # force=True themselves.
            raise PruneError(
                "prune requires force=True from caller; "
                "interactive confirmation is the CLI's responsibility"
            )

        for name in orphans:
            try:
                self.delete(name)  # service-layer guard for defense-in-depth
            except OSError as e:
                raise PruneError(f"prune failed deleting {name!r}: {e}") from e
            report.deleted.append(name)
        return report

    def _compute_orphans(self) -> list[str]:
        active_set = set(self._store.get_active().values())
        profiles_root = self._store.state_dir() / "profiles"
        if not profiles_root.is_dir():
            return []
        disk_set = {p.name for p in profiles_root.iterdir() if p.is_dir()}
        return sorted(disk_set - active_set)

    def _profile_size_bytes(self, name: str) -> int:
        """Sum every file size under <state_dir>/profiles/<name>/. KB-MB scale."""
        total = 0
        prof_dir = self._store.profile_dir(name)
        for root, _, files in os.walk(prof_dir):
            for f in files:
                total += (Path(root) / f).stat().st_size
        return total

    # Uninstall execution ---------------------------------------------------

    def _execute_uninstall_mapping(self, m: _UninstallMapping) -> None:
        """Per-DirMapping execution, branching on classified state.

        For SYMLINK: copy profile content to a sibling temp dir, then call
        restore_real_dir. Pre-flight already rejected the case where the
        sibling temp path collides with unrelated content; if we still see
        a temp here it's a race condition, so fail loud (do NOT rmtree).
        """
        if m.state == UninstallMappingState.SYMLINK:
            temp = _temp_dir_for_uninstall(m.live_path)
            if temp.exists():
                raise UninstallPreflightError(
                    f"sibling temp dir {temp} appeared during execution; refusing "
                    f"to overwrite. Inspect/remove {temp} manually and re-run."
                )
            # On copytree failure we'd otherwise leave a partial temp behind,
            # which the next retry's pre-flight collision check would refuse —
            # wedging recovery until manual cleanup. ignore_errors is fine
            # because the temp is wholly within our control (sibling of live)
            # and the original error is what we want to surface.
            #
            # symlinks=True preserves the user's original tree shape: any
            # symlink inside the managed config dir (file or directory) was
            # captured into the profile by `move_or_seed_dir` as-is, and
            # uninstall must restore it as-is. With symlinks=False, copytree
            # would dereference linked subdirs and silently change the live
            # tree shape; the resume classifier's `_dirs_match()` would then
            # see more entries in temp than in profile (because os.walk does
            # not recurse into linked dirs) and mark the mapping CORRUPT
            # instead of MISSING_LIVE_TEMP_PRESENT, breaking interrupted-
            # uninstall resume. Hermes review.
            try:
                shutil.copytree(m.profile_dir_subdir, temp, symlinks=True, dirs_exist_ok=False)
            except Exception:
                shutil.rmtree(temp, ignore_errors=True)
                raise
            restore_real_dir(temp, m.live_path)
            return
        if m.state == UninstallMappingState.MISSING_LIVE_TEMP_PRESENT:
            temp = _temp_dir_for_uninstall(m.live_path)
            # Re-check live_path before rename: classification was made from an
            # earlier read; if the live path reappeared (concurrent process,
            # interrupted retry), refuse to clobber it.
            if m.live_path.is_symlink() or m.live_path.exists():
                raise UninstallPreflightError(
                    f"live path {m.live_path} reappeared during execution; refusing to overwrite"
                )
            # Validate temp is a REAL directory before rename, mirroring
            # restore_real_dir's invariant. A concurrent process could have
            # replaced temp with a link/file between classification and
            # execution; renaming that into place would install a link in
            # the live position instead of restoring a real dir.
            temp_is_link = temp.is_symlink() or (IS_WINDOWS and os.path.isjunction(temp))
            if temp_is_link or not temp.is_dir():
                raise UninstallPreflightError(
                    f"temp dir {temp} is not a real directory at execution time; "
                    "refusing to rename into live position"
                )
            temp.rename(m.live_path)
            return
        if m.state == UninstallMappingState.ALREADY_RESTORED:
            # No-op; already done.
            return
        # CORRUPT shouldn't reach here — pre-flight rejected it.
        raise AssertionError(f"unreachable: {m.state}")


@dataclass(frozen=True)
class InitReport:
    """Return shape of ProfileService.init() — v0.1.4.

    profile_name: the dated-current profile created.
    captured: tool ids actually captured into the new profile.
    requested_but_not_installed: ids the user passed via --only that
        aren't installed locally (informational; not an error when SOME
        tools in the --only list were captured).
    skipped_via_skip_flag: ids excluded via --skip (informational).
    skipped_via_interactive: ids the user answered No to in
        --interactive mode (informational).
    """

    profile_name: str
    captured: list[str]
    requested_but_not_installed: list[str]
    skipped_via_skip_flag: list[str]
    skipped_via_interactive: list[str]


@dataclass(frozen=True)
class InitAlreadyCompletedReport:
    """Return shape of ``ProfileService.init(continue_=True | abort=True)``
    when the journal record was already fully completed (committed but
    log-unmarked) — spec §2.2 "committed but log-unmarked" path.

    Surfaces the short-circuit case as a distinct return value so
    the CLI can disambiguate between "compensation ran" (return None)
    and "the journal was just cleaned up; the underlying init was
    already on disk before the crash" (this report). Without this
    distinction, ``init --abort`` would be a silent no-op on a
    committed init — the user would think they reversed the init
    when in fact the init is still in place.

    profile_name: the dated-current profile the journal record was for.
    kind: which recovery flag triggered the short-circuit. CLI tailors
        the message; the abort variant points the user at
        ``switcher uninstall`` if they actually want to reverse the
        committed init.
    """

    profile_name: str
    kind: Literal["continue", "abort"]


@dataclass(frozen=True)
class RescanAlreadyCompletedReport:
    """Return shape of ``ProfileService.rescan(continue_=True | abort=True)``
    when the journal record was already fully completed — spec §2.4
    "committed but log-unmarked" path (symmetric to init's variant).

    Surfaces the short-circuit case as a distinct return value so the
    CLI can disambiguate between "compensation ran" (return None) and
    "the journal was just cleaned up; the underlying rescan was
    already on disk before the crash" (this report).

    target_profiles: the profile-name map from the journal record so
        the CLI can name what the now-completed rescan captured.
    kind: which recovery flag triggered the short-circuit.
    """

    target_profiles: dict[str, str]
    kind: Literal["continue", "abort"]


@dataclass
class UninstallReport:
    skipped: list[tuple[str, str]]
    mappings: list[_UninstallMapping]
    purged: bool = False


@dataclass(frozen=True)
class UnmanageReport:
    """Return shape of ProfileService.unmanage() — v0.1.4.

    tool_id: which tool was unmanaged.
    mappings: per-DirMapping classifications + execution outcomes.
        Uses the (still-private) _UninstallMapping dataclass — same
        shape as UninstallReport.mappings. CLI accesses only the
        attribute surface (state, live_path, profile_subdir).
    skipped_orphan: True when the tool was orphan-no-cache AND
        --force was passed; symlinks were left in place.
    """

    tool_id: str
    mappings: list[_UninstallMapping]
    skipped_orphan: bool = False


@dataclass
class RescanReport:
    captured: list[tuple[str, str]]  # (tool_id, target_profile)


@dataclass
class PruneReport:
    deleted: list[str]
    sizes_bytes: dict[str, int]


class _MigrationValidationError(Exception):
    """Internal: a strict-validation check failed during migration."""
