# pyright: reportPrivateUsage=none
"""Tests for op-log Pydantic models (spec §2.1)."""

import json
import sys
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
    mark_completed,
    parse_record,
    parse_records,
)


def _now() -> datetime:
    return datetime(2026, 5, 12, 10, 30, tzinfo=UTC)


def _live_path(tool_id: str = "claude") -> str:
    """Return a host-native absolute live path for ``tool_id``.

    ``validate_absolute_path`` rejects cross-platform "absolute" shapes
    (POSIX ``/...`` on Windows, drive-letter ``C:\\...`` on POSIX) so
    tests that need a happy-path value must use the host's native
    form. Centralized so every test references the same shape on each
    OS, and so a single edit re-routes coverage for both shards.
    """
    if sys.platform == "win32":
        return f"C:\\Users\\u\\.{tool_id}"
    return f"/home/u/.{tool_id}"


def test_mapping_intent_round_trip():
    intent = _MappingIntent(
        tool_id="claude",
        mapping_index=0,
        live_path=_live_path(),
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
                live_path=_live_path(),
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


@pytest.mark.parametrize(
    "field",
    ["tool_id", "live_path", "profile_subdir"],
)
def test_mapping_intent_string_fields_reject_empty(field: str):
    """min_length=1 on every persisted string keeps an obviously corrupt
    entry (empty tool_id, empty path) from passing validation.
    """
    payload = {
        "tool_id": "claude",
        "mapping_index": 0,
        "live_path": _live_path(),
        "profile_subdir": "claude",
        "original_kind": "real-dir",
    }
    payload[field] = ""
    with pytest.raises(ValidationError):
        _MappingIntent.model_validate(payload)


# Names that ``validate_safe_name`` rejects. Used across multiple
# record-level tests below to keep the corruption-boundary coverage
# symmetric across every persisted name/id field.
_UNSAFE_NAMES = [
    "../escape",  # parent-dir traversal
    "..",
    "a/b",  # POSIX path separator
    "a\\b",  # Windows path separator
    "/etc/passwd",  # POSIX absolute
    "C:\\Windows",  # Windows absolute
    "claude.",  # trailing dot (Windows-illegal)
    "CON",  # Windows reserved device name
    "CON.txt",  # reserved stem
]


@pytest.mark.parametrize("unsafe_id", _UNSAFE_NAMES)
def test_mapping_intent_tool_id_rejects_unsafe_names(unsafe_id: str):
    """``tool_id`` is the registry key for a Tool — same shape
    ``Tool.id`` already validates via ``validate_safe_name`` in
    ``models.py``. The journal must enforce the same invariant so a
    hand-edited entry can't carry an id that the rest of the codebase
    treats as a safe path segment.
    """
    payload = {
        "tool_id": unsafe_id,
        "mapping_index": 0,
        "live_path": _live_path(),
        "profile_subdir": "claude",
        "original_kind": "real-dir",
    }
    with pytest.raises(ValidationError):
        _MappingIntent.model_validate(payload)


# Paths that ``validate_absolute_path`` rejects regardless of host
# platform — wrong shape (relative, tilde, traversal) is corruption
# everywhere.
_UNSAFE_LIVE_PATHS_ANY_HOST = [
    "foo",  # bare relative
    "../escape",  # traversal
    "rel/path",  # relative multi-segment
    "~/rel",  # unexpanded tilde
    "~",
    "~user/rel",  # ~username form (also rejected by expand())
    "C:..\\escape",  # drive-relative traversal — not absolute on either
    "",  # empty (also caught by NonEmptyStr, but pinned here too)
]


@pytest.mark.parametrize("unsafe_live_path", _UNSAFE_LIVE_PATHS_ANY_HOST)
def test_mapping_intent_live_path_rejects_non_canonical_absolute(unsafe_live_path: str):
    """``live_path`` is later trusted by ``classify_mapping`` as a real
    absolute path via ``Path(intent.live_path)``. The intent writer
    always runs the value through ``PathResolver.expand()`` first, which
    produces a canonical absolute path. The journal must enforce that
    contract at the corruption boundary too; a hand-edited entry like
    ``"../escape"``, ``"~/rel"``, or ``"foo"`` would otherwise survive
    validation and let compensation reason about cwd-relative paths.
    """
    payload = {
        "tool_id": "claude",
        "mapping_index": 0,
        "live_path": unsafe_live_path,
        "profile_subdir": "claude",
        "original_kind": "real-dir",
    }
    with pytest.raises(ValidationError):
        _MappingIntent.model_validate(payload)


def test_mapping_intent_live_path_rejects_filesystem_root():
    """A bare filesystem root (``/`` on POSIX, ``C:\\`` on Windows)
    is structurally absolute and traversal-free, but the writer never
    emits a root-only path — real values look like
    ``/home/u/.claude``. Rejecting roots keeps the corruption
    boundary maximally fail-fast: a hand-edited journal can't point
    compensation at ``/`` and silently operate from there.
    """
    if sys.platform == "win32":
        root = "C:\\"
    else:
        root = "/"
    payload = {
        "tool_id": "claude",
        "mapping_index": 0,
        "live_path": root,
        "profile_subdir": "claude",
        "original_kind": "real-dir",
    }
    with pytest.raises(ValidationError):
        _MappingIntent.model_validate(payload)


def test_mapping_intent_live_path_rejects_host_native_traversal():
    """``..`` segments inside an otherwise-absolute host-native path are
    still corruption — they let compensation reason about a path
    outside the intended location. Use the host's separator so the
    parts list actually contains ``".."``.
    """
    if sys.platform == "win32":
        bad = "C:\\Users\\foo\\..\\escape"
    else:
        bad = "/home/u/../escape"
    payload = {
        "tool_id": "claude",
        "mapping_index": 0,
        "live_path": bad,
        "profile_subdir": "claude",
        "original_kind": "real-dir",
    }
    with pytest.raises(ValidationError):
        _MappingIntent.model_validate(payload)


# Host-native acceptance: only the shapes the host's pathlib.Path
# considers absolute. Cross-platform "absolute" forms get rejected
# because classify_mapping later does ``Path(intent.live_path)``,
# which would silently parse a cross-platform shape as relative
# against CWD and misclassify the mapping.
_POSIX_ABSOLUTE_LIVE_PATHS = [
    _live_path(),
    "/Users/foo/.claude",
    "/var/lib/foo",
]
_WINDOWS_ABSOLUTE_LIVE_PATHS = [
    "C:\\Users\\foo\\.claude",
    "C:/Users/foo/.claude",
    "\\\\server\\share\\.claude",
]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-shaped accept set")
@pytest.mark.parametrize("good_live_path", _POSIX_ABSOLUTE_LIVE_PATHS)
def test_mapping_intent_live_path_accepts_posix_absolute_on_posix(good_live_path: str):
    payload = {
        "tool_id": "claude",
        "mapping_index": 0,
        "live_path": good_live_path,
        "profile_subdir": "claude",
        "original_kind": "real-dir",
    }
    intent = _MappingIntent.model_validate(payload)
    assert intent.live_path == good_live_path


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-shaped accept set")
@pytest.mark.parametrize("good_live_path", _WINDOWS_ABSOLUTE_LIVE_PATHS)
def test_mapping_intent_live_path_accepts_windows_absolute_on_windows(good_live_path: str):
    payload = {
        "tool_id": "claude",
        "mapping_index": 0,
        "live_path": good_live_path,
        "profile_subdir": "claude",
        "original_kind": "real-dir",
    }
    intent = _MappingIntent.model_validate(payload)
    assert intent.live_path == good_live_path


@pytest.mark.skipif(sys.platform == "win32", reason="Windows-shaped on POSIX is corruption")
@pytest.mark.parametrize("cross_platform", _WINDOWS_ABSOLUTE_LIVE_PATHS)
def test_mapping_intent_live_path_rejects_windows_shape_on_posix(cross_platform: str):
    """A Windows-absolute path on a POSIX host is not absolute to the
    host's ``pathlib.Path`` — ``Path("C:/Users/me").is_absolute()`` is
    False under PosixPath, so the classifier would treat it as a
    cwd-relative path and silently misclassify the mapping (Hermes
    reproduced this returning UNTOUCHED instead of refusing). Reject
    at the corruption boundary so the validator's contract matches
    what the runtime consumer actually does.
    """
    payload = {
        "tool_id": "claude",
        "mapping_index": 0,
        "live_path": cross_platform,
        "profile_subdir": "claude",
        "original_kind": "real-dir",
    }
    with pytest.raises(ValidationError):
        _MappingIntent.model_validate(payload)


@pytest.mark.skipif(sys.platform != "win32", reason="POSIX-shaped on Windows is corruption")
@pytest.mark.parametrize("cross_platform", _POSIX_ABSOLUTE_LIVE_PATHS)
def test_mapping_intent_live_path_rejects_posix_shape_on_windows(cross_platform: str):
    """Mirror of the POSIX case: a POSIX-rooted path on Windows is
    current-drive-relative, not absolute, so ``Path(...).is_absolute()``
    is False and the classifier would misroute the mapping.
    """
    payload = {
        "tool_id": "claude",
        "mapping_index": 0,
        "live_path": cross_platform,
        "profile_subdir": "claude",
        "original_kind": "real-dir",
    }
    with pytest.raises(ValidationError):
        _MappingIntent.model_validate(payload)


@pytest.mark.parametrize(
    "unsafe_subdir",
    _UNSAFE_NAMES,
)
def test_mapping_intent_profile_subdir_rejects_unsafe_names(unsafe_subdir: str):
    """``profile_subdir`` is joined to ``profile_dir`` as a path segment
    during classification/recovery, so a hand-edited journal entry like
    ``"../escape"`` or ``"/etc/passwd"`` would let compensation reason
    about paths outside the profile directory and operate on arbitrary
    locations. The rest of the codebase routes ``DirMapping.profile_subdir``
    through ``validate_safe_name``; the op-log persists the same value
    and must enforce the same invariant at the corruption boundary so
    a corrupt journal can't bypass that check.
    """
    payload = {
        "tool_id": "claude",
        "mapping_index": 0,
        "live_path": _live_path(),
        "profile_subdir": unsafe_subdir,
        "original_kind": "real-dir",
    }
    with pytest.raises(ValidationError):
        _MappingIntent.model_validate(payload)


def test_init_op_rejects_empty_profile_name_and_target_id():
    with pytest.raises(ValidationError):
        parse_record(
            {
                "op": "init",
                "started_at": "2026-05-12T10:30:00+00:00",
                "target_ids": [""],
                "profile_name": "x",
                "mappings": [],
            }
        )
    with pytest.raises(ValidationError):
        parse_record(
            {
                "op": "init",
                "started_at": "2026-05-12T10:30:00+00:00",
                "target_ids": ["claude"],
                "profile_name": "",
                "mappings": [],
            }
        )


def test_rename_op_rejects_empty_from_or_to():
    with pytest.raises(ValidationError):
        _RenameOp.model_validate(
            {
                "op": "rename",
                "started_at": _now(),
                "from": "",
                "to": "client-A",
                "affected_ids": [],
            }
        )
    with pytest.raises(ValidationError):
        _RenameOp.model_validate(
            {
                "op": "rename",
                "started_at": _now(),
                "from": "experiment",
                "to": "",
                "affected_ids": [],
            }
        )


@pytest.mark.parametrize("unsafe", _UNSAFE_NAMES)
def test_init_op_profile_name_rejects_unsafe_names(unsafe: str):
    """``profile_name`` is the profile-store key; ``Profile.name``
    already validates the same shape via ``validate_safe_name``. A
    corrupt journal entry like ``profile_name="../escape"`` would
    otherwise survive deserialization and only blow up later when
    compensation joined it to ``state_dir/profiles``.
    """
    with pytest.raises(ValidationError):
        parse_record(
            {
                "op": "init",
                "started_at": "2026-05-12T10:30:00+00:00",
                "target_ids": ["claude"],
                "profile_name": unsafe,
                "mappings": [],
            }
        )


@pytest.mark.parametrize("unsafe", _UNSAFE_NAMES)
def test_init_op_target_ids_rejects_unsafe_names(unsafe: str):
    """``target_ids`` carries tool IDs — ``Tool.id`` is itself
    validated via ``validate_safe_name``. The journal must enforce
    the same invariant so a hand-edited list can't carry a tool id
    that the rest of the codebase treats as a safe segment.
    """
    with pytest.raises(ValidationError):
        parse_record(
            {
                "op": "init",
                "started_at": "2026-05-12T10:30:00+00:00",
                "target_ids": [unsafe],
                "profile_name": "current",
                "mappings": [],
            }
        )


@pytest.mark.parametrize("unsafe", _UNSAFE_NAMES)
@pytest.mark.parametrize("field", ["from", "to"])
def test_rename_op_from_and_to_reject_unsafe_names(field: str, unsafe: str):
    """``from``/``to`` are profile names. Same source-of-truth shape as
    ``profile_name`` above.
    """
    payload = {
        "op": "rename",
        "started_at": "2026-05-12T10:30:00+00:00",
        "from": "old-profile",
        "to": "new-profile",
        "affected_ids": [],
    }
    payload[field] = unsafe
    with pytest.raises(ValidationError):
        parse_record(payload)


@pytest.mark.parametrize("unsafe", _UNSAFE_NAMES)
def test_rename_op_affected_ids_rejects_unsafe_names(unsafe: str):
    with pytest.raises(ValidationError):
        parse_record(
            {
                "op": "rename",
                "started_at": "2026-05-12T10:30:00+00:00",
                "from": "old-profile",
                "to": "new-profile",
                "affected_ids": [unsafe],
            }
        )


@pytest.mark.parametrize("unsafe", _UNSAFE_NAMES)
def test_rescan_op_target_ids_rejects_unsafe_names(unsafe: str):
    with pytest.raises(ValidationError):
        parse_record(
            {
                "op": "rescan",
                "started_at": "2026-05-12T10:30:00+00:00",
                "target_ids": [unsafe],
                "target_profiles": {unsafe: "current"},
                "into_mode": False,
                "mappings": [],
            }
        )


@pytest.mark.parametrize("unsafe", _UNSAFE_NAMES)
def test_rescan_op_target_profiles_value_rejects_unsafe_names(unsafe: str):
    """``target_profiles`` values are profile names (where to rescan
    INTO). Same shape constraint as ``profile_name`` / ``Profile.name``.
    """
    with pytest.raises(ValidationError):
        parse_record(
            {
                "op": "rescan",
                "started_at": "2026-05-12T10:30:00+00:00",
                "target_ids": ["claude"],
                "target_profiles": {"claude": unsafe},
                "into_mode": False,
                "mappings": [],
            }
        )


@pytest.mark.parametrize("unsafe", _UNSAFE_NAMES)
def test_rescan_op_target_profiles_key_rejects_unsafe_names(unsafe: str):
    """``target_profiles`` keys are tool IDs."""
    with pytest.raises(ValidationError):
        parse_record(
            {
                "op": "rescan",
                "started_at": "2026-05-12T10:30:00+00:00",
                "target_ids": [unsafe],
                "target_profiles": {unsafe: "current"},
                "into_mode": False,
                "mappings": [],
            }
        )


@pytest.mark.parametrize("unsafe", _UNSAFE_NAMES)
def test_rescan_op_previous_tools_outer_key_rejects_unsafe_names(unsafe: str):
    """``previous_tools`` outer keys are profile names (the
    profile-set being rescanned INTO)."""
    with pytest.raises(ValidationError):
        parse_record(
            {
                "op": "rescan",
                "started_at": "2026-05-12T10:30:00+00:00",
                "target_ids": ["claude"],
                "target_profiles": {"claude": unsafe},
                "into_mode": True,
                "previous_tools": {unsafe: {"claude": True}},
                "mappings": [],
            }
        )


@pytest.mark.parametrize("unsafe", _UNSAFE_NAMES)
def test_rescan_op_previous_tools_inner_key_rejects_unsafe_names(unsafe: str):
    """``previous_tools`` inner keys are tool IDs (the tools the
    target profile previously hosted)."""
    with pytest.raises(ValidationError):
        parse_record(
            {
                "op": "rescan",
                "started_at": "2026-05-12T10:30:00+00:00",
                "target_ids": ["claude"],
                "target_profiles": {"claude": "current"},
                "into_mode": True,
                "previous_tools": {"current": {unsafe: True}},
                "mappings": [],
            }
        )


def test_negative_mapping_index_rejected():
    """mapping_index addresses a position in a DirMapping list. A
    negative value would resolve to the last element via Python's
    list-indexing semantics — driving compensation against the wrong
    mapping. ge=0 keeps that out of the journal.
    """
    with pytest.raises(ValidationError):
        _MappingIntent.model_validate(
            {
                "tool_id": "claude",
                "mapping_index": -1,
                "live_path": _live_path(),
                "profile_subdir": "claude",
                "original_kind": "real-dir",
            }
        )


def test_mapping_index_str_not_coerced_to_int():
    """Pydantic's default int coercion would accept "0" → 0, which
    would let a hand-edited journal slip past the corruption boundary.
    StrictInt rejects.
    """
    with pytest.raises(ValidationError):
        _MappingIntent.model_validate(
            {
                "tool_id": "claude",
                "mapping_index": "0",
                "live_path": _live_path(),
                "profile_subdir": "claude",
                "original_kind": "real-dir",
            }
        )


def test_target_ids_int_not_coerced_to_str():
    """target_ids=[123] would coerce to ["123"] without StrictStr."""
    with pytest.raises(ValidationError):
        parse_record(
            {
                "op": "init",
                "started_at": "2026-05-12T10:30:00+00:00",
                "target_ids": [123],
                "profile_name": "x",
                "mappings": [],
            }
        )


def test_previous_tools_str_not_coerced_to_bool():
    """previous_tools={"shared": {"claude": "false"}} would coerce
    "false" → False without StrictBool. The journal must reject."""
    with pytest.raises(ValidationError):
        parse_record(
            {
                "op": "rescan",
                "started_at": "2026-05-12T10:30:00+00:00",
                "target_ids": ["claude"],
                "target_profiles": {"claude": "shared"},
                "into_mode": True,
                "previous_tools": {"shared": {"claude": "false"}},
                "mappings": [],
            }
        )


def test_into_mode_str_not_coerced_to_bool():
    """Same rejection on a top-level _RescanOp field."""
    with pytest.raises(ValidationError):
        parse_record(
            {
                "op": "rescan",
                "started_at": "2026-05-12T10:30:00+00:00",
                "target_ids": ["claude"],
                "target_profiles": {"claude": "shared"},
                "into_mode": "true",
                "previous_tools": {"shared": {"claude": True}},
                "mappings": [],
            }
        )


@pytest.mark.parametrize("bogus", ["hardlink", "link", "file", "directory", ""])
def test_invalid_original_kind_rejected(bogus: str):
    """original_kind is narrow on purpose: only the values
    init/rescan pre-flight actually admits — `missing` and `real-dir` —
    are accepted. `link` and `file` are explicitly rejected so a
    hand-edited or corrupted journal fails fast at the corruption
    boundary instead of deferring the failure to abort time.
    """
    with pytest.raises(ValidationError):
        _MappingIntent.model_validate(
            {
                "tool_id": "claude",
                "mapping_index": 0,
                "live_path": _live_path(),
                "profile_subdir": "claude",
                "original_kind": bogus,
            }
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


def test_unknown_field_in_rename_op_rejected():
    """Symmetric coverage with _InitOp — rejecting unknown fields is a
    journal-wide safety property, not init-only."""
    with pytest.raises(ValidationError):
        parse_record(
            {
                "op": "rename",
                "started_at": "2026-05-12T10:30:00+00:00",
                "from": "a",
                "to": "b",
                "affected_ids": [],
                "rogue_field": "drift",
            }
        )


def test_unknown_field_in_rescan_op_rejected():
    with pytest.raises(ValidationError):
        parse_record(
            {
                "op": "rescan",
                "started_at": "2026-05-12T10:30:00+00:00",
                "target_ids": [],
                "target_profiles": {},
                "into_mode": False,
                "previous_tools": None,
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
                "live_path": _live_path(),
                "profile_subdir": "claude",
                "original_kind": "real-dir",
                "rogue_field": "drift",
            }
        )


def _mapping(tool_id: str = "claude", mapping_index: int = 0) -> _MappingIntent:
    return _MappingIntent(
        tool_id=tool_id,
        mapping_index=mapping_index,
        live_path=_live_path(tool_id),
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


def test_rename_op_rejects_from_equals_to():
    """A rename to the same name is either a no-op or corruption;
    either way the journal should refuse it rather than route through
    compensation for no purpose.
    """
    with pytest.raises(ValidationError):
        _RenameOp.model_validate(
            {
                "op": "rename",
                "started_at": _now(),
                "from": "experiment",
                "to": "experiment",
                "affected_ids": ["claude"],
            }
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
        live_path=_live_path(),
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


def test_mark_completed_produces_validated_completed_record():
    """mark_completed is the documented transition path because it
    re-runs validation; model_copy(update=...) in pydantic v2 would
    silently bypass validators and let a corrupt completed_at slip
    into the journal.
    """
    op = _InitOp(
        op="init",
        started_at=_now(),
        target_ids=["claude"],
        profile_name="2026-05-12-current",
        mappings=[],
    )
    when = datetime(2026, 5, 12, 10, 31, tzinfo=UTC)
    completed = mark_completed(op, when)
    assert op.completed_at is None  # original untouched
    assert completed.completed_at == when
    assert completed is not op


def test_mark_completed_rejects_naive_timestamp():
    """The write path must be as strict as the read path —
    AwareDatetime guards parse_record; mark_completed must guard the
    completion transition the same way (model_copy would have
    silently accepted a naive datetime).
    """
    op = _InitOp(
        op="init",
        started_at=_now(),
        target_ids=["claude"],
        profile_name="2026-05-12-current",
        mappings=[],
    )
    naive = datetime(2026, 5, 12, 10, 31)
    with pytest.raises(ValidationError):
        mark_completed(op, naive)


def test_mark_completed_rejects_backdated_timestamp():
    """Same reasoning: _check_completion_ordering guards parse_record,
    and mark_completed must re-validate so a backdated completed_at
    can't reach disk via the write path.
    """
    op = _InitOp(
        op="init",
        started_at=datetime(2026, 5, 12, 10, 30, tzinfo=UTC),
        target_ids=["claude"],
        profile_name="2026-05-12-current",
        mappings=[],
    )
    backdated = datetime(2026, 5, 12, 10, 29, tzinfo=UTC)
    with pytest.raises(ValidationError):
        mark_completed(op, backdated)


def test_mark_completed_refuses_to_recomplete():
    """The audit contract says records are immutable from intent-write
    through completion. Re-marking an already-completed record would
    rewrite its timestamp; refuse instead so vacuum_completed remains
    the only legitimate way for a completed record to leave the log.
    """
    op = _InitOp(
        op="init",
        started_at=_now(),
        target_ids=["claude"],
        profile_name="2026-05-12-current",
        mappings=[],
    )
    once = mark_completed(op, datetime(2026, 5, 12, 10, 31, tzinfo=UTC))
    assert once.completed_at is not None
    with pytest.raises(ValueError, match="already completed"):
        mark_completed(once, datetime(2026, 5, 12, 10, 32, tzinfo=UTC))


def test_mark_completed_preserves_rename_alias_field():
    """Sanity check the round-trip: _RenameOp's from_ field has a
    JSON alias, and the validate-after-dump pattern in mark_completed
    must preserve it (model_dump emits 'from' due to
    serialize_by_alias=True; parse_record reads it back via
    populate_by_name=True).
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
    completed = mark_completed(op, datetime(2026, 5, 12, 10, 31, tzinfo=UTC))
    assert isinstance(completed, _RenameOp)
    assert completed.from_ == "experiment"
    assert completed.to == "client-A"


def test_completed_at_before_started_at_rejected():
    """Timestamp inversion = corrupt record. Without the validator
    the failure would surface much later in ordering/recency logic
    where it's hard to distinguish from external drift.
    """
    with pytest.raises(ValidationError):
        _InitOp(
            op="init",
            started_at=datetime(2026, 5, 12, 10, 30, tzinfo=UTC),
            completed_at=datetime(2026, 5, 12, 10, 29, tzinfo=UTC),
            target_ids=[],
            profile_name="x",
            mappings=[],
        )


def test_completed_at_equal_to_started_at_allowed():
    """The check is strict ordering, not strict inequality: a record
    that starts and completes within the same clock tick is legal.
    """
    ts = datetime(2026, 5, 12, 10, 30, tzinfo=UTC)
    op = _InitOp(
        op="init",
        started_at=ts,
        completed_at=ts,
        target_ids=[],
        profile_name="x",
        mappings=[],
    )
    assert op.completed_at == op.started_at


def test_parse_records_rejects_non_list_top_level():
    """parse_records is the file-level entry point — a top-level
    object/string/null in oplog.json must surface as ValidationError
    rather than slip through as a one-element coerced list.
    """
    with pytest.raises(ValidationError):
        parse_records({"not": "a list"})
    with pytest.raises(ValidationError):
        parse_records("string")
    with pytest.raises(ValidationError):
        parse_records(None)


def test_parse_records_rejects_list_of_non_objects():
    with pytest.raises(ValidationError):
        parse_records(["just a string"])
    with pytest.raises(ValidationError):
        parse_records([42])
    with pytest.raises(ValidationError):
        parse_records([None])


def test_storage_path_round_trip_preserves_aliases_across_record_types():
    """The on-disk format is a JSON array, written via dump_records and
    read back via parse_records. Round-tripping through that path is
    what catches alias loss on _RenameOp.from_, discriminator drift,
    and any list-shape regressions.
    """
    intent = _MappingIntent(
        tool_id="claude",
        mapping_index=0,
        live_path=_live_path(),
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
