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
    StateAlreadyInitializedError,
    StateNotInitializedError,
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
        # Pre-flight: refuse if any first dir is already a link.
        for tool in installed:
            for i in range(len(tool.config_dirs)):
                live = self._resolver.tool_dir(tool, i)
                if self._resolver.is_link(live):
                    raise AlreadyLinkedError(f"{live} is already a link; refusing to initialize")
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
        for tid in target_ids:
            tool = find_tool(self._registry, tid)
            if tool is None:
                raise UnknownToolError(f"unknown tool {tid!r}")
            for i, dm in enumerate(tool.config_dirs):
                target = self._store.profile_dir(profile_name) / dm.profile_subdir
                live = self._resolver.tool_dir(tool, i)
                swap_link(target, live)
            active[tid] = profile_name
        self._store.set_active(active)
