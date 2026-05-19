# pyright: reportPrivateUsage=none
"""Tests for rescan op-log compensation over config_file_mappings (spec §3.7).

Mirrors test_init_config_file_compensation.py for the rescan side.
Rescan adds one bifurcation: fresh-profile vs --into. Compensation runs
the same per-ConfigFile dispatch for both modes; the test set covers
fresh-mode (which is what the production rescan flow lands first) plus
a representative --into case for the abort path.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from switcher.errors import OpLogCorruptError
from switcher.models import ConfigFile, Tool
from switcher.oplog import (
    OpLogIO,
    _ConfigFileMappingIntent,
    _MappingIntent,
    _RescanOp,
)
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
    owned_json_paths=(".mcpServers", ".projects[].mcpServers", ".oauthAccount"),
)

# Stable rescan_id for tests that pre-stage fresh-mode target profiles
# (which need profile.journal_id == record.rescan_id per the Hermes
# pass-PR-2 ownership check). 32 hex chars matches uuid4().hex.
_TEST_RESCAN_ID = "test-rescan-id-0123456789abcdef0123456789abcdef"


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


def _now() -> datetime:
    return datetime(2026, 5, 19, 10, 30, tzinfo=UTC)


def _claude_dir_mapping(tmp_home: Path, original_kind: str = "real-dir") -> _MappingIntent:
    return _MappingIntent.model_validate(
        {
            "tool_id": "claude",
            "mapping_index": 0,
            "live_path": str(tmp_home / ".claude"),
            "profile_subdir": "claude",
            "original_kind": original_kind,
        }
    )


def _claude_cf_mapping(tmp_home: Path) -> _ConfigFileMappingIntent:
    return _ConfigFileMappingIntent.model_validate(
        {
            "tool_id": "claude",
            "profile_subdir": "claude",
            "profile_filename": "claude.json",
            "live_path": str(tmp_home / ".claude.json"),
            "owned_json_paths": (
                ".mcpServers",
                ".projects[].mcpServers",
                ".oauthAccount",
            ),
        }
    )


def _make_rescan_record(
    *,
    target_profile: str = "rescan-profile",
    into_mode: bool = False,
    previous_tools: dict[str, dict[str, bool]] | None = None,
    rescan_id: str = _TEST_RESCAN_ID,
    mappings: list[_MappingIntent] | None = None,
    config_file_mappings: list[_ConfigFileMappingIntent] | None = None,
) -> _RescanOp:
    return _RescanOp.model_validate(
        {
            "op": "rescan",
            "started_at": _now(),
            "target_ids": ["claude"],
            "target_profiles": {"claude": target_profile},
            "into_mode": into_mode,
            "previous_tools": previous_tools,
            "mappings": mappings if mappings is not None else [],
            "config_file_mappings": (
                config_file_mappings if config_file_mappings is not None else []
            ),
            "rescan_id": rescan_id,
        }
    )


def _stage_completed_rescan_dir_mapping(
    tmp_home: Path,
    profile_dir: Path,
    *,
    original_content: str = '{"orig": true}',
) -> None:
    target = profile_dir / "claude"
    target.mkdir(parents=True, exist_ok=True)
    (target / "settings.json").write_text(original_content)
    live = tmp_home / ".claude"
    if live.is_dir() and not live.is_symlink():
        shutil.rmtree(live)
    live.symlink_to(target, target_is_directory=True)


def _prepare_init_state(service: ProfileService, tmp_home: Path) -> None:
    """The rescan compensation entry point pre-flights via
    ``_require_initialized``. Drop claude beforehand so init manages
    only copilot, then claude is a clean rescan target."""
    shutil.rmtree(tmp_home / ".claude", ignore_errors=True)
    service.init()


def test_continue_recaptures_when_snapshot_untouched(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Rescan-side mirror of the init test: crash between
    intent-write and snapshot-write leaves the snapshot UNTOUCHED;
    --continue re-extracts from live."""
    _prepare_init_state(service, tmp_home)

    # Recreate claude live + drop the JSON.
    (tmp_home / ".claude").mkdir()
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"recovered": {}}}))

    profile_name = "rescan-profile"
    store = FileProfileStore(tmp_state)
    store.create(
        profile_name,
        {"claude": True},
        journal_id=_TEST_RESCAN_ID,
    )
    profile_dir = store.profile_dir(profile_name)
    _stage_completed_rescan_dir_mapping(tmp_home, profile_dir)

    record = _make_rescan_record(
        target_profile=profile_name,
        into_mode=False,
        previous_tools=None,
        mappings=[_claude_dir_mapping(tmp_home)],
        config_file_mappings=[_claude_cf_mapping(tmp_home)],
    )
    OpLogIO(tmp_state).append_record(record)

    service.rescan(continue_=True)

    snap = store.config_file_snapshot_path(profile_name, "claude", "claude.json")
    assert snap.exists()
    assert json.loads(snap.read_text()) == {"mcpServers": {"recovered": {}}}


def test_continue_noop_when_snapshot_complete(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    _prepare_init_state(service, tmp_home)

    (tmp_home / ".claude").mkdir()
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"drifted": {}}}))

    profile_name = "rescan-profile"
    store = FileProfileStore(tmp_state)
    store.create(
        profile_name,
        {"claude": True},
        journal_id=_TEST_RESCAN_ID,
    )
    profile_dir = store.profile_dir(profile_name)
    _stage_completed_rescan_dir_mapping(tmp_home, profile_dir)

    snap = store.config_file_snapshot_path(profile_name, "claude", "claude.json")
    snap.parent.mkdir(parents=True, exist_ok=True)
    staged = {"mcpServers": {"captured-pre-crash": {}}}
    snap.write_text(json.dumps(staged))

    record = _make_rescan_record(
        target_profile=profile_name,
        into_mode=False,
        previous_tools=None,
        mappings=[_claude_dir_mapping(tmp_home)],
        config_file_mappings=[_claude_cf_mapping(tmp_home)],
    )
    OpLogIO(tmp_state).append_record(record)

    service.rescan(continue_=True)

    # COMPLETE means continue leaves the staged bytes untouched.
    assert json.loads(snap.read_text()) == staged


def test_continue_raises_oplog_corrupt_on_ambiguous_snapshot(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    _prepare_init_state(service, tmp_home)

    (tmp_home / ".claude").mkdir()
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({}))

    profile_name = "rescan-profile"
    store = FileProfileStore(tmp_state)
    store.create(
        profile_name,
        {"claude": True},
        journal_id=_TEST_RESCAN_ID,
    )
    profile_dir = store.profile_dir(profile_name)
    _stage_completed_rescan_dir_mapping(tmp_home, profile_dir)

    # AMBIGUOUS via directory at the snapshot path.
    snap = store.config_file_snapshot_path(profile_name, "claude", "claude.json")
    snap.mkdir(parents=True)

    record = _make_rescan_record(
        target_profile=profile_name,
        into_mode=False,
        previous_tools=None,
        mappings=[_claude_dir_mapping(tmp_home)],
        config_file_mappings=[_claude_cf_mapping(tmp_home)],
    )
    OpLogIO(tmp_state).append_record(record)

    with pytest.raises(OpLogCorruptError, match="ambiguous"):
        service.rescan(continue_=True)


def test_abort_unlinks_complete_snapshot_fresh_mode(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    _prepare_init_state(service, tmp_home)

    (tmp_home / ".claude").mkdir()
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"untouched-by-abort": {}}}))
    live_before = live.read_text()

    profile_name = "rescan-profile"
    store = FileProfileStore(tmp_state)
    store.create(
        profile_name,
        {"claude": True},
        journal_id=_TEST_RESCAN_ID,
    )
    profile_dir = store.profile_dir(profile_name)
    _stage_completed_rescan_dir_mapping(tmp_home, profile_dir)
    snap = store.config_file_snapshot_path(profile_name, "claude", "claude.json")
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(json.dumps({"mcpServers": {"x": {}}}))

    record = _make_rescan_record(
        target_profile=profile_name,
        into_mode=False,
        previous_tools=None,
        mappings=[_claude_dir_mapping(tmp_home)],
        config_file_mappings=[_claude_cf_mapping(tmp_home)],
    )
    OpLogIO(tmp_state).append_record(record)

    service.rescan(abort=True)

    # Fresh-mode abort drops the whole profile; snapshot goes with it.
    # Live read-only by rescan capture, so abort leaves it alone.
    assert not profile_dir.exists()
    assert live.read_text() == live_before
