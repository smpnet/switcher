# pyright: reportPrivateUsage=none
"""Tests for ConfigFile capture during ProfileService.init().

The fixtures (`tmp_home`, `tmp_state`) come from tests/conftest.py. The local
`registry` / `service` fixtures replace the built-in claude tool with a
ConfigFile-equipped variant pointing at a tmp_home-relative ~/.claude.json,
matching the §3.3 reference shape used by the save and use suites.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from switcher.errors import StorageError
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
    """Built-in registry with claude augmented to carry the spec's ConfigFile."""
    base = build_registry(Path("/nonexistent"))
    out: list[Tool] = []
    for t in base:
        if t.id == "claude":
            out.append(t.model_copy(update={"config_files": (CLAUDE_CONFIG_FILE,)}))
        else:
            out.append(t)
    return tuple(out)


@pytest.fixture
def service(
    tmp_home: Path, tmp_state: Path, registry: tuple[Tool, ...]
) -> ProfileService:
    store = FileProfileStore(tmp_state)
    resolver = PathResolver(home=tmp_home)
    return ProfileService(store, resolver, registry)


def test_init_captures_live_config_file_to_default_profile(
    service: ProfileService, tmp_home: Path
) -> None:
    live = tmp_home / ".claude.json"
    live.write_text(
        json.dumps(
            {
                "mcpServers": {"installed": {"command": "x"}},
                "projects": {"/repo": {"mcpServers": {"p": {}}}},
                "oauthAccount": {"email": "user@x"},
                # hasCompletedOnboarding is machine-global, not in any owned
                # path, must be absent from the snapshot.
                "hasCompletedOnboarding": True,
            }
        )
    )

    service.init(["claude"])
    active = service._store.get_active()
    default_profile = active["claude"]
    snap_path = service._store.config_file_snapshot_path(
        default_profile, "claude", "claude.json"
    )
    snap = json.loads(snap_path.read_text())
    assert snap == {
        "mcpServers": {"installed": {"command": "x"}},
        "projects": {"/repo": {"mcpServers": {"p": {}}}},
        "oauthAccount": {"email": "user@x"},
    }


def test_init_handles_missing_live_config_file(
    service: ProfileService, tmp_home: Path
) -> None:
    """Fresh-machine init: ~/.claude.json does not exist yet → snapshot is {}.

    Refusing to capture here would block init on a freshly installed Claude
    before the user has launched it once (spec §3.6).
    """
    assert not (tmp_home / ".claude.json").exists()
    service.init(["claude"])
    active = service._store.get_active()
    default_profile = active["claude"]
    snap_path = service._store.config_file_snapshot_path(
        default_profile, "claude", "claude.json"
    )
    assert json.loads(snap_path.read_text()) == {}


def test_init_raises_on_malformed_live_config_file(
    service: ProfileService, tmp_home: Path
) -> None:
    live = tmp_home / ".claude.json"
    live.write_text("not valid json {")
    with pytest.raises(StorageError):
        service.init(["claude"])
