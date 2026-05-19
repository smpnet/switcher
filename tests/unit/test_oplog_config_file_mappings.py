# pyright: reportPrivateUsage=none
"""Tests for _InitOp / _RescanOp config_file_mappings validators (spec §3.7).

Three invariants the validator enforces:
1. tool_id referential integrity — every entry's tool_id must be in target_ids.
2. Tuple uniqueness — (tool_id, profile_subdir, profile_filename) is unique.
3. SafeName / AbsolutePath shape — corrupt hand-edits surface as ValidationError.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from switcher.oplog import (
    _ConfigFileMappingIntent,
    _InitOp,
    _MappingIntent,
    _RescanOp,
)


def _now() -> datetime:
    return datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)


def _claude_dir_mapping() -> _MappingIntent:
    return _MappingIntent(
        tool_id="claude",
        mapping_index=0,
        live_path="/Users/test/.claude",
        profile_subdir="claude",
        original_kind="real-dir",
    )


def _cf_mapping(
    tool_id: str = "claude",
    profile_subdir: str = "claude",
    profile_filename: str = "claude.json",
    live_path: str = "/Users/test/.claude.json",
) -> _ConfigFileMappingIntent:
    return _ConfigFileMappingIntent(
        tool_id=tool_id,
        profile_subdir=profile_subdir,
        profile_filename=profile_filename,
        live_path=live_path,
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
                _cf_mapping(),  # duplicate (tool_id, subdir, filename)
            ]
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


def test_cf_mapping_rejects_unsafe_subdir() -> None:
    """SafeName on profile_subdir guards against journal path-traversal."""
    with pytest.raises(ValidationError):
        _ConfigFileMappingIntent(
            tool_id="claude",
            profile_subdir="../escape",
            profile_filename="claude.json",
            live_path="/Users/test/.claude.json",
        )


def test_cf_mapping_rejects_unsafe_filename() -> None:
    with pytest.raises(ValidationError):
        _ConfigFileMappingIntent(
            tool_id="claude",
            profile_subdir="claude",
            profile_filename="../escape.json",
            live_path="/Users/test/.claude.json",
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
        )
