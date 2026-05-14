"""ProfileService — orchestrates resolver + links + store + registry into the
user-visible operations (init/use/save/create/which/rename/delete).

`now` is module-level so tests can monkeypatch.setattr it (or use freezegun)
without reaching inside the service.
"""

from __future__ import annotations

import contextlib
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
    NoInProgressInitError,
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
from switcher.links import move_or_seed_dir, remove_link, restore_real_dir, swap_link
from switcher.models import Profile, Tool

# Op-log record classes are namespace-private to oplog.py (the underscore marks
# them as internal-to-switcher, not a user-facing surface). Service is the
# legitimate cross-module consumer that builds and dispatches them; the
# per-line suppression keeps the convention without leaking module-wide.
from switcher.oplog import (
    MappingDiskState,
    OpLogIO,
    _InitOp,  # pyright: ignore[reportPrivateUsage]
    _MappingIntent,  # pyright: ignore[reportPrivateUsage]
    _RenameOp,  # pyright: ignore[reportPrivateUsage]
    _RescanOp,  # pyright: ignore[reportPrivateUsage]
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
    # still get a non-None InitReport. The compensation branches (which
    # return None) require an explicit continue_/abort=True at the call
    # site — overload narrowing keeps existing tests like
    # `service.init().profile_name` typing cleanly without per-site
    # `assert is not None` noise.
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
    ) -> InitReport | None: ...

    def init(
        self,
        target_ids: Sequence[str] | None = None,
        *,
        requested_but_not_installed: Sequence[str] = (),
        skipped_via_skip_flag: Sequence[str] = (),
        skipped_via_interactive: Sequence[str] = (),
        continue_: bool = False,
        abort: bool = False,
    ) -> InitReport | None:
        """v0.1.4: target_ids filters which detected tools to capture.

        **Return-shape note (v0.1.5):** the default path returns an
        ``InitReport`` as before. The recovery path
        (``continue_=True`` / ``abort=True``) returns ``None`` because
        no fresh init happened — the call drove ``_compensate_init_*``
        against an already-in-flight journal record. The ``@overload``
        above keeps the default-args call site typed as
        ``-> InitReport``; direct callers that pass either flag get
        ``-> InitReport | None`` and must handle the recovery branch
        explicitly.

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
            if isinstance(in_flight, _RescanOp):
                raise RescanInProgressError(
                    "an interrupted rescan is in flight; run "
                    "`switcher rescan --continue` or `switcher rescan --abort`"
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
                oplog.mark_completed(in_flight)
                return None
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
        intent = _InitOp.model_validate(
            {
                "op": "init",
                "started_at": now(),
                "target_ids": [t.id for t in installed],
                "profile_name": current_name,
                "mappings": mappings,
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
        self._store.create("vanilla", {t.id: True for t in installed})
        for tool in installed:
            self._seed_credentials(current_name, "vanilla", tool)
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
        """
        for name in (record.profile_name, "vanilla"):
            p = self._store.profile_dir(name)
            if self._resolver.is_link(p) or not p.is_dir():
                return False
        profile_dir = self._store.profile_dir(record.profile_name)
        for intent in record.mappings:
            if classify_mapping(intent, profile_dir) is not MappingDiskState.COMPLETE:
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
        """
        profile_dir = self._store.profile_dir(record.profile_name)
        vanilla_dir = self._store.profile_dir("vanilla")

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

        # Step 6: vanilla profile + credential seeding. Shape of
        # vanilla_dir was already validated in the first-pass scan; a
        # symlink / file at profiles/vanilla raised OpLogCorruptError
        # before any mutation ran. Idempotent on a vanilla profile
        # that already exists (e.g. init crashed AFTER vanilla
        # creation but BEFORE set_active_state).
        if not vanilla_dir.is_dir():
            self._store.create("vanilla", dict.fromkeys(record.target_ids, True))
        for tid in record.target_ids:
            tool = find_tool(self._registry, tid)
            if tool is None:
                continue
            self._seed_credentials(record.profile_name, "vanilla", tool)

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
                elif intent.original_kind == "missing":
                    shutil.rmtree(target)

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
                if not target.is_dir():
                    raise PathNotADirectoryError(
                        f"profile {profile_name!r} is missing "
                        f"{dm.profile_subdir!r} for tool {tid!r}"
                    )
        for tid, tool in resolved:
            for i, dm in enumerate(tool.config_dirs):
                target = self._store.profile_dir(profile_name) / dm.profile_subdir
                live = self._resolver.tool_dir(tool, i)
                swap_link(target, live)
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
            if existing_subdirs:
                raise UninstallPreflightError(
                    f"orphan tool {tool_id!r} still has profile data on disk "
                    f"(subdir(s) {existing_subdirs} under {profile_name!r}). "
                    f"Refusing to drop from active map — that data would be "
                    f"silently lost on a later `switcher uninstall --purge`. "
                    f"Restore the registry TOML and re-run, or delete the "
                    f"subdir(s) manually first."
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

    def rescan(
        self,
        *,
        only: list[str] | None = None,
        into: str | None = None,
        dry_run: bool = False,
    ) -> RescanReport:
        """Capture newly-installed tools (spec §4)."""
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

        # Capture per tool, with rollback on partial failure. The per-tool
        # state write (active + cache, atomic per spec §4.4) lives INSIDE
        # the rollback try/except: a config-write failure after a successful
        # capture would otherwise leave live dirs symlinked into a profile
        # the persisted active map doesn't reference, causing retries to
        # hit AlreadyLinkedError instead of recovering cleanly.
        report = RescanReport(captured=[])
        live_paths_cache = self.get_active_live_paths()
        for tool in candidates:
            target = targets[tool.id]
            new_paths = [
                str(self._resolver.tool_dir(tool, i)) for i in range(len(tool.config_dirs))
            ]
            previous_tools: dict[str, bool] | None = None
            try:
                previous_tools = self._capture_tool_for_rescan(tool, target, into=into is not None)
                active[tool.id] = target
                live_paths_cache[tool.id] = new_paths
                self._store.set_active_state(active, live_paths_cache)
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
        return report

    def _capture_tool_for_rescan(
        self, tool: Tool, target: str, *, into: bool
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
        """
        target_dir = self._store.profile_dir(target)
        previous_tools: dict[str, bool] | None = None
        updated_tools: dict[str, bool] | None = None  # for post-capture --into write

        if not into:
            self._store.create(target, {tool.id: True})
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
