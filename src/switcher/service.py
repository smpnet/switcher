"""ProfileService — orchestrates resolver + links + store + registry into the
user-visible operations (init/use/save/create/which/rename/delete).

`now` is module-level so tests can monkeypatch.setattr it (or use freezegun)
without reaching inside the service.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from datetime import UTC, datetime

from switcher.errors import (
    AlreadyLinkedError,
    PathNotADirectoryError,
    ProfileExistsError,
    StateAlreadyInitializedError,
    StateNotInitializedError,
    ToolHasNoActiveProfileError,
    ToolNotInProfileError,
    UnknownToolError,
)
from switcher.links import move_or_seed_dir, swap_link
from switcher.models import Profile, Tool
from switcher.paths import PathResolver
from switcher.registry import find_tool
from switcher.store import ProfileStore


def now() -> datetime:
    return datetime.now(UTC)


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

    def _capture_tool(self, profile: str, tool: Tool) -> None:
        """Move every live dir for `tool` into `profile`, then link back."""
        for i, dm in enumerate(tool.config_dirs):
            live = self._resolver.tool_dir(tool, i)
            target = self._store.profile_dir(profile) / dm.profile_subdir
            move_or_seed_dir(live, target)
            swap_link(target, live)

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
        for tool in installed:
            self._capture_tool(current_name, tool)
        self._store.create("vanilla", {t.id: True for t in installed})
        for tool in installed:
            self._seed_credentials(current_name, "vanilla", tool)
        self._store.set_active({t.id: current_name for t in installed})
        return current_name

    def use(self, profile_name: str, only: list[str] | None = None) -> None:
        self._require_initialized()
        profile = self._store.get(profile_name)
        active = self._store.get_active()
        if only is not None:
            for tid in only:
                if tid not in profile.tools:
                    raise ToolNotInProfileError(
                        f"profile {profile_name!r} does not include {tid!r}"
                    )
            target_ids = list(only)
        else:
            target_ids = sorted(profile.tools.keys())
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
        self._store.set_active(active)

    def save(self, name: str) -> None:
        """Snapshot live config into a new profile.

        Only currently-installed tools are snapshotted. A tool that's in the
        active map but no longer installed live is intentionally skipped:
        save's contract is "snapshot live state", and a uninstalled-but-
        persisted-in-store flow doesn't fit that contract cleanly. If the
        user wants that data preserved, the active profile already holds it
        and `use(other_profile)` won't disturb it.
        """
        self._require_initialized()
        if self._store.profile_dir(name).exists():
            raise ProfileExistsError(f"profile {name!r} already exists")
        installed = self.detect_installed()
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
        self._require_initialized()
        if self._store.profile_dir(name).exists():
            raise ProfileExistsError(f"profile {name!r} already exists")
        installed = self.detect_installed()
        active = self._store.get_active()
        self._store.create(name, {t.id: True for t in installed})
        for tool in installed:
            src_profile = active.get(tool.id)
            if src_profile is None:
                # Just ensure layout exists; no source to seed from.
                for dm in tool.config_dirs:
                    (self._store.profile_dir(name) / dm.profile_subdir).mkdir(
                        parents=True, exist_ok=True
                    )
                continue
            self._seed_credentials(src_profile, name, tool)

    def which(self, tool_id: str) -> str:
        self._require_initialized()
        active = self._store.get_active()
        if tool_id not in active:
            raise ToolHasNoActiveProfileError(f"tool {tool_id!r} has no active profile")
        return active[tool_id]
