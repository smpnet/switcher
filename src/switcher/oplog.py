"""Op-log journal — guided recovery of interrupted init/rename/rescan.

Spec §2.1. Records carry intent (the op type, its targets, per-mapping
original-state metadata for safe abort); compensation derives progress
from disk on every pass via the §2.1.1 four-state classifier.

Records are immutable: `frozen=True` on every model rejects in-place
mutation, and `extra="forbid"` rejects unknown fields so schema drift
surfaces as ValidationError (mapped to OpLogCorruptError by OpLogIO)
rather than being silently discarded. Transitioning a record to
"completed" therefore produces a fresh instance via `model_copy(
update={"completed_at": ...})` instead of in-place assignment.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, model_validator


class _MappingIntent(BaseModel):
    """Per-DirMapping original state — seed metadata for abort.

    `frozen=True` enforces the module-level immutability contract;
    `extra="forbid"` ensures schema drift surfaces as ValidationError
    (which OpLogIO maps to OpLogCorruptError) rather than being
    silently dropped.

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

    model_config = ConfigDict(frozen=True, extra="forbid")
    tool_id: str
    mapping_index: int
    live_path: str
    profile_subdir: str
    original_kind: Literal["missing", "link", "file", "real-dir"]


class _BaseOp(BaseModel):
    """Shared shape for every op record.

    Config:
    - `populate_by_name=True` so `_RenameOp.from_` accepts both `from`
      (JSON-side alias) and `from_` (Python-side keyword) on input.
    - `serialize_by_alias=True` so model_dump / model_dump_json default
      to alias-style keys (`from`, not `from_`). The journal's on-disk
      format uses aliases; making it the default removes the chance of
      a future single-record write accidentally emitting `from_`.
    - `frozen=True` enforces the module-level immutability contract.
      Transitioning to "completed" uses `model_copy(update={...})`
      to produce a new record rather than mutating in place.
    - `extra="forbid"` surfaces schema drift in oplog.json as
      ValidationError (mapped to OpLogCorruptError by OpLogIO) rather
      than silently discarding unknown fields and proceeding on
      partial data — that would defeat the journal's correctness role.

    The `op` discriminator is declared on each subclass as
    `Literal[...]` so basedpyright's invariant-override check stays
    happy (declaring `op: str` here would conflict with the narrower
    subclass overrides).
    """

    model_config = ConfigDict(
        populate_by_name=True,
        serialize_by_alias=True,
        frozen=True,
        extra="forbid",
    )
    # AwareDatetime rejects naive timestamps at validation time. A
    # recovery journal that accepted naive values would risk
    # cross-timezone ordering bugs: ts serialized on one host and
    # deserialized on another could compare incorrectly against aware
    # datetimes elsewhere in the codebase. Better to refuse at the door.
    started_at: AwareDatetime
    completed_at: AwareDatetime | None = None


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

    @model_validator(mode="after")
    def _check_into_mode_previous_tools_coherence(self) -> Self:
        """Reject schema-valid but semantically impossible combinations.

        `--into <existing>` rescan snapshots the prior tool-set of the
        target profile (so abort can restore it); fresh-mode rescan has
        nothing to snapshot. Allowing `into_mode=True, previous_tools=None`
        or `into_mode=False, previous_tools={...}` to slip into the
        journal would push invariant checking into recovery code and
        blur the line between corruption and a genuine interrupted op.
        """
        if self.into_mode and self.previous_tools is None:
            raise ValueError("_RescanOp: into_mode=True requires previous_tools to be present")
        if not self.into_mode and self.previous_tools is not None:
            raise ValueError("_RescanOp: into_mode=False requires previous_tools to be None")
        return self


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
    """Serialize a list of op records to JSON.

    Alias-style keys (e.g. `from` rather than `from_`) come from the
    model-level `serialize_by_alias=True` config; this helper does not
    need to pass `by_alias=True` explicitly.
    """
    return _records_adapter.dump_json(records).decode("utf-8")
