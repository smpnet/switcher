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

from switcher.errors import (
    AlreadyLinkedError,
    NoToolsManagedError,
    PathNotADirectoryError,
    ProfileExistsError,
    ProfileIsActiveError,
    PruneError,
    RescanCaptureError,
    StateAlreadyInitializedError,
    StateNotInitializedError,
    ToolHasNoActiveProfileError,
    ToolNotInProfileError,
    ToolNotManagedError,
    UninstallPreflightError,
    UnknownProfileError,
    UnknownToolError,
)
from switcher.links import move_or_seed_dir, remove_link, restore_real_dir, swap_link
from switcher.models import Profile, Tool
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
# derivation can't recover live paths whose parent isn't mentioned by the
# post-rewrite registry (e.g., ~/.config/github-copilot after the v0.1.4
# Copilot builtin rewrite).
_LEGACY_PARENT_DIRS_POSIX: tuple[str, ...] = (
    "~",
    "~/.config",
)
_LEGACY_PARENT_DIRS_WINDOWS: tuple[str, ...] = (
    "%USERPROFILE%",
    "%LOCALAPPDATA%",
)

# Per-tool historical profile_subdir names: subdir names a given tool's
# profile dir may legitimately contain across switcher versions. Union of
# (current registry) + (historical) gives the expected-subdir set for
# FS-truth discovery. Sourced from git history of src/switcher/builtins/*.toml.
_HISTORICAL_PROFILE_SUBDIRS: dict[str, frozenset[str]] = {
    "copilot": frozenset({"copilot-config", "copilot-auth"}),
    # claude has always used "claude" — no historical drift to record.
}

# Per-tool historical live-path basenames: the leaf names a tool's live path
# may have used across switcher versions. Union of (current registry) +
# (historical) gives the canonical-basename set discovery requires before
# capturing a symlink — without this, any user-created symlink resolving
# into an owned profile subdir (e.g., `~/copilot-backup`) would be falsely
# classified as managed and later mutated by uninstall.
_HISTORICAL_LIVE_PATH_BASENAMES: dict[str, frozenset[str]] = {
    "copilot": frozenset({"github-copilot"}),
    # claude has always lived at ~/.claude (or %USERPROFILE%\.claude).
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

    def _candidate_live_basenames_for(self, tool_id: str) -> frozenset[str]:
        """Live-path leaf names this tool may legitimately own on disk.

        Union of (a) basenames of the tool's current registry config_dirs
        and (b) hardcoded historical basenames from prior switcher versions.
        Used by FS-truth discovery to reject user-created symlinks that
        happen to resolve into an owned profile subdir but were never
        managed by switcher (e.g., a `~/copilot-backup` alias).
        """
        tool = find_tool(self._registry, tool_id)
        current: frozenset[str] = (
            frozenset(self._resolver.tool_dir(tool, i).name for i in range(len(tool.config_dirs)))
            if tool is not None
            else frozenset()
        )
        historical = _HISTORICAL_LIVE_PATH_BASENAMES.get(tool_id, frozenset())
        return current | historical

    def _candidate_parents_for(self, tool_id: str) -> list[Path]:
        """Parent directories to scan during FS-truth migration discovery.

        Union of (a) parents of the tool's current registry config_dirs and
        (b) hardcoded legacy parent dirs. The legacy set rescues paths the
        post-rewrite registry no longer mentions (e.g., ~/.config/github-
        copilot after the v0.1.4 Copilot single-dir rewrite).
        """
        parents: set[Path] = set()
        tool = find_tool(self._registry, tool_id)
        if tool is not None:
            for i in range(len(tool.config_dirs)):
                parents.add(self._resolver.tool_dir(tool, i).parent)
        legacy_raw = _LEGACY_PARENT_DIRS_WINDOWS if IS_WINDOWS else _LEGACY_PARENT_DIRS_POSIX
        for raw in legacy_raw:
            # Expand env vars on Windows (%USERPROFILE%, %LOCALAPPDATA%),
            # then expand ~ against the resolver's configured home so
            # tests with injected home dirs work correctly.
            parents.add(self._resolver.expand(raw))
        return sorted(parents)

    def _discover_live_paths_for_active(self, tool_id: str, profile_name: str) -> list[str]:
        """Walk the FS for symlinks resolving into this tool's owned subdirs.

        Trusts the filesystem over the registry. Used during legacy migration
        when active_live_paths is unpopulated (spec §4.2). The per-tool subdir
        map prevents misattribution in multi-tool profiles.

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

        # Hermes round-1: capture must be gated on canonical leaf names as
        # well as resolved target. Otherwise an unrelated `~/copilot-backup
        # -> <profile>/copilot-config` alias gets recorded and later mutated
        # by uninstall — a destructive false positive.
        expected_basenames = self._candidate_live_basenames_for(tool_id)
        if not expected_basenames:
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
        for parent in self._candidate_parents_for(tool_id):
            if not parent.is_dir():
                continue
            for child in parent.iterdir():
                if child.name not in expected_basenames:
                    continue
                if not self._resolver.is_link(child):
                    continue
                try:
                    resolved = child.resolve()
                except (OSError, RuntimeError):
                    # Broken/dangling/cyclic symlink — skip silently.
                    continue
                if resolved in owned_targets:
                    discovered.append(child)
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
                        # drifted. Falls through to CORRUPT for orphan resume
                        # OR registry-drifted resume on this specific mapping.
                        matched_subdir: str | None = None
                        if tool is not None:
                            for i, dm in enumerate(tool.config_dirs):
                                if self._resolver.tool_dir(tool, i) == live:
                                    matched_subdir = dm.profile_subdir
                                    break
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
            if not profile_target.exists():
                return (
                    UninstallMappingState.CORRUPT,
                    f"link target profile dir {profile_target} missing",
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

    def init(self) -> str:
        if self._store.list():
            raise StateAlreadyInitializedError("switcher is already initialized")
        installed = self.detect_installed()
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
        self._store.create(current_name, {t.id: True for t in installed})
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
        return current_name

    def use(self, profile_name: str, only: list[str] | None = None) -> None:
        self._require_initialized()
        # v0.1.4 §3.1/§3.5: empty active map → loud failure rather than
        # a silent no-op switch.
        self._require_managed()
        profile = self._store.get(profile_name)
        active = self._store.get_active()
        managed = set(active.keys())
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
            target_ids = list(only)
        else:
            # v0.1.4 §3.1 durability: the default switches only the
            # managed subset of the profile, not every tool in
            # profile.tools. Without this filter, use(other_profile)
            # re-activates a tool the user just unmanaged.
            target_ids = sorted(profile.tools.keys() & managed)
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

        The narrow failure window between steps 1 and 2 (rename succeeds,
        set_active fails) leaves the active map pointing at ``old`` while
        the profile lives at ``new``. That requires a manual state.json
        edit until tracked-ops land in v0.2.0 — same constraint as
        init() multi-step failures.
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
        # mutate" discipline as use() / save() / init(). True transactional
        # rollback on transient swap_link failures is v0.2.0 (tracked-ops).
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
        self._store.rename(old, new)
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
        # that round-trips the existing on-disk active_live_paths cache, so
        # an already-populated cache survives. The migration flush comes
        # AFTER swap_link below — at this point the symlinks still point at
        # `<old>` (Path.resolve() returns the stored target verbatim, even
        # when it no longer exists), so the strict validator can't yet
        # produce a usable cache entry.
        self._store.set_active(active)
        for tid in affected_ids:
            tool = find_tool(self._registry, tid)
            if tool is None:
                continue
            for i, dm in enumerate(tool.config_dirs):
                target = self._store.profile_dir(new) / dm.profile_subdir
                live = self._resolver.tool_dir(tool, i)
                swap_link(target, live)
        # Post-relink migration flush: now that every affected symlink points
        # into <new>, _derive_cache_for_active can validate them against the
        # post-rename active map. This is a second atomic write — safe
        # because the canonical state (store dir + active map) is already
        # consistent; the cache is a derived index, not load-bearing for
        # recovery (recovery path is `use(<new>)` regardless).
        self._store.set_active_live_paths(self._derive_cache_for_active(active))

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


@dataclass
class UninstallReport:
    skipped: list[tuple[str, str]]
    mappings: list[_UninstallMapping]
    purged: bool = False


@dataclass
class RescanReport:
    captured: list[tuple[str, str]]  # (tool_id, target_profile)


@dataclass
class PruneReport:
    deleted: list[str]
    sizes_bytes: dict[str, int]


class _MigrationValidationError(Exception):
    """Internal: a strict-validation check failed during migration."""
