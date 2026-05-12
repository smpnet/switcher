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


def _mapping(tool_id: str = "claude", mapping_index: int = 0) -> _MappingIntent:
    return _MappingIntent(
        tool_id=tool_id,
        mapping_index=mapping_index,
        live_path=f"/home/u/.{tool_id}",
        profile_subdir=tool_id,
        original_kind="real-dir",
    )


def test_init_op_rejects_mapping_for_untargeted_tool():
    """A mapping for a tool not in target_ids is a corrupt journal entry
    — recovery would not know whether to treat it as part of this op or
    as external drift.
    """
    with pytest.raises(ValidationError):
        _InitOp(
            op="init",
            started_at=_now(),
            target_ids=["claude"],
            profile_name="2026-05-12-current",
            mappings=[_mapping(tool_id="copilot")],
        )


def test_init_op_rejects_duplicate_mapping_pair():
    """(tool_id, mapping_index) identifies one DirMapping; duplicates
    would let recovery double-process the same mapping.
    """
    with pytest.raises(ValidationError):
        _InitOp(
            op="init",
            started_at=_now(),
            target_ids=["claude"],
            profile_name="2026-05-12-current",
            mappings=[_mapping(mapping_index=0), _mapping(mapping_index=0)],
        )


def test_init_op_accepts_target_with_zero_mappings():
    """Registry-only tools (no DirMappings) are legitimate init targets;
    enforcing "every target_id has a mapping" would reject them.
    """
    op = _InitOp(
        op="init",
        started_at=_now(),
        target_ids=["claude", "registry-only-tool"],
        profile_name="2026-05-12-current",
        mappings=[_mapping(tool_id="claude")],
    )
    assert {m.tool_id for m in op.mappings} == {"claude"}


def test_rescan_op_rejects_target_profiles_mismatch():
    """target_profiles is a per-tool routing table; missing or extra
    entries vs target_ids would leave recovery without (or with surplus)
    routing data.
    """
    with pytest.raises(ValidationError):
        _RescanOp(
            op="rescan",
            started_at=_now(),
            target_ids=["claude"],
            target_profiles={"copilot": "shared"},  # wrong key
            into_mode=False,
            previous_tools=None,
            mappings=[],
        )
    with pytest.raises(ValidationError):
        _RescanOp(
            op="rescan",
            started_at=_now(),
            target_ids=["claude"],
            target_profiles={"claude": "x", "copilot": "y"},  # extra key
            into_mode=False,
            previous_tools=None,
            mappings=[],
        )


def test_rescan_op_rejects_mapping_for_untargeted_tool():
    with pytest.raises(ValidationError):
        _RescanOp(
            op="rescan",
            started_at=_now(),
            target_ids=["claude"],
            target_profiles={"claude": "shared"},
            into_mode=False,
            previous_tools=None,
            mappings=[_mapping(tool_id="copilot")],
        )


def test_init_op_rejects_duplicate_target_ids():
    """Duplicate target_ids = corrupt journal entry: iteration-based
    consumers double-count, set-based consumers silently dedupe.
    Refuse at validation time so neither path can happen.
    """
    with pytest.raises(ValidationError):
        _InitOp(
            op="init",
            started_at=_now(),
            target_ids=["claude", "claude"],
            profile_name="2026-05-12-current",
            mappings=[],
        )


def test_rescan_op_rejects_duplicate_target_ids():
    with pytest.raises(ValidationError):
        _RescanOp(
            op="rescan",
            started_at=_now(),
            target_ids=["claude", "claude"],
            target_profiles={"claude": "shared"},
            into_mode=False,
            previous_tools=None,
            mappings=[],
        )


def test_rename_op_rejects_duplicate_affected_ids():
    with pytest.raises(ValidationError):
        _RenameOp.model_validate(
            {
                "op": "rename",
                "started_at": _now(),
                "from": "a",
                "to": "b",
                "affected_ids": ["claude", "claude"],
            }
        )


def test_rescan_previous_tools_keys_must_match_target_profile_values():
    """In into-mode, previous_tools is the snapshot of every profile
    being rescanned into. Mismatched keys = no trustworthy snapshot for
    the profile we're about to overwrite (or snapshot of an unrelated
    profile carried along by mistake).
    """
    # Snapshot is for the wrong profile entirely.
    with pytest.raises(ValidationError):
        _RescanOp(
            op="rescan",
            started_at=_now(),
            target_ids=["claude"],
            target_profiles={"claude": "shared"},
            into_mode=True,
            previous_tools={"other-profile": {"claude": True}},
            mappings=[],
        )
    # Snapshot covers only one of two targeted profiles.
    with pytest.raises(ValidationError):
        _RescanOp(
            op="rescan",
            started_at=_now(),
            target_ids=["claude", "copilot"],
            target_profiles={"claude": "shared-a", "copilot": "shared-b"},
            into_mode=True,
            previous_tools={"shared-a": {"claude": True}},
            mappings=[],
        )


def test_rescan_previous_tools_accepts_shared_profile_collapse():
    """Two tools landing in the same profile = one snapshot, not two.
    `set(target_profiles.values())` collapses the duplicates so a
    single-entry previous_tools matches.
    """
    op = _RescanOp(
        op="rescan",
        started_at=_now(),
        target_ids=["claude", "copilot"],
        target_profiles={"claude": "shared", "copilot": "shared"},
        into_mode=True,
        previous_tools={"shared": {"claude": True, "copilot": False}},
        mappings=[],
    )
    assert op.into_mode is True


def test_rescan_into_mode_true_without_previous_tools_rejected():
    """into_mode=True snapshots the prior tool-set so abort can
    restore it; missing previous_tools is a semantically impossible
    record and must not be accepted into the journal.
    """
    with pytest.raises(ValidationError):
        _RescanOp(
            op="rescan",
            started_at=_now(),
            target_ids=["claude"],
            target_profiles={"claude": "shared"},
            into_mode=True,
            previous_tools=None,
            mappings=[],
        )


def test_rescan_into_mode_false_with_previous_tools_rejected():
    """Fresh-mode rescan has nothing to snapshot; carrying
    previous_tools alongside into_mode=False is semantically
    impossible and must not be accepted.
    """
    with pytest.raises(ValidationError):
        _RescanOp(
            op="rescan",
            started_at=_now(),
            target_ids=["claude"],
            target_profiles={"claude": "2026-05-12-rescan-1"},
            into_mode=False,
            previous_tools={"shared": {"claude": True}},
            mappings=[],
        )


def test_single_record_dump_uses_aliases_by_default():
    """serialize_by_alias=True at model level means a caller writing
    one record (rather than going through dump_records) still emits
    JSON-side aliases — `from`, not `from_`.
    """
    op = _RenameOp.model_validate(
        {
            "op": "rename",
            "started_at": _now(),
            "from": "experiment",
            "to": "client-A",
            "affected_ids": ["claude"],
        }
    )
    parsed = json.loads(op.model_dump_json())
    assert parsed["from"] == "experiment"
    assert "from_" not in parsed


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
