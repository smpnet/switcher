"""Op-log journal — guided recovery of interrupted init/rename/rescan.

Spec §2.1. Records carry intent (the op type, its targets, per-mapping
original-state metadata for safe abort); compensation derives progress
from disk on every pass via the §2.1.1 four-state classifier.

Records are immutable: `frozen=True` on every model rejects in-place
mutation, and `extra="forbid"` rejects unknown fields so schema drift
surfaces as ValidationError (mapped to OpLogCorruptError by OpLogIO)
rather than being silently discarded. Transitioning a record to
"completed" therefore produces a fresh instance via `mark_completed`,
which re-runs validation so a corrupt timestamp (naive, backdated)
can't slip into the journal through the write path —
`model_copy(update={...})` would bypass validation and is intentionally
not used here.
"""

from __future__ import annotations

from datetime import datetime
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
      Transitioning to "completed" goes through `mark_completed` (which
      re-validates) rather than the lower-level `model_copy(update={...})`,
      which silently bypasses validators in Pydantic v2.
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

    @model_validator(mode="after")
    def _check_completion_ordering(self) -> Self:
        """A record that completes before it started is corrupt — but
        without this check, the failure would only surface much later
        in ordering / recency logic where it's indistinguishable from
        external state drift. Refuse at validation time.
        """
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError(
                f"{type(self).__name__}: completed_at ({self.completed_at.isoformat()}) "
                f"precedes started_at ({self.started_at.isoformat()})"
            )
        return self


def _check_unique_tool_id_list(op_name: str, field_name: str, ids: list[str]) -> None:
    """Reject duplicate IDs in a tool-id list.

    `target_ids` / `affected_ids` are sets-as-lists for JSON-friendliness.
    A duplicate (e.g. target_ids=["claude", "claude"]) is a corrupt
    journal entry: downstream code that iterates the list double-counts,
    while code that converts to a set silently discards. We refuse at
    validation time so neither path can happen.
    """
    seen: set[str] = set()
    dupes: list[str] = []
    for tid in ids:
        if tid in seen and tid not in dupes:
            dupes.append(tid)
        seen.add(tid)
    if dupes:
        raise ValueError(f"{op_name}: {field_name} contains duplicate ids: {dupes!r}")


def _check_mappings_against_target_ids(
    op_name: str, target_ids: list[str], mappings: list[_MappingIntent]
) -> None:
    """Shared cross-field validator for init/rescan records.

    Two invariants — both "semantically impossible" entries that should
    never survive validation:

    1. Every `mappings[i].tool_id` must appear in `target_ids`. A mapping
       for a tool that isn't in the targeted set is a corrupt journal
       entry; recovery would not know whether to treat it as part of
       this op or as drift.

    2. `(tool_id, mapping_index)` pairs must be unique within mappings.
       The pair identifies a single DirMapping (tools can have several
       — e.g. claude has `~/.claude` and `~/.config/claude`); duplicates
       in the journal would let recovery double-process or double-revert
       the same mapping.

    NOT enforced: that every tool in `target_ids` has at least one
    mapping. Registry-only tools (no DirMappings) are valid targets and
    legitimately contribute zero entries to `mappings`.

    Target-id uniqueness is enforced separately via
    `_check_unique_tool_id_list` so the error message can point at the
    offending field directly.
    """
    target_set = set(target_ids)
    seen_pairs: set[tuple[str, int]] = set()
    for m in mappings:
        if m.tool_id not in target_set:
            raise ValueError(
                f"{op_name}: mapping references tool_id={m.tool_id!r} "
                f"not in target_ids={sorted(target_set)!r}"
            )
        key = (m.tool_id, m.mapping_index)
        if key in seen_pairs:
            raise ValueError(f"{op_name}: duplicate (tool_id, mapping_index)={key!r} in mappings")
        seen_pairs.add(key)


class _InitOp(_BaseOp):
    op: Literal["init"]
    target_ids: list[str]
    profile_name: str
    mappings: list[_MappingIntent]

    @model_validator(mode="after")
    def _check_target_ids_unique(self) -> Self:
        _check_unique_tool_id_list("_InitOp", "target_ids", self.target_ids)
        return self

    @model_validator(mode="after")
    def _check_mappings_consistency(self) -> Self:
        _check_mappings_against_target_ids("_InitOp", self.target_ids, self.mappings)
        return self


class _RenameOp(_BaseOp):
    op: Literal["rename"]
    from_: str = Field(alias="from")
    to: str
    affected_ids: list[str]

    @model_validator(mode="after")
    def _check_affected_ids_unique(self) -> Self:
        _check_unique_tool_id_list("_RenameOp", "affected_ids", self.affected_ids)
        return self

    @model_validator(mode="after")
    def _check_from_not_equal_to(self) -> Self:
        """A rename to the same name is either a no-op (nothing to do)
        or corruption (something else mutated the record). Either way
        the journal should refuse it rather than carry a record that
        would route through compensation logic for no purpose.
        """
        if self.from_ == self.to:
            raise ValueError(f"_RenameOp: from and to are both {self.from_!r}; rename is a no-op")
        return self


class _RescanOp(_BaseOp):
    op: Literal["rescan"]
    target_ids: list[str]
    target_profiles: dict[str, str]
    into_mode: bool
    previous_tools: dict[str, dict[str, bool]] | None = None
    mappings: list[_MappingIntent]

    @model_validator(mode="after")
    def _check_target_ids_unique(self) -> Self:
        _check_unique_tool_id_list("_RescanOp", "target_ids", self.target_ids)
        return self

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

    @model_validator(mode="after")
    def _check_target_profiles_keys_match_target_ids(self) -> Self:
        """target_profiles is a per-tool routing table; every targeted
        tool must have an entry (so recovery knows which profile to
        continue/abort), and no extra tools may appear.
        """
        if set(self.target_profiles.keys()) != set(self.target_ids):
            raise ValueError(
                f"_RescanOp: target_profiles keys {sorted(self.target_profiles)!r} "
                f"must equal target_ids {sorted(self.target_ids)!r}"
            )
        return self

    @model_validator(mode="after")
    def _check_previous_tools_keys_match_target_profile_values(self) -> Self:
        """In into-mode, previous_tools snapshots the prior contents of
        every profile that's being rescanned INTO. Its keys must equal
        `set(target_profiles.values())` — anything else means the journal
        either has no snapshot for a profile it's about to overwrite, or
        carries a snapshot of an unrelated profile. Both leave recovery
        without trustworthy data.

        Skipped in fresh mode — `_check_into_mode_previous_tools_coherence`
        already requires previous_tools to be None there.
        """
        if self.into_mode and self.previous_tools is not None:
            expected = set(self.target_profiles.values())
            actual = set(self.previous_tools.keys())
            if actual != expected:
                raise ValueError(
                    f"_RescanOp: previous_tools keys {sorted(actual)!r} must equal "
                    f"set(target_profiles.values()) = {sorted(expected)!r}"
                )
        return self

    @model_validator(mode="after")
    def _check_mappings_consistency(self) -> Self:
        _check_mappings_against_target_ids("_RescanOp", self.target_ids, self.mappings)
        return self


OpLogRecord = Annotated[
    _InitOp | _RenameOp | _RescanOp,
    Field(discriminator="op"),
]
_record_adapter: TypeAdapter[OpLogRecord] = TypeAdapter(OpLogRecord)
_records_adapter: TypeAdapter[list[OpLogRecord]] = TypeAdapter(list[OpLogRecord])


def parse_record(blob: Any) -> OpLogRecord:
    """Parse one JSON value into the right op record subclass.

    The parameter is typed `Any` rather than `dict[str, Any]` because
    callers feed this from json.loads() and the runtime value may be
    any JSON shape; Pydantic raises ValidationError on every shape
    other than the expected dict-of-fields, and the caller wraps that
    into OpLogCorruptError when reading the on-disk log.
    """
    return _record_adapter.validate_python(blob)


def parse_records(data: Any) -> list[OpLogRecord]:
    """Parse a JSON value into a list of op records.

    Like parse_record, this accepts `Any` so a top-level non-list value
    (object, string, null) surfaces through Pydantic's own
    ValidationError rather than as a TypeError at the boundary.
    """
    return _records_adapter.validate_python(data)


def dump_records(records: list[OpLogRecord]) -> str:
    """Serialize a list of op records to JSON.

    Alias-style keys (e.g. `from` rather than `from_`) come from the
    model-level `serialize_by_alias=True` config; this helper does not
    need to pass `by_alias=True` explicitly.
    """
    return _records_adapter.dump_json(records).decode("utf-8")


def mark_completed(record: OpLogRecord, when: datetime) -> OpLogRecord:
    """Return a copy of `record` with `completed_at=when`, re-validated.

    Two contracts:

    - Pydantic v2's `model_copy(update={...})` silently bypasses
      validators on updated fields — using it for this transition could
      let a naive or backdated `completed_at` slip into the journal even
      though parse_record/parse_records would have rejected the same
      payload on read. This helper round-trips through the validator so
      the write path is as strict as the read path.

    - The record is immutable from intent-write through mark_completed.
      Re-completing an already-completed record would rewrite its
      timestamp and undermine that audit guarantee, so we refuse instead
      — vacuum_completed is the only legitimate way for a completed
      record to leave the log.
    """
    if record.completed_at is not None:
        raise ValueError(
            f"{type(record).__name__}: already completed at "
            f"{record.completed_at.isoformat()}; mark_completed cannot rewrite it"
        )
    payload = record.model_dump()
    payload["completed_at"] = when
    return parse_record(payload)
