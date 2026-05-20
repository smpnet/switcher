# pyright: reportPrivateUsage=none
"""Tests for ConfigFile capture during ProfileService.init().

The fixtures (`tmp_home`, `tmp_state`) come from tests/conftest.py. The local
`registry` / `service` fixtures replace the built-in claude tool with a
ConfigFile-equipped variant pointing at a tmp_home-relative ~/.claude.json,
matching the §3.3 reference shape used by the save and use suites.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from switcher.errors import StorageError
from switcher.models import ConfigFile, Tool
from switcher.paths import PathResolver
from switcher.registry import build_registry
from switcher.service import ProfileService
from switcher.store import FileProfileStore

IS_WINDOWS = sys.platform == "win32"

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
def service(tmp_home: Path, tmp_state: Path, registry: tuple[Tool, ...]) -> ProfileService:
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
    snap_path = service._store.config_file_snapshot_path(default_profile, "claude", "claude.json")
    snap = json.loads(snap_path.read_text())
    assert snap == {
        "mcpServers": {"installed": {"command": "x"}},
        "projects": {"/repo": {"mcpServers": {"p": {}}}},
        "oauthAccount": {"email": "user@x"},
    }


def test_init_handles_missing_live_config_file(service: ProfileService, tmp_home: Path) -> None:
    """Fresh-machine init: ~/.claude.json does not exist yet → snapshot is {}.

    Refusing to capture here would block init on a freshly installed Claude
    before the user has launched it once (spec §3.6).
    """
    assert not (tmp_home / ".claude.json").exists()
    service.init(["claude"])
    active = service._store.get_active()
    default_profile = active["claude"]
    snap_path = service._store.config_file_snapshot_path(default_profile, "claude", "claude.json")
    assert json.loads(snap_path.read_text()) == {}


def test_init_raises_on_malformed_live_config_file(service: ProfileService, tmp_home: Path) -> None:
    live = tmp_home / ".claude.json"
    live.write_text("not valid json {")
    with pytest.raises(StorageError):
        service.init(["claude"])


@pytest.mark.skipif(IS_WINDOWS, reason="symlinks require elevation on Windows")
def test_init_preflight_aborts_before_mutation_on_cf_symlink(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Hermes pass-PR-7 13:04 blocker: a deterministic ConfigFile
    validation failure must abort BEFORE ``_store.create()`` and
    ``_capture_tool()`` mutate disk + journal. Pre-fix init swapped
    ``~/.claude`` to a managed symlink, persisted the dated profile,
    appended an in-flight oplog record, AND THEN raised from
    ``_capture_config_files`` — same validation-after-mutation half-
    applied class the ``use()`` preflight already closed in abby r4.
    A symlinked ``~/.claude.json`` is the canonical repro: the
    detection layer (Hermes pass-PR-7 #2) reports claude installed
    via the JSON-file signal, so init proceeds far enough to hit the
    CF validator.
    """
    live = tmp_home / ".claude.json"
    live.symlink_to(tmp_home / ".does-not-exist.json")

    claude_dir = tmp_home / ".claude"
    is_dir_before = claude_dir.is_dir() and not claude_dir.is_symlink()

    with pytest.raises(StorageError, match="symlink"):
        service.init(["claude"])

    # Pre-mutation refusal: ~/.claude is unchanged, no profiles
    # persisted, no oplog records left in flight.
    assert claude_dir.is_dir() == is_dir_before
    assert not claude_dir.is_symlink()
    assert service._store.list() == []
    profiles_dir = tmp_state / "profiles"
    assert not profiles_dir.exists() or list(profiles_dir.iterdir()) == []
    oplog_path = tmp_state / "oplog.json"
    if oplog_path.exists():
        records = json.loads(oplog_path.read_text())
        assert records.get("records", []) == []


def test_init_preflight_aborts_before_mutation_on_cf_malformed_json(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Same half-applied class as the symlink case, but via the
    malformed-JSON branch of the preflight. Pre-fix this raised from
    ``_capture_config_files`` after the dir mappings had already been
    captured + swap_link'd.
    """
    live = tmp_home / ".claude.json"
    live.write_text("not valid json {")

    claude_dir = tmp_home / ".claude"
    is_dir_before = claude_dir.is_dir() and not claude_dir.is_symlink()

    with pytest.raises(StorageError, match="malformed JSON"):
        service.init(["claude"])

    assert claude_dir.is_dir() == is_dir_before
    assert not claude_dir.is_symlink()
    assert service._store.list() == []
    profiles_dir = tmp_state / "profiles"
    assert not profiles_dir.exists() or list(profiles_dir.iterdir()) == []
    oplog_path = tmp_state / "oplog.json"
    if oplog_path.exists():
        records = json.loads(oplog_path.read_text())
        assert records.get("records", []) == []


def test_init_preflight_aborts_before_mutation_on_cf_non_object(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Live JSON parses as a list, not an object. Preflight must
    refuse before any dir mapping mutates.
    """
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps([1, 2, 3]))

    claude_dir = tmp_home / ".claude"

    with pytest.raises(StorageError, match="expected JSON object"):
        service.init(["claude"])

    assert not claude_dir.is_symlink()
    assert service._store.list() == []
