"""Op-log journal — guided recovery of interrupted init/rename/rescan.

Spec §2.1. Records carry intent (the op type, its targets, per-mapping
original-state metadata for safe abort); compensation derives progress
from disk on every pass via the §2.1.1 four-state classifier.

Records are immutable from intent-write to mark_completed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class _MappingIntent(BaseModel):
    """Per-DirMapping original state — seed metadata for abort.

    Captured at intent-write time, BEFORE any FS mutation. Tells abort
    how to undo move_or_seed_dir safely. None of these fields are
    consulted to decide what's "done" — only what's reversible and how.

    A "was_seeded" boolean is fully derivable from
    `original_kind == "missing"` ("real-dir" implies a move). Not stored
    separately.

    The `original_kind` literal includes `"link"` and `"file"` even
    though init/rescan pre-flight reject both shapes at intent time
    (so they should never be written to the log in normal operation).
    They remain representable so the deserialized record can carry an
    accurate post-hoc snapshot of pre-op state when external drift has
    occurred between intent-write and abort; the abort-time classifier
    then raises AbortPreflightError on either value rather than
    guessing. Narrowing the literal would force abort to lose this
    diagnostic information.
    """

    tool_id: str
    mapping_index: int
    live_path: str
    profile_subdir: str
    original_kind: Literal["missing", "link", "file", "real-dir"]


class _BaseOp(BaseModel):
    """Shared shape for every op record. `populate_by_name=True` so
    `_RenameOp.from_` accepts both `from` (JSON-side alias) and `from_`
    (Python-side keyword). The `op` discriminator is declared on each
    subclass as `Literal[...]` so basedpyright's invariant-override
    check stays happy."""

    model_config = ConfigDict(populate_by_name=True)
    started_at: datetime
    completed_at: datetime | None = None


class _InitOp(_BaseOp):
    op: Literal["init"]
    target_ids: list[str]
    profile_name: str
    mappings: list[_MappingIntent]


class _RenameOp(_BaseOp):
    op: Literal["rename"]
    from_: str = Field(alias="from")
    to: str
    affected_ids: list[str]


class _RescanOp(_BaseOp):
    op: Literal["rescan"]
    target_ids: list[str]
    target_profiles: dict[str, str]
    into_mode: bool
    previous_tools: dict[str, dict[str, bool]] | None = None
    mappings: list[_MappingIntent]


OpLogRecord = Annotated[
    _InitOp | _RenameOp | _RescanOp,
    Field(discriminator="op"),
]
_record_adapter: TypeAdapter[OpLogRecord] = TypeAdapter(OpLogRecord)
_records_adapter: TypeAdapter[list[OpLogRecord]] = TypeAdapter(list[OpLogRecord])


def parse_record(blob: dict[str, Any]) -> OpLogRecord:
    """Parse one JSON dict into the right op record subclass.

    Raises pydantic.ValidationError on malformed input — caller wraps
    that into OpLogCorruptError when reading the on-disk log.
    """
    return _record_adapter.validate_python(blob)


def parse_records(data: list[dict[str, Any]]) -> list[OpLogRecord]:
    """Parse a JSON array into a list of op records."""
    return _records_adapter.validate_python(data)


def dump_records(records: list[OpLogRecord]) -> str:
    """Serialize a list of op records to JSON (with alias-style keys)."""
    return _records_adapter.dump_json(records, by_alias=True).decode("utf-8")
