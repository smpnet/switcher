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
)
from switcher.links import move_or_seed_dir, swap_link
from switcher.models import Profile, Tool
from switcher.paths import PathResolver
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
