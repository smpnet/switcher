# pyright: reportPrivateUsage=none
"""Tests for init op-log compensation over config_file_mappings (spec §3.7).

Mirrors test_init_compensation.py for the snapshot side of init. The
in-flight ``_InitOp`` record is constructed directly so the tests can
probe each disk-state branch without depending on intent-write fault
injection. The local ``registry`` / ``service`` fixtures inject a
ConfigFile-equipped claude into the registry — same pattern as
test_service_config_file_save.py.
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
    _InitOp,
    _MappingIntent,
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
def service(
    tmp_home: Path, tmp_state: Path, registry: tuple[Tool, ...]
) -> ProfileService:
    store = FileProfileStore(tmp_state)
    resolver = PathResolver(home=tmp_home)
    return ProfileService(store, resolver, registry)


def _now() -> datetime:
    return datetime(2026, 5, 19, 10, 30, tzinfo=UTC)


def _claude_mapping(tmp_home: Path, original_kind: str = "real-dir") -> _MappingIntent:
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
        }
    )


def _make_init_record(
    profile_name: str,
    *,
    target_ids: list[str],
    mappings: list[_MappingIntent],
    config_file_mappings: list[_ConfigFileMappingIntent],
) -> _InitOp:
    return _InitOp.model_validate(
        {
            "op": "init",
            "started_at": _now(),
            "target_ids": target_ids,
            "profile_name": profile_name,
            "mappings": mappings,
            "config_file_mappings": config_file_mappings,
        }
    )


def _stage_completed_dir_mapping(
    tmp_home: Path, profile_dir: Path, original_content: str = '{"orig": true}'
) -> None:
    """Promote the claude dir mapping to MappingDiskState.COMPLETE so the
    continue dispatch isolates the ConfigFile branch under test.

    Concretely: move ``~/.claude/<files>`` into ``<profile_dir>/claude/``
    and replace the live path with a symlink — exactly what
    ``move_or_seed_dir + swap_link`` does during a clean init.
    """
    target = profile_dir / "claude"
    target.mkdir(parents=True, exist_ok=True)
    live = tmp_home / ".claude"
    (target / "settings.json").write_text(original_content)
    if live.is_dir() and not live.is_symlink():
        shutil.rmtree(live)
    live.symlink_to(target, target_is_directory=True)


def test_continue_recaptures_when_snapshot_untouched(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """A crash between intent-write and snapshot-write leaves the
    snapshot UNTOUCHED. --continue re-extracts from current live and
    atomic-writes it under the canonical snapshot path."""
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"x": {"command": "y"}}}))

    profile_name = "2026-05-19-current"
    store = FileProfileStore(tmp_state)
    store.create(profile_name, {"claude": True})
    profile_dir = store.profile_dir(profile_name)
    _stage_completed_dir_mapping(tmp_home, profile_dir)

    record = _make_init_record(
        profile_name,
        target_ids=["claude"],
        mappings=[_claude_mapping(tmp_home)],
        config_file_mappings=[_claude_cf_mapping(tmp_home)],
    )
    OpLogIO(tmp_state).append_record(record)

    service.init(continue_=True)

    snap = store.config_file_snapshot_path(profile_name, "claude", "claude.json")
    assert snap.exists()
    assert json.loads(snap.read_text()) == {"mcpServers": {"x": {"command": "y"}}}


def test_continue_noop_when_snapshot_complete(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """A crash AFTER the snapshot was written leaves it COMPLETE. The
    --continue path must NOT re-extract — would clobber the captured
    bytes with a drifted live read."""
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"x": {}}}))

    profile_name = "2026-05-19-current"
    store = FileProfileStore(tmp_state)
    store.create(profile_name, {"claude": True})
    profile_dir = store.profile_dir(profile_name)
    _stage_completed_dir_mapping(tmp_home, profile_dir)

    # Pre-populate the snapshot to COMPLETE shape with content distinct
    # from the live file, so a "no-op" verdict is detectable by
    # comparing the post-continue snapshot bytes to the staged ones.
    snap = store.config_file_snapshot_path(profile_name, "claude", "claude.json")
    snap.parent.mkdir(parents=True, exist_ok=True)
    staged = {"mcpServers": {"captured-pre-crash": {}}}
    snap.write_text(json.dumps(staged))

    # Live drifts after the staged snapshot was written.
    live.write_text(json.dumps({"mcpServers": {"drifted": {}}}))

    record = _make_init_record(
        profile_name,
        target_ids=["claude"],
        mappings=[_claude_mapping(tmp_home)],
        config_file_mappings=[_claude_cf_mapping(tmp_home)],
    )
    OpLogIO(tmp_state).append_record(record)

    service.init(continue_=True)

    # COMPLETE means we leave the staged bytes alone.
    assert json.loads(snap.read_text()) == staged


def test_continue_raises_oplog_corrupt_on_ambiguous_snapshot(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """A directory at the snapshot path (or any non-regular-file shape)
    is AMBIGUOUS. --continue must refuse rather than rmtree-then-rewrite
    over potentially-meaningful state."""
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({}))

    profile_name = "2026-05-19-current"
    store = FileProfileStore(tmp_state)
    store.create(profile_name, {"claude": True})
    profile_dir = store.profile_dir(profile_name)
    _stage_completed_dir_mapping(tmp_home, profile_dir)

    # Plant a directory at the snapshot path — classifier reads
    # AMBIGUOUS, continue dispatch must surface OpLogCorruptError.
    snap = store.config_file_snapshot_path(profile_name, "claude", "claude.json")
    snap.mkdir(parents=True)

    record = _make_init_record(
        profile_name,
        target_ids=["claude"],
        mappings=[_claude_mapping(tmp_home)],
        config_file_mappings=[_claude_cf_mapping(tmp_home)],
    )
    OpLogIO(tmp_state).append_record(record)

    with pytest.raises(OpLogCorruptError, match="ambiguous"):
        service.init(continue_=True)


def test_abort_unlinks_complete_snapshot(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """--abort removes a COMPLETE snapshot. Init never mutates live for
    ConfigFile (capture is read-only on live), so abort has nothing to
    restore on the live side."""
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"x": {}}}))
    live_before = live.read_text()

    profile_name = "2026-05-19-current"
    store = FileProfileStore(tmp_state)
    store.create(profile_name, {"claude": True})
    profile_dir = store.profile_dir(profile_name)
    _stage_completed_dir_mapping(tmp_home, profile_dir)

    # Write the snapshot so it's COMPLETE at abort time.
    snap = store.config_file_snapshot_path(profile_name, "claude", "claude.json")
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(json.dumps({"mcpServers": {"x": {}}}))

    record = _make_init_record(
        profile_name,
        target_ids=["claude"],
        mappings=[_claude_mapping(tmp_home)],
        config_file_mappings=[_claude_cf_mapping(tmp_home)],
    )
    OpLogIO(tmp_state).append_record(record)

    service.init(abort=True)

    # Profile dir is gone; the snapshot under it is gone with it. Live
    # untouched — init never wrote to live, so abort doesn't restore.
    assert not profile_dir.exists()
    assert live.read_text() == live_before


def test_continue_raises_corrupt_when_registry_drops_config_file(
    tmp_home: Path, tmp_state: Path
) -> None:
    """Spec §3.7 runtime registry check: if the tool's current
    ConfigFile registry list no longer carries the (profile_subdir,
    profile_filename) pair the journal references, --continue must
    refuse as OpLogCorruptError.

    Concretely simulates a registry edit between intent-write and
    recovery — e.g., the user removed the ``[[config_files]]`` block
    from a hand-edited claude.toml after a crash, or pinned a stale
    switcher binary whose registry predates the entry.
    """
    # Local service whose registry has NO config_files for claude —
    # the journal still references one (planted below), so the
    # runtime check must trip.
    base = build_registry(Path("/nonexistent"))  # no ConfigFile injection
    registry = tuple(base)
    store = FileProfileStore(tmp_state)
    resolver = PathResolver(home=tmp_home)
    service = ProfileService(store, resolver, registry)

    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {}}))

    profile_name = "2026-05-19-current"
    store.create(profile_name, {"claude": True})
    profile_dir = store.profile_dir(profile_name)
    _stage_completed_dir_mapping(tmp_home, profile_dir)

    record = _make_init_record(
        profile_name,
        target_ids=["claude"],
        mappings=[_claude_mapping(tmp_home)],
        config_file_mappings=[_claude_cf_mapping(tmp_home)],
    )
    OpLogIO(tmp_state).append_record(record)

    with pytest.raises(OpLogCorruptError, match="config_file"):
        service.init(continue_=True)
