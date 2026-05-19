# pyright: reportPrivateUsage=none
"""Tests for _InitOp / _RescanOp config_file_mappings validators (spec §3.7).

Three invariants the validator enforces:
1. tool_id referential integrity — every entry's tool_id must be in target_ids.
2. Tuple uniqueness — (tool_id, profile_subdir, profile_filename) is unique.
3. SafeName / AbsolutePath shape — corrupt hand-edits surface as ValidationError.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from switcher.oplog import (
    _ConfigFileMappingIntent,
    _InitOp,
    _MappingIntent,
    _RescanOp,
)

IS_WINDOWS = sys.platform == "win32"


def _abs(posix: str, windows: str) -> str:
    """Return a host-absolute path string the AbsolutePath validator accepts.

    The validator rejects POSIX-shaped paths on Windows (and vice versa) —
    hard-coding ``/Users/test/...`` made the validator fixtures fail on
    Windows CI (CR pass batch-4). Pick the platform's canonical form.
    """
    return windows if IS_WINDOWS else posix


def _now() -> datetime:
    return datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)


def _claude_dir_mapping() -> _MappingIntent:
    return _MappingIntent(
        tool_id="claude",
        mapping_index=0,
        live_path=_abs("/Users/test/.claude", "C:\\Users\\test\\.claude"),
        profile_subdir="claude",
        original_kind="real-dir",
    )


def _cf_mapping(
    tool_id: str = "claude",
    profile_subdir: str = "claude",
    profile_filename: str = "claude.json",
    live_path: str | None = None,
    owned_json_paths: tuple[str, ...] = (".mcpServers",),
) -> _ConfigFileMappingIntent:
    return _ConfigFileMappingIntent(
        tool_id=tool_id,
        profile_subdir=profile_subdir,
        profile_filename=profile_filename,
        live_path=live_path or _abs("/Users/test/.claude.json", "C:\\Users\\test\\.claude.json"),
        owned_json_paths=owned_json_paths,
    )


def _init_op(**overrides: object) -> _InitOp:
    base: dict[str, object] = {
        "op": "init",
        "started_at": _now(),
        "target_ids": ["claude"],
        "profile_name": "default",
        "mappings": [_claude_dir_mapping()],
    }
    base.update(overrides)
    return _InitOp.model_validate(base)


def _rescan_op(**overrides: object) -> _RescanOp:
    base: dict[str, object] = {
        "op": "rescan",
        "started_at": _now(),
        "target_ids": ["claude"],
        "target_profiles": {"claude": "rescan-1"},
        "into_mode": False,
        "previous_tools": None,
        "mappings": [_claude_dir_mapping()],
    }
    base.update(overrides)
    return _RescanOp.model_validate(base)


def test_init_op_accepts_config_file_mappings() -> None:
    op = _init_op(config_file_mappings=[_cf_mapping()])
    assert len(op.config_file_mappings) == 1
    assert op.config_file_mappings[0].tool_id == "claude"


def test_init_op_defaults_config_file_mappings_to_empty() -> None:
    """Existing journals predate this feature; new field MUST default to
    ``[]`` so prior records continue to parse without amendment."""
    op = _init_op()
    assert op.config_file_mappings == []


def test_init_op_rejects_cf_mapping_for_unknown_tool() -> None:
    with pytest.raises(ValidationError, match="tool_id"):
        _init_op(config_file_mappings=[_cf_mapping(tool_id="nonexistent")])


def test_init_op_rejects_duplicate_cf_tuple() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        _init_op(
            config_file_mappings=[
                _cf_mapping(),
                _cf_mapping(),  # duplicate storage path
            ]
        )


def test_init_op_rejects_two_tools_writing_same_snapshot_path() -> None:
    """Two different tool_ids that would write to the same storage
    path under the init profile must be refused. ``tool_id`` is
    intentionally not part of the uniqueness key because two distinct
    tool_ids targeting the same snapshot file is precisely the
    corruption shape the validator exists to catch (abby r-batch4).
    """
    with pytest.raises(ValidationError, match=r"duplicate.*storage path"):
        _init_op(
            target_ids=["claude", "other"],
            mappings=[
                _claude_dir_mapping(),
                _MappingIntent(
                    tool_id="other",
                    mapping_index=0,
                    live_path=_abs("/Users/test/.other", "C:\\Users\\test\\.other"),
                    profile_subdir="other",
                    original_kind="real-dir",
                ),
            ],
            config_file_mappings=[
                _cf_mapping(tool_id="claude"),
                _cf_mapping(tool_id="other"),  # same (subdir, filename) under same init profile
            ],
        )


def test_rescan_op_accepts_config_file_mappings() -> None:
    op = _rescan_op(config_file_mappings=[_cf_mapping()])
    assert len(op.config_file_mappings) == 1


def test_rescan_op_defaults_config_file_mappings_to_empty() -> None:
    op = _rescan_op()
    assert op.config_file_mappings == []


def test_rescan_op_rejects_cf_mapping_for_unknown_tool() -> None:
    with pytest.raises(ValidationError, match="tool_id"):
        _rescan_op(config_file_mappings=[_cf_mapping(tool_id="nonexistent")])


def test_rescan_op_rejects_duplicate_cf_tuple() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        _rescan_op(
            config_file_mappings=[
                _cf_mapping(),
                _cf_mapping(),
            ]
        )


def test_rescan_op_allows_same_subdir_when_target_profiles_differ() -> None:
    """Fresh-mode rescan with two tools landing in DIFFERENT target
    profiles must accept identical (profile_subdir, profile_filename)
    pairs — the storage paths differ because each tool's target
    profile differs. abby r-batch4: uniqueness must be measured on
    the actual storage path, not the (subdir, filename) tuple alone.

    The check uses ``target_profile_for_tool`` so this case
    legitimately resolves to two distinct storage paths.
    """
    op = _RescanOp.model_validate(
        {
            "op": "rescan",
            "started_at": _now(),
            "target_ids": ["claude", "other"],
            "target_profiles": {
                "claude": "claude-rescan",
                "other": "other-rescan",
            },
            "into_mode": False,
            "previous_tools": None,
            "mappings": [
                _claude_dir_mapping(),
                _MappingIntent(
                    tool_id="other",
                    mapping_index=0,
                    live_path=_abs("/Users/test/.other", "C:\\Users\\test\\.other"),
                    profile_subdir="other-dir",
                    original_kind="real-dir",
                ),
            ],
            "config_file_mappings": [
                _cf_mapping(tool_id="claude"),
                # Same (subdir, filename) but lands in a different
                # target profile — DIFFERENT storage path.
                _cf_mapping(tool_id="other"),
            ],
        }
    )
    assert len(op.config_file_mappings) == 2


def test_rescan_op_rejects_two_tools_writing_same_snapshot_under_into_target() -> None:
    """--into rescan with two tools both pointing at the same
    (subdir, filename) under the SAME --into target = collision.
    """
    with pytest.raises(ValidationError, match=r"duplicate.*storage path"):
        _RescanOp.model_validate(
            {
                "op": "rescan",
                "started_at": _now(),
                "target_ids": ["claude", "other"],
                "target_profiles": {
                    "claude": "shared-into",
                    "other": "shared-into",
                },
                "into_mode": True,
                "previous_tools": {"shared-into": {"existing": True}},
                "mappings": [
                    _claude_dir_mapping(),
                    _MappingIntent(
                        tool_id="other",
                        mapping_index=0,
                        live_path=_abs("/Users/test/.other", "C:\\Users\\test\\.other"),
                        profile_subdir="other-dir",
                        original_kind="real-dir",
                    ),
                ],
                "config_file_mappings": [
                    _cf_mapping(tool_id="claude"),
                    _cf_mapping(tool_id="other"),
                ],
            }
        )


def test_cf_mapping_rejects_unsafe_subdir() -> None:
    """SafeName on profile_subdir guards against journal path-traversal."""
    with pytest.raises(ValidationError):
        _ConfigFileMappingIntent(
            tool_id="claude",
            profile_subdir="../escape",
            profile_filename="claude.json",
            live_path=_abs("/Users/test/.claude.json", "C:\\Users\\test\\.claude.json"),
            owned_json_paths=(".mcpServers",),
        )


def test_cf_mapping_rejects_unsafe_filename() -> None:
    with pytest.raises(ValidationError):
        _ConfigFileMappingIntent(
            tool_id="claude",
            profile_subdir="claude",
            profile_filename="../escape.json",
            live_path=_abs("/Users/test/.claude.json", "C:\\Users\\test\\.claude.json"),
            owned_json_paths=(".mcpServers",),
        )


def test_cf_mapping_rejects_relative_live_path() -> None:
    """AbsolutePath rejects relative paths so corrupt journals can't
    direct compensation at attacker-relative targets."""
    with pytest.raises(ValidationError):
        _ConfigFileMappingIntent(
            tool_id="claude",
            profile_subdir="claude",
            profile_filename="claude.json",
            live_path="relative/path.json",
            owned_json_paths=(".mcpServers",),
        )


def test_cf_mapping_requires_owned_json_paths() -> None:
    """owned_json_paths is the journaled walker contract — recovery
    must use the original op's intended paths, not whatever the
    current registry says. Missing the field is corruption.
    """
    with pytest.raises(ValidationError, match="owned_json_paths"):
        _ConfigFileMappingIntent.model_validate(
            {
                "tool_id": "claude",
                "profile_subdir": "claude",
                "profile_filename": "claude.json",
                "live_path": _abs("/Users/test/.claude.json", "C:\\Users\\test\\.claude.json"),
            }
        )


def test_cf_mapping_preserves_owned_json_paths_across_roundtrip() -> None:
    """The journaled walker contract must roundtrip exactly so
    compensation extracts the same shape on replay (abby r-batch4)."""
    entry = _ConfigFileMappingIntent(
        tool_id="claude",
        profile_subdir="claude",
        profile_filename="claude.json",
        live_path=_abs("/Users/test/.claude.json", "C:\\Users\\test\\.claude.json"),
        owned_json_paths=(".mcpServers", ".projects[].mcpServers", ".oauthAccount"),
    )
    dumped = entry.model_dump()
    restored = _ConfigFileMappingIntent.model_validate(dumped)
    assert restored.owned_json_paths == (
        ".mcpServers",
        ".projects[].mcpServers",
        ".oauthAccount",
    )


@pytest.mark.parametrize(
    "bad_path",
    [
        "mcpServers",  # missing leading '.'
        ".projects[]",  # leaf '[]' rejected by v1 grammar
        ".",  # empty after the dot
        "[]",  # bare iter token without leading '.'
    ],
)
def test_cf_mapping_rejects_invalid_owned_json_paths_at_parse_time(bad_path: str) -> None:
    """CR pass-PR-2 major: ``owned_json_paths`` must parse against the
    owned-path grammar at journal-parse time. Without parse-time
    validation a hand-edited journal carrying out-of-grammar tokens
    would only fail later at compensation time, defeating the
    fail-fast corruption boundary the rest of this module is designed
    around.
    """
    with pytest.raises(ValidationError, match="owned_json_paths"):
        _ConfigFileMappingIntent.model_validate(
            {
                "tool_id": "claude",
                "profile_subdir": "claude",
                "profile_filename": "claude.json",
                "live_path": _abs("/Users/test/.claude.json", "C:\\Users\\test\\.claude.json"),
                "owned_json_paths": (bad_path,),
            }
        )
