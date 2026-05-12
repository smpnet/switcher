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
    dump_records,
    parse_record,
    parse_records,
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


def test_unknown_field_in_init_op_rejected():
    """Schema drift in oplog.json must surface as ValidationError so
    OpLogIO can map it to OpLogCorruptError. Silently discarding the
    unknown field and proceeding on partial data would defeat the
    journal's correctness role.
    """
    with pytest.raises(ValidationError):
        parse_record(
            {
                "op": "init",
                "started_at": "2026-05-12T10:30:00+00:00",
                "target_ids": ["claude"],
                "profile_name": "x",
                "mappings": [],
                "rogue_field": "drift",
            }
        )


def test_unknown_field_in_mapping_intent_rejected():
    with pytest.raises(ValidationError):
        _MappingIntent.model_validate(
            {
                "tool_id": "claude",
                "mapping_index": 0,
                "live_path": "/home/u/.claude",
                "profile_subdir": "claude",
                "original_kind": "real-dir",
                "rogue_field": "drift",
            }
        )


def test_naive_started_at_rejected_for_every_record_type():
    """A recovery journal must use timezone-aware timestamps so that a
    record serialized on one host can't be misordered against aware
    datetimes elsewhere in the codebase after a round trip. Pydantic's
    AwareDatetime enforces this at validation time on both started_at
    and completed_at across all three op subclasses.
    """
    naive = datetime(2026, 5, 12, 10, 30)  # intentionally naive for the test

    for op_name, extra in [
        ("init", {"target_ids": [], "profile_name": "x", "mappings": []}),
        ("rename", {"from": "a", "to": "b", "affected_ids": []}),
        (
            "rescan",
            {
                "target_ids": [],
                "target_profiles": {},
                "into_mode": False,
                "previous_tools": None,
                "mappings": [],
            },
        ),
    ]:
        payload: dict[str, Any] = {
            "op": op_name,
            "started_at": naive.isoformat(),  # naive ISO string, no offset
            **extra,
        }
        with pytest.raises(ValidationError):
            parse_record(payload)


def test_naive_completed_at_rejected():
    with pytest.raises(ValidationError):
        parse_record(
            {
                "op": "init",
                "started_at": "2026-05-12T10:30:00+00:00",
                "completed_at": "2026-05-12T10:31:00",  # naive
                "target_ids": [],
                "profile_name": "x",
                "mappings": [],
            }
        )


def test_records_are_frozen():
    """`frozen=True` enforces the documented immutability contract —
    callers can't accidentally mutate a record between intent-write
    and mark_completed.
    """
    intent = _MappingIntent(
        tool_id="claude",
        mapping_index=0,
        live_path="/home/u/.claude",
        profile_subdir="claude",
        original_kind="real-dir",
    )
    op = _InitOp(
        op="init",
        started_at=_now(),
        target_ids=["claude"],
        profile_name="2026-05-12-current",
        mappings=[intent],
    )
    with pytest.raises(ValidationError):
        op.completed_at = _now()  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(ValidationError):
        intent.original_kind = "missing"  # pyright: ignore[reportAttributeAccessIssue]


def test_model_copy_produces_completed_record():
    """The documented `model_copy(update=...)` path is how OpLogIO
    transitions an in-flight record to completed without violating
    frozen=True.
    """
    op = _InitOp(
        op="init",
        started_at=_now(),
        target_ids=["claude"],
        profile_name="2026-05-12-current",
        mappings=[],
    )
    completed = op.model_copy(update={"completed_at": _now()})
    assert op.completed_at is None
    assert completed.completed_at == _now()
    assert completed is not op


def test_storage_path_round_trip_preserves_aliases_across_record_types():
    """The on-disk format is a JSON array, written via dump_records and
    read back via parse_records. Round-tripping through that path is
    what catches alias loss on _RenameOp.from_, discriminator drift,
    and any list-shape regressions.
    """
    intent = _MappingIntent(
        tool_id="claude",
        mapping_index=0,
        live_path="/home/u/.claude",
        profile_subdir="claude",
        original_kind="real-dir",
    )
    records = [
        _InitOp(
            op="init",
            started_at=_now(),
            target_ids=["claude"],
            profile_name="2026-05-12-current",
            mappings=[intent],
        ),
        _RenameOp.model_validate(
            {
                "op": "rename",
                "started_at": _now(),
                "from": "experiment",
                "to": "client-A",
                "affected_ids": ["claude"],
            }
        ),
        _RescanOp(
            op="rescan",
            started_at=_now(),
            target_ids=["claude"],
            target_profiles={"claude": "2026-05-12-rescan-1"},
            into_mode=False,
            previous_tools=None,
            mappings=[intent],
        ),
    ]

    blob = dump_records(records)
    raw: list[dict[str, Any]] = json.loads(blob)
    assert isinstance(raw, list)
    # JSON-side alias survives the round trip in the rename entry.
    rename_raw = next(r for r in raw if r["op"] == "rename")
    assert rename_raw["from"] == "experiment"
    assert "from_" not in rename_raw

    restored = parse_records(raw)
    assert len(restored) == 3
    assert isinstance(restored[0], _InitOp)
    assert isinstance(restored[1], _RenameOp)
    assert isinstance(restored[2], _RescanOp)
    assert restored[1].from_ == "experiment"
    assert restored[0].mappings[0].original_kind == "real-dir"
