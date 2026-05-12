# pyright: reportPrivateUsage=none
"""Tests for op-log Pydantic models (spec §2.1)."""

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from switcher.oplog import (
    _InitOp,
    _MappingIntent,
    _RenameOp,
    _RescanOp,
    parse_record,
)


def _now() -> datetime:
    return datetime(2026, 5, 12, 10, 30, tzinfo=UTC)


def test_mapping_intent_round_trip():
    intent = _MappingIntent(
        tool_id="claude",
        mapping_index=0,
        live_path="/home/u/.claude",
        profile_subdir="claude",
        original_kind="real-dir",
    )
    blob = intent.model_dump_json()
    restored = _MappingIntent.model_validate_json(blob)
    assert restored == intent


def test_init_op_round_trip():
    op = _InitOp(
        op="init",
        started_at=_now(),
        target_ids=["claude", "copilot"],
        profile_name="2026-05-12-current",
        mappings=[
            _MappingIntent(
                tool_id="claude",
                mapping_index=0,
                live_path="/home/u/.claude",
                profile_subdir="claude",
                original_kind="real-dir",
            ),
        ],
    )
    blob = op.model_dump_json()
    parsed = json.loads(blob)
    assert parsed["op"] == "init"
    assert parsed["completed_at"] is None
    restored = parse_record(parsed)
    assert isinstance(restored, _InitOp)
    assert restored.target_ids == ["claude", "copilot"]


def test_rename_op_from_keyword_alias_round_trip():
    # Construct via the JSON-side alias `from`. populate_by_name=True is
    # what lets pydantic accept the python-side `from_` at runtime too,
    # but basedpyright's generated __init__ signature uses the alias.
    op = _RenameOp.model_validate(
        {
            "op": "rename",
            "started_at": _now(),
            "from": "experiment",
            "to": "client-A",
            "affected_ids": ["claude"],
        }
    )
    assert op.from_ == "experiment"
    blob = op.model_dump_json(by_alias=True)
    parsed = json.loads(blob)
    assert parsed["from"] == "experiment"
    assert parsed["to"] == "client-A"
    restored = parse_record(parsed)
    assert isinstance(restored, _RenameOp)
    assert restored.from_ == "experiment"


def test_rescan_op_round_trip_fresh_mode():
    op = _RescanOp(
        op="rescan",
        started_at=_now(),
        target_ids=["claude"],
        target_profiles={"claude": "2026-05-12-rescan-1"},
        into_mode=False,
        previous_tools=None,
        mappings=[],
    )
    blob = op.model_dump_json()
    restored = parse_record(json.loads(blob))
    assert isinstance(restored, _RescanOp)
    assert restored.target_profiles == {"claude": "2026-05-12-rescan-1"}


def test_rescan_op_into_mode_carries_previous_tools():
    op = _RescanOp(
        op="rescan",
        started_at=_now(),
        target_ids=["copilot"],
        target_profiles={"copilot": "shared"},
        into_mode=True,
        previous_tools={"shared": {"claude": True}},
        mappings=[],
    )
    blob = op.model_dump_json()
    restored = parse_record(json.loads(blob))
    assert isinstance(restored, _RescanOp)
    assert restored.previous_tools == {"shared": {"claude": True}}


def test_discriminator_picks_correct_subclass():
    for op_name, expected_cls in [("init", _InitOp), ("rename", _RenameOp), ("rescan", _RescanOp)]:
        sample: dict[str, Any] = {
            "op": op_name,
            "started_at": "2026-05-12T10:30:00+00:00",
        }
        if op_name == "init":
            sample.update(target_ids=[], profile_name="x", mappings=[])
        elif op_name == "rename":
            sample.update({"from": "a", "to": "b", "affected_ids": []})
        else:
            sample.update(
                target_ids=[],
                target_profiles={},
                into_mode=False,
                previous_tools=None,
                mappings=[],
            )
        record = parse_record(sample)
        assert isinstance(record, expected_cls)


def test_unknown_op_value_raises_validation_error():
    with pytest.raises(ValidationError):
        parse_record({"op": "vacuum", "started_at": "2026-05-12T10:30:00+00:00"})


def test_invalid_original_kind_rejected():
    with pytest.raises(ValidationError):
        _MappingIntent(
            tool_id="claude",
            mapping_index=0,
            live_path="/home/u/.claude",
            profile_subdir="claude",
            original_kind="hardlink",  # pyright: ignore[reportArgumentType]
        )
