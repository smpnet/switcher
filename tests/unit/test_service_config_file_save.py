# pyright: reportPrivateUsage=none
"""Tests for ConfigFile handling in ProfileService.save().

The fixtures (`tmp_home`, `tmp_state`) come from tests/conftest.py. The local
`registry` and `service` fixtures replace the built-in claude tool with a
ConfigFile-equipped variant pointing at a tmp_home-relative ~/.claude.json —
the spec's reference shape from §3.3 / §3.5.
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


def test_save_writes_owned_paths_to_snapshot(
    service: ProfileService, tmp_home: Path
) -> None:
    live = tmp_home / ".claude.json"
    live.write_text(
        json.dumps(
            {
                "mcpServers": {"shared": {"command": "x"}},
                "projects": {
                    "/repo": {
                        "mcpServers": {"proj": {"command": "y"}},
                        # lastSessionId is machine-global; not owned, so the
                        # snapshot must exclude it.
                        "lastSessionId": "ABC",
                    }
                },
                "oauthAccount": {"email": "user@example.com"},
                # hasCompletedOnboarding is machine-global; not in any owned
                # path, must be absent from the snapshot.
                "hasCompletedOnboarding": True,
            }
        )
    )
    service.init(["claude"])
    service.save("workA")

    snap_path = service._store.config_file_snapshot_path(
        "workA", "claude", "claude.json"
    )
    snap = json.loads(snap_path.read_text())
    assert snap == {
        "mcpServers": {"shared": {"command": "x"}},
        "projects": {"/repo": {"mcpServers": {"proj": {"command": "y"}}}},
        "oauthAccount": {"email": "user@example.com"},
    }


def test_save_handles_missing_live_file(
    service: ProfileService, tmp_home: Path
) -> None:
    """No ~/.claude.json on disk (fresh machine, pre-first-run) → store `{}`.

    Refusing to capture in this case would block init on a freshly installed
    Claude before the user has launched it once.
    """
    # tmp_home pre-seeds ~/.claude/, but not ~/.claude.json. Sanity-check.
    assert not (tmp_home / ".claude.json").exists()
    service.init(["claude"])
    service.save("workA")
    snap_path = service._store.config_file_snapshot_path(
        "workA", "claude", "claude.json"
    )
    assert json.loads(snap_path.read_text()) == {}


def test_save_raises_on_malformed_live_json(
    service: ProfileService, tmp_home: Path
) -> None:
    live = tmp_home / ".claude.json"
    live.write_text("not valid json {")
    service.init(["claude"])
    with pytest.raises(StorageError, match="malformed"):
        service.save("workA")


def test_save_raises_when_live_is_not_a_json_object(
    service: ProfileService, tmp_home: Path
) -> None:
    """A JSON array or scalar at the live path is not a valid Claude config."""
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps(["not", "an", "object"]))
    service.init(["claude"])
    with pytest.raises(StorageError, match="object"):
        service.save("workA")


def test_save_rollback_removes_partial_profile_on_write_failure(
    service: ProfileService,
    tmp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed snapshot write must not leave a half-built profile that
    blocks retry with ProfileExistsError."""
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"a": {}}}))
    service.init(["claude"])

    # service.py imports atomic_write_file via `from switcher.links import ...`,
    # so patching links.atomic_write_file doesn't reach the module-local
    # binding the helper actually calls. Patch the rebound symbol.
    from switcher import service as service_mod

    def boom(target: Path, content: bytes) -> None:
        raise OSError("simulated write failure")

    monkeypatch.setattr(service_mod, "atomic_write_file", boom)
    with pytest.raises(OSError, match="simulated write failure"):
        service.save("workA")
    assert not service._store.profile_dir("workA").exists()
