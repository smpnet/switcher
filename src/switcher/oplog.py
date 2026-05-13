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

import json
import os
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from switcher.errors import OpLogCorruptError

# StrictStr blocks str/int/bool coercion; Field(min_length=1) rejects
# the empty string. Together they ensure an empty `tool_id`,
# `profile_name`, `live_path`, etc. can't slip past validation as a
# valid (but obviously corrupt) journal entry. Used as an Annotated
# alias because Pydantic v2's StrictStr is a class, not a constraint
# container.
NonEmptyStr = Annotated[StrictStr, Field(min_length=1)]

# Mapping indices reference position within a DirMapping list. A
# negative index would silently resolve to the last element via Python's
# list-indexing semantics — driving compensation against the wrong
# mapping. Constrain to ge=0 so the journal never carries one.
NonNegativeInt = Annotated[StrictInt, Field(ge=0)]


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

    `original_kind` is narrow on purpose: init/rescan pre-flight
    explicitly reject `"link"` and `"file"` shapes at the live_path
    BEFORE a record is ever written, so the only values the journal can
    legitimately carry are `"missing"` and `"real-dir"`. Admitting
    `"link"` / `"file"` would let a hand-edited or corrupted journal
    file pass validation and defer the failure to abort time — exactly
    the "fail-fast at the corruption boundary" property the rest of
    this module is designed around.

    Detection of a live_path that has become a link or file *between*
    intent-write and abort is the four-state classifier's job (it
    raises AbortPreflightError on either shape); that observation comes
    from the live filesystem at abort time, not from the persisted
    snapshot.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    # Strict* types reject Pydantic's default str↔int / str↔bool / etc.
    # coercion: a hand-edited oplog.json with `mapping_index: "0"` would
    # otherwise validate as 0 and slip past the corruption boundary the
    # rest of this module is designed around. NonEmptyStr additionally
    # rejects the empty string. AwareDatetime is left as the regular
    # pydantic type because the on-disk format stores timestamps as
    # ISO strings — JSON has no native datetime — and AwareDatetime is
    # itself strict about the tzinfo invariant.
    tool_id: NonEmptyStr
    mapping_index: NonNegativeInt
    live_path: NonEmptyStr
    profile_subdir: NonEmptyStr
    original_kind: Literal["missing", "real-dir"]


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
    target_ids: list[NonEmptyStr]
    profile_name: NonEmptyStr
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
    from_: NonEmptyStr = Field(alias="from")
    to: NonEmptyStr
    affected_ids: list[NonEmptyStr]

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
    target_ids: list[NonEmptyStr]
    target_profiles: dict[NonEmptyStr, NonEmptyStr]
    into_mode: StrictBool
    previous_tools: dict[NonEmptyStr, dict[NonEmptyStr, StrictBool]] | None = None
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

    Two contracts, two failure shapes:

    - Pydantic v2's `model_copy(update={...})` silently bypasses
      validators on updated fields — using it for this transition could
      let a naive or backdated `completed_at` slip into the journal even
      though parse_record/parse_records would have rejected the same
      payload on read. This helper round-trips through the validator so
      the write path is as strict as the read path. A bad `when`
      surfaces as **ValidationError**, the same shape OpLogIO already
      wraps into OpLogCorruptError at the storage boundary.

    - The record is immutable from intent-write through mark_completed.
      Re-completing an already-completed record would rewrite its
      timestamp and undermine that audit guarantee, so we refuse instead
      — vacuum_completed is the only legitimate way for a completed
      record to leave the log. This case raises **ValueError**, not
      ValidationError, because it's API misuse by an in-memory caller
      rather than corruption of on-disk data; the journal-storage layer
      never sees it.
    """
    if record.completed_at is not None:
        raise ValueError(
            f"{type(record).__name__}: already completed at "
            f"{record.completed_at.isoformat()}; mark_completed cannot rewrite it"
        )
    payload = record.model_dump()
    payload["completed_at"] = when
    return parse_record(payload)


class MappingDiskState(Enum):
    """Per-mapping on-disk state for op-log compensation (spec §2.1.1).

    Four states partition the (live, target) shape space:

    - COMPLETE: live is a symlink resolving to target — nothing to do.
    - MOVE_DONE_LINK_MISSING: target populated, live absent — the
      SIGKILL window between move_or_seed_dir and swap_link. A two-state
      "captured or not" predicate would misclassify this as untouched.
    - UNTOUCHED: target absent, live matches `original_kind` — abort
      is a no-op for this mapping.
    - AMBIGUOUS: any other shape — refuse to compensate; surface to the
      user. Includes data-in-two-places, link-to-wrong-target, and live
      drifted to a shape that doesn't match the recorded original_kind.
    """

    COMPLETE = "complete"
    MOVE_DONE_LINK_MISSING = "move_done_link_missing"
    UNTOUCHED = "untouched"
    AMBIGUOUS = "ambiguous"


def _is_link(path: Path) -> bool:
    """Treat symlinks and Windows directory junctions as links.

    POSIX has only symlinks. Windows additionally has junctions, which
    `Path.is_symlink` returns False for; junctions are how this codebase
    falls back to "link-like" semantics when the user lacks the symlink
    privilege. Without this check, a junction-rooted live_path would
    classify as AMBIGUOUS even when it correctly resolves to target.

    `os.path.isjunction` was added in Python 3.12 and is always present
    under this project's `requires-python = ">=3.13"`; `paths.py` and
    `service.py` use it the same way. A hasattr guard would be
    redundant with the supported Python contract.
    """
    if path.is_symlink():
        return True
    if os.name == "nt":
        return os.path.isjunction(str(path))
    return False


def classify_mapping(intent: _MappingIntent, profile_dir: Path) -> MappingDiskState:
    """Classify the on-disk state of (live, target) for one mapping.

    Pure read; no mutations. The same classifier drives both --continue
    (where MOVE_DONE_LINK_MISSING means "finish the swap") and --abort
    (where MOVE_DONE_LINK_MISSING means "restore live from target")
    in spec §2.2 and §2.4. AMBIGUOUS is the catch-all the caller maps
    to AbortPreflightError / a user-facing refusal.

    Args:
        intent: the per-mapping intent record (`original_kind`, paths).
        profile_dir: the resolved <state_dir>/profiles/<profile_name>
            directory the op was targeting. Passed in explicitly so the
            classifier does not need to take a Store reference.
    """
    live = Path(intent.live_path)
    target = profile_dir / intent.profile_subdir
    live_is_link = _is_link(live)
    # `target_is_real_dir` requires the target path to be a *real*
    # directory owned by the profile, not a directory-shaped link.
    # `Path.is_dir()` follows symlinks and junctions, so without the
    # `_is_link` guard a target path that has been replaced by a link
    # to an unrelated directory would resolve through to `True` — and
    # if `live` resolves to the same place, the COMPLETE branch below
    # would silently accept reparse-point drift at the target as a
    # healthy mapping. The journal contract is "target is a directory",
    # not "target resolves to a directory"; treat the difference as
    # corruption and route through AMBIGUOUS.
    target_is_real_dir = target.is_dir() and not _is_link(target)
    # `target_missing` distinguishes "the target path has nothing at all"
    # from "the target path has something but it's not a directory" (a
    # regular file, a broken symlink, a link to an unrelated location).
    # Collapsing those two cases into `not target_is_real_dir` would let
    # UNTOUCHED fire when a corrupted target artifact is sitting there,
    # which is exactly the silent miscompensation the classifier exists
    # to refuse. `Path.exists()` returns False for broken symlinks and
    # for missing paths alike, so we check `_is_link(target)` too — a
    # broken symlink IS present, just dangling.
    target_missing = not target.exists() and not _is_link(target)

    # State 1: COMPLETE — live is a link resolving to a real target dir.
    # A link that fails to resolve, or resolves to anything other than
    # target, is AMBIGUOUS rather than UNTOUCHED: original_kind is
    # restricted to {"missing", "real-dir"} at intent-write time, so a
    # link at live_path is always drift relative to the recorded shape.
    if live_is_link:
        try:
            resolved = live.resolve()
        except (OSError, RuntimeError):
            return MappingDiskState.AMBIGUOUS
        if target_is_real_dir:
            try:
                if resolved == target.resolve():
                    return MappingDiskState.COMPLETE
            except (OSError, RuntimeError):
                return MappingDiskState.AMBIGUOUS
        return MappingDiskState.AMBIGUOUS

    # State 2: MOVE_DONE_LINK_MISSING — target is a real dir, live absent.
    # `live.exists()` follows symlinks; we already excluded the symlink
    # branch above so this check is correct for a regular path. Gating
    # on `target_is_real_dir` (not just `is_dir`) keeps reparse-point
    # drift at the target from being silently treated as a recoverable
    # mid-op state.
    if target_is_real_dir and not live.exists():
        return MappingDiskState.MOVE_DONE_LINK_MISSING

    # State 3: UNTOUCHED — target TRULY absent (not just "not a dir"),
    # and live matches the recorded original_kind exactly. Any deviation
    # from the recorded shape (a regular file where a directory was
    # expected, a directory where nothing was expected) is AMBIGUOUS —
    # refusing to compensate is safer than guessing intent.
    if target_missing:
        if intent.original_kind == "missing" and not live.exists():
            return MappingDiskState.UNTOUCHED
        if intent.original_kind == "real-dir" and live.is_dir() and not live_is_link:
            return MappingDiskState.UNTOUCHED
        return MappingDiskState.AMBIGUOUS

    # State 4: AMBIGUOUS — target is a real dir AND live also present as
    # a non-link shape (data in two places), OR target exists in some
    # shape that is not a real directory (regular file, link/junction
    # of any kind, broken symlink). All are corruption from the
    # journal's perspective.
    return MappingDiskState.AMBIGUOUS


class OpLogIO:
    """Thin façade over ``<state_dir>/oplog.json``.

    Every write goes through ``_write_records`` (tmp + ``Path.replace``)
    so a process crash mid-write cannot tear the file — observers see
    either the old contents or the new ones, never a partial write.
    See ``_write_records`` for the precise atomicity vs. durability
    contract. Empty / malformed / schema-mismatched payloads surface as
    :class:`OpLogCorruptError` rather than being silently recovered —
    a tmp-then-rename write never produces an empty file, so an empty
    oplog.json on disk means something external interfered, and a
    silent fallback would mask real corruption.

    Single-in-flight invariant: at most one record on disk has
    ``completed_at is None``. ``append_record`` refuses to add an
    intent while another op is still in flight; ``read_in_flight``
    raises on multiple uncompleted records as defense-in-depth against
    hand-edited journals. The journal serves a single-user CLI with
    no concurrent ops, so two in-flight records is corruption, not a
    race.
    """

    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir / "oplog.json"

    def read_records(self) -> list[OpLogRecord]:
        """Return every record on disk; empty list if the file is absent.

        Raises:
            OpLogCorruptError: file is unreadable, present as a dangling
                symlink, empty (tmp-then-rename writes never produce
                that shape), not valid JSON, not a JSON array at the
                top level, or fails schema validation.
        """
        if not self._path.exists():
            # `Path.exists()` follows symlinks, so a dangling symlink at
            # the journal path is False here even though something is
            # plainly present. Treat that as corruption rather than
            # silently returning [] — callers would otherwise proceed as
            # if no journal exists and could overwrite the evidence. A
            # symlink pointing at a real file falls through to the
            # normal read path and is treated as valid (a user putting
            # state on another disk is legitimate).
            if self._path.is_symlink():
                raise OpLogCorruptError(
                    f"oplog at {self._path} is a dangling symlink; manual recovery required"
                )
            return []
        try:
            blob = self._path.read_text(encoding="utf-8")
        except OSError as e:
            raise OpLogCorruptError(f"oplog at {self._path} could not be read: {e}") from e
        if not blob.strip():
            raise OpLogCorruptError(f"oplog at {self._path} is empty; manual recovery required")
        try:
            data = json.loads(blob)
        except json.JSONDecodeError as e:
            raise OpLogCorruptError(f"oplog at {self._path} is not valid JSON: {e}") from e
        if not isinstance(data, list):
            raise OpLogCorruptError(f"oplog at {self._path}: top-level must be a JSON array")
        try:
            return parse_records(data)
        except ValidationError as e:
            raise OpLogCorruptError(f"oplog at {self._path} failed schema validation: {e}") from e

    def read_in_flight(self) -> OpLogRecord | None:
        """Return the unique uncompleted record, or None.

        Raises:
            OpLogCorruptError: more than one record has
                ``completed_at is None``. ``append_record`` enforces the
                invariant on its own writes; surfacing it here protects
                against hand-edited or otherwise externally-corrupted
                journals.
        """
        in_flight = [r for r in self.read_records() if r.completed_at is None]
        if len(in_flight) > 1:
            raise OpLogCorruptError(
                f"oplog at {self._path}: {len(in_flight)} records are in-flight; "
                f"single-in-flight invariant violated. Manual recovery required."
            )
        return in_flight[0] if in_flight else None

    def append_record(self, record: OpLogRecord) -> None:
        """Append a new intent record and atomically rewrite the file.

        Raises:
            OpLogCorruptError: another record is already in-flight on
                disk. The caller must run the appropriate compensation
                command (``--continue`` or ``--abort``) for the existing
                op before starting a new one.
        """
        existing = self.read_records()
        if any(r.completed_at is None for r in existing):
            raise OpLogCorruptError(
                f"oplog at {self._path}: cannot append intent record while "
                f"another op is already in-flight. Run the compensation "
                f"command for the existing op first."
            )
        existing.append(record)
        self._write_records(existing)

    def mark_completed(self, record: OpLogRecord) -> None:
        """Set ``completed_at`` on the matching on-disk record and rewrite.

        Matches the disk record by ``op`` + ``started_at`` (sufficient
        discriminator for a single-user CLI). Delegates to the
        module-level :func:`mark_completed` helper, which re-validates
        through ``parse_record`` so a corrupt timestamp cannot slip in
        via the write path — Pydantic v2's ``model_copy(update={...})``
        bypasses validators on updated fields, which is intentionally
        avoided here. ValidationError from the helper is mapped to
        OpLogCorruptError to match the read-path failure shape.

        Enforces the single-in-flight invariant directly here (in
        addition to ``read_in_flight``): without it, this method would
        happily complete the first matching record in a hand-edited
        2-in-flight journal and leave the second one dangling — partial
        "healing" that hides corruption from the user instead of
        surfacing it. Same failure shape as ``read_in_flight``.

        Raises:
            OpLogCorruptError: more than one record is in-flight on
                disk (single-in-flight invariant violated), no in-flight
                record matches ``op`` + ``started_at``, or the
                completion timestamp fails re-validation. A caller
                holding a stale record reference is treated as
                corruption rather than as a silent no-op.
        """
        records = self.read_records()
        in_flight_count = sum(1 for r in records if r.completed_at is None)
        if in_flight_count > 1:
            raise OpLogCorruptError(
                f"oplog at {self._path}: {in_flight_count} records are in-flight; "
                f"single-in-flight invariant violated. Manual recovery required."
            )
        completed_at = datetime.now(UTC)
        matched = False
        new_records: list[OpLogRecord] = []
        for r in records:
            if (
                not matched
                and r.op == record.op
                and r.started_at == record.started_at
                and r.completed_at is None
            ):
                try:
                    new_records.append(mark_completed(r, completed_at))
                except ValidationError as e:
                    raise OpLogCorruptError(
                        f"oplog at {self._path}: mark_completed re-validation "
                        f"failed for op={r.op!r} started_at={r.started_at!r}: {e}"
                    ) from e
                matched = True
            else:
                new_records.append(r)
        if not matched:
            raise OpLogCorruptError(
                f"oplog at {self._path}: no matching in-flight record for "
                f"op={record.op!r} started_at={record.started_at!r}"
            )
        self._write_records(new_records)

    def vacuum_completed(self) -> None:
        """Drop every record whose ``completed_at`` is not None.

        Idempotent. If no completed records are present, the file is
        not rewritten — keeps mtime stable for callers that gate on it
        and avoids needless disk writes.

        Delegates through :meth:`read_records` for the absent / valid /
        corrupt decision rather than re-checking ``Path.exists()``
        here: that duplicate guard previously hid a dangling
        ``oplog.json`` symlink under the empty-list branch, which
        contradicts the read-path policy of surfacing external
        interference as :class:`OpLogCorruptError`. One read entry
        point keeps both paths aligned.

        Raises:
            OpLogCorruptError: anything read_records raises for —
                dangling symlink, malformed JSON, schema mismatch, etc.
        """
        records = self.read_records()
        kept = [r for r in records if r.completed_at is None]
        if len(kept) != len(records):
            self._write_records(kept)

    def _write_records(self, records: list[OpLogRecord]) -> None:
        """Tmp-then-rename write: prevent torn JSON, not power-loss durability.

        The guarantee is "no observer ever sees a partially-written
        oplog.json": a process crash mid-write leaves the old file
        atomically intact and the tmp file as harmless garbage. This is
        the right scope for the spec — recovery targets SIGKILL-style
        interruption of the CLI, not a kernel crash or power loss
        between the write and a subsequent fsync.

        It does NOT guarantee durability across a sudden host crash or
        power loss. Without an fsync on the tmp file (and the parent
        directory), a kernel-level event between write completion and
        the on-disk commit can lose the most recent record. Callers
        that need that level of durability are out of scope for the
        single-user recovery journal.

        Creates the parent directory if missing — on first-ever
        ``switcher init``, the op-log intent record is written BEFORE
        ``_store.create()`` materializes the state dir, so the parent
        may not exist yet. ``mkdir(parents=True, exist_ok=True)`` is
        idempotent and safe to call on every write.

        ``Path.replace`` is atomic on POSIX and Windows for
        same-filesystem renames; the tmp file shares the parent dir, so
        the rename never crosses filesystems.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = dump_records(records)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self._path)
