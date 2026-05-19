# pyright: reportPrivateUsage=none
"""Tests for ConfigFile seeding during ProfileService.create().

The fixtures (`tmp_home`, `tmp_state`) come from tests/conftest.py. The
ConfigFile-equipped registry follows the same shape as the save/use/init
suites (spec §3.3).

create() copies the source profile's ConfigFile snapshot into the new
profile (state-store data copy, not a live capture). Without it, the
first `switcher use <new>` would hit snapshot-missing for every
ConfigFile-equipped tool and warn-and-skip — defeating the point of
creating the profile.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from switcher.models import ConfigFile, Tool
from switcher.paths import PathResolver
from switcher.registry import build_registry
from switcher.service import ProfileService
from switcher.store import FileProfileStore

CLAUDE_CONFIG_FILE = ConfigFile(
    posix_path="~/.claude.json",
    windows_path="%USERPROFILE%\\.claude.json",
    profile_subdir="claude",
    profile_filename="claude.json",
    merge_strategy="json_subtree_merge",
    owned_json_paths=(
        ".mcpServers",
        ".projects[].mcpServers",
        ".oauthAccount",
    ),
)


@pytest.fixture
def registry() -> tuple[Tool, ...]:
    base = build_registry(Path("/nonexistent"))
    out: list[Tool] = []
    for t in base:
        if t.id == "claude":
            out.append(t.model_copy(update={"config_files": (CLAUDE_CONFIG_FILE,)}))
        else:
            out.append(t)
    return tuple(out)


@pytest.fixture
def service(tmp_home: Path, tmp_state: Path, registry: tuple[Tool, ...]) -> ProfileService:
    store = FileProfileStore(tmp_state)
    resolver = PathResolver(home=tmp_home)
    return ProfileService(store, resolver, registry)


def test_create_seeds_config_file_snapshot_from_active_source(
    service: ProfileService, tmp_home: Path
) -> None:
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"shared": {"command": "x"}}}))
    service.init(["claude"])
    service.save("profA")  # profA's snapshot has the MCP

    service.create("profB")

    src_snap = service._store.config_file_snapshot_path("profA", "claude", "claude.json")
    dst_snap = service._store.config_file_snapshot_path("profB", "claude", "claude.json")
    assert dst_snap.exists()
    assert dst_snap.read_text() == src_snap.read_text()


def test_create_silently_skips_when_source_snapshot_missing(
    service: ProfileService, tmp_home: Path
) -> None:
    """Source profile pre-dates this feature: silently skip. rescan repairs.

    create() seeds from the active map's source, not from any save()-target
    name (save doesn't set active). Simulate a legacy profile by deleting
    the snapshot of whichever profile is currently active for claude.
    """
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {}}))
    service.init(["claude"])
    src_profile = service._store.get_active()["claude"]

    # Simulate pre-feature profile by deleting its snapshot.
    src_snap = service._store.config_file_snapshot_path(src_profile, "claude", "claude.json")
    src_snap.unlink()

    service.create("profB")  # must not raise
    dst_snap = service._store.config_file_snapshot_path("profB", "claude", "claude.json")
    assert not dst_snap.exists()


def test_create_rollback_removes_partial_profile_on_seed_failure(
    service: ProfileService,
    tmp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"a": {}}}))
    service.init(["claude"])
    service.save("profA")

    real_copy2 = shutil.copy2

    def failing_copy(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
        # Fail only when copying the ConfigFile snapshot. Credential copies
        # for the same profile pass through real_copy2 so we exercise the
        # rollback specifically at the new _seed_config_files step.
        if str(src).endswith("claude.json"):
            raise OSError("simulated seed failure")
        return real_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(shutil, "copy2", failing_copy)
    with pytest.raises(OSError, match="simulated seed failure"):
        service.create("profB")
    assert not service._store.profile_dir("profB").exists()
