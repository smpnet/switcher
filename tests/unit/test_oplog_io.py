# pyright: reportPrivateUsage=none
"""Tests for the OpLogIO class — read, write, append, mark_completed,
vacuum (spec §2.1, §2.6)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from switcher.errors import OpLogCorruptError, StorageError
from switcher.oplog import (
    OpLogIO,
    _RenameOp,
)


def _file_symlinks_available() -> bool:
    """Probe at module-import time whether the runner can create file
    symlinks. Always True on POSIX; depends on the SeCreateSymbolicLink
    privilege (or Developer Mode) on Windows. Without the privilege,
    ``Path.symlink_to(target)`` raises OSError before our code under
    test runs at all.
    """
    if sys.platform != "win32":
        return True
    try:
        with tempfile.TemporaryDirectory() as d:
            probe_dir = Path(d)
            target = probe_dir / "_t"
            target.write_text("")
            link = probe_dir / "_l"
            link.symlink_to(target)
        return True
    except OSError:
        return False


_FILE_SYMLINKS_AVAILABLE = _file_symlinks_available()
_skip_no_file_symlinks = pytest.mark.skipif(
    not _FILE_SYMLINKS_AVAILABLE,
    reason="file symlink creation requires privilege/Developer Mode on Windows",
)


def _now() -> datetime:
    return datetime(2026, 5, 12, 10, 30, tzinfo=UTC)


def _make_rename_op(from_name: str = "a", to_name: str = "b") -> _RenameOp:
    # Use the JSON-side alias `from` via model_validate — populate_by_name
    # would let _RenameOp(**kwargs) accept `from_` at runtime, but
    # basedpyright's generated __init__ signature uses the alias, so the
    # validate path keeps the test type-clean.
    return _RenameOp.model_validate(
        {
            "op": "rename",
            "started_at": _now(),
            "from": from_name,
            "to": to_name,
            "affected_ids": [],
        }
    )


def test_read_records_absent_file_returns_empty(tmp_path: Path):
    io = OpLogIO(tmp_path)
    assert io.read_records() == []


def test_read_records_empty_file_raises_corrupt(tmp_path: Path):
    (tmp_path / "oplog.json").write_text("")
    io = OpLogIO(tmp_path)
    with pytest.raises(OpLogCorruptError):
        io.read_records()


def test_read_records_io_failure_raises_storage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A bare ``OSError`` from ``read_text`` (EACCES, transient FS
    failure, file unmounted underneath us, etc.) is an I/O concern,
    not a journal-corruption concern: the file *contents* may be
    perfectly fine, we just couldn't open it. Mapping every OSError
    to ``OpLogCorruptError`` would tell the user "manual recovery
    required" for what's actually a permissions or transient-FS
    issue, and conflicts with ``store.py``'s pattern of routing
    unreadable files through :class:`StorageError`. Keep the
    corruption boundary for content/shape problems only.
    """
    (tmp_path / "oplog.json").write_text(
        "[]", encoding="utf-8"
    )  # contents are valid; only the read fails

    def raises_eacces(self: Path, **_kwargs: object) -> str:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_text", raises_eacces)
    io = OpLogIO(tmp_path)
    with pytest.raises(StorageError):
        io.read_records()


def test_read_records_malformed_json_raises_corrupt(tmp_path: Path):
    (tmp_path / "oplog.json").write_text("{not json")
    io = OpLogIO(tmp_path)
    with pytest.raises(OpLogCorruptError):
        io.read_records()


def test_read_records_unknown_op_raises_corrupt(tmp_path: Path):
    (tmp_path / "oplog.json").write_text(
        json.dumps([{"op": "vacuum", "started_at": "2026-05-12T10:30:00+00:00"}])
    )
    io = OpLogIO(tmp_path)
    with pytest.raises(OpLogCorruptError):
        io.read_records()


@_skip_no_file_symlinks
def test_read_records_dangling_symlink_raises_corrupt(tmp_path: Path):
    """Any symlink at the journal path is corruption — see the
    non-dangling test below for why. Dangling is the easier shape to
    notice because ``Path.exists()`` already returns False; covered
    explicitly so the broken-link sub-case stays pinned.
    """
    (tmp_path / "oplog.json").symlink_to(tmp_path / "does-not-exist")
    io = OpLogIO(tmp_path)
    with pytest.raises(OpLogCorruptError):
        io.read_records()


@_skip_no_file_symlinks
def test_read_records_valid_symlink_raises_corrupt(tmp_path: Path):
    """A symlink at the journal path pointing at a real file would
    read fine — but ``_write_records`` uses ``tmp.replace(self._path)``
    which replaces the symlink ITSELF with a regular file in
    state_dir, orphaning whatever the symlink pointed at. Letting the
    read path silently accept this shape forks journal state on the
    first write. Reject the whole shape (broken OR resolving), and
    let the user decide whether to undo the symlink or move state
    dirs. Matches read_records' fail-fast contract with the write
    path's actual behavior.
    """
    external = tmp_path / "external.json"
    external.write_text("[]", encoding="utf-8")
    (tmp_path / "oplog.json").symlink_to(external)
    io = OpLogIO(tmp_path)
    with pytest.raises(OpLogCorruptError):
        io.read_records()


def test_read_records_invalid_utf8_raises_corrupt(tmp_path: Path):
    """``read_text(encoding='utf-8')`` raises ``UnicodeDecodeError`` for
    non-UTF-8 bytes. The module only catches ``OSError`` around it, so
    invalid bytes would escape as a raw decode error and bypass the
    corruption boundary the rest of the module is enforcing. Same
    failure shape as malformed JSON: refuse, surface as
    ``OpLogCorruptError``.
    """
    (tmp_path / "oplog.json").write_bytes(b"\xff\xfe\xfd")
    io = OpLogIO(tmp_path)
    with pytest.raises(OpLogCorruptError):
        io.read_records()


def test_read_records_cross_platform_live_path_raises_corrupt(tmp_path: Path):
    """End-to-end regression for the live_path host-native contract.
    A journal carrying a non-host-native absolute path (Windows-shaped
    on POSIX or vice versa) must surface as OpLogCorruptError when
    read, rather than slipping through validation and letting
    classify_mapping silently misclassify the mapping. Pin the
    behavior at the OpLogIO boundary so the contract holds for every
    consumer that reaches the disk through this façade.
    """
    if sys.platform == "win32":
        cross_platform_path = "/home/u/.claude"
    else:
        cross_platform_path = "C:\\Users\\me\\.claude"
    payload = json.dumps(
        [
            {
                "op": "init",
                "started_at": "2026-05-12T10:30:00+00:00",
                "target_ids": ["claude"],
                "profile_name": "current",
                "mappings": [
                    {
                        "tool_id": "claude",
                        "mapping_index": 0,
                        "live_path": cross_platform_path,
                        "profile_subdir": "claude",
                        "original_kind": "real-dir",
                    }
                ],
            }
        ]
    )
    (tmp_path / "oplog.json").write_text(payload, encoding="utf-8")
    io = OpLogIO(tmp_path)
    with pytest.raises(OpLogCorruptError):
        io.read_records()


def test_read_records_non_list_top_level_raises_corrupt(tmp_path: Path):
    """A valid JSON value that isn't a list (e.g. an object) is a
    distinct corruption path from malformed JSON — it parses but fails
    the top-level array contract enforced before schema validation.
    """
    (tmp_path / "oplog.json").write_text("{}")
    io = OpLogIO(tmp_path)
    with pytest.raises(OpLogCorruptError):
        io.read_records()


def test_append_record_creates_file(tmp_path: Path):
    io = OpLogIO(tmp_path)
    io.append_record(_make_rename_op())
    on_disk: list[dict[str, Any]] = json.loads(
        (tmp_path / "oplog.json").read_text(encoding="utf-8")
    )
    assert isinstance(on_disk, list)
    assert len(on_disk) == 1
    assert on_disk[0]["op"] == "rename"
    assert on_disk[0]["from"] == "a"  # alias preserved


def test_append_record_creates_state_dir_if_missing(tmp_path: Path):
    """First-ever switcher init writes the op-log intent BEFORE _store.create
    makes the state dir, so OpLogIO must create its parent directory."""
    state_dir = tmp_path / "does-not-exist-yet"
    assert not state_dir.exists()
    io = OpLogIO(state_dir)
    io.append_record(_make_rename_op())
    assert state_dir.is_dir()
    assert (state_dir / "oplog.json").exists()


def test_append_record_refuses_when_in_flight_present(tmp_path: Path):
    """Single-in-flight invariant: cannot start a new op while another is
    still in-flight on disk."""
    io = OpLogIO(tmp_path)
    io.append_record(_make_rename_op("a", "b"))
    # Second append, no completion of the first — should refuse.
    with pytest.raises(OpLogCorruptError):
        io.append_record(_make_rename_op("c", "d"))


def test_append_record_rejects_already_completed_record(tmp_path: Path):
    """``append_record`` writes an *intent*; the lifecycle is
    ``append (completed_at=None) → mark_completed``. A caller passing a
    record that already has ``completed_at`` set is API misuse — same
    shape as a stale-reference re-completion through ``mark_completed``,
    which raises ``ValueError`` there. Reject it up front rather than
    silently appending a finished record and letting it skew the
    in-flight / audit accounting.
    """
    record = _make_rename_op()
    # mark_completed (module-level helper) returns an immutable copy
    # with completed_at set — exactly the shape append_record must
    # refuse.
    from switcher.oplog import mark_completed

    completed = mark_completed(record, datetime(2026, 5, 12, 11, 0, tzinfo=UTC))
    io = OpLogIO(tmp_path)
    with pytest.raises(ValueError):
        io.append_record(completed)


def test_append_record_succeeds_after_prior_completed(tmp_path: Path):
    """Once a prior record is marked completed, a new intent can land
    (the completed record is just waiting on the next vacuum)."""
    io = OpLogIO(tmp_path)
    first = _make_rename_op("a", "b")
    io.append_record(first)
    io.mark_completed(first)
    io.append_record(_make_rename_op("c", "d"))
    # Both records present: one completed, one in-flight.
    records = io.read_records()
    assert len(records) == 2
    in_flight = io.read_in_flight()
    assert in_flight is not None
    assert isinstance(in_flight, _RenameOp)
    assert in_flight.to == "d"


def test_read_in_flight_raises_corrupt_on_multiple_uncompleted(tmp_path: Path):
    """Defense in depth: if disk has two in-flight records (which
    append_record won't normally produce, but external manipulation
    could), read_in_flight surfaces the corruption."""
    # Write the corrupt state directly to disk to simulate external
    # manipulation.
    payload = json.dumps(
        [
            {
                "op": "rename",
                "from": "a",
                "to": "b",
                "started_at": "2026-05-12T10:30:00+00:00",
                "affected_ids": [],
            },
            {
                "op": "rename",
                "from": "c",
                "to": "d",
                "started_at": "2026-05-12T10:31:00+00:00",
                "affected_ids": [],
            },
        ]
    )
    (tmp_path / "oplog.json").write_text(payload, encoding="utf-8")
    io = OpLogIO(tmp_path)
    with pytest.raises(OpLogCorruptError):
        io.read_in_flight()


def test_read_in_flight_returns_none_when_empty(tmp_path: Path):
    io = OpLogIO(tmp_path)
    assert io.read_in_flight() is None


def test_read_in_flight_returns_uncompleted_record(tmp_path: Path):
    io = OpLogIO(tmp_path)
    record = _make_rename_op()
    io.append_record(record)
    in_flight = io.read_in_flight()
    assert in_flight is not None
    assert in_flight.op == "rename"
    assert in_flight.completed_at is None


def test_read_in_flight_ignores_completed_records(tmp_path: Path):
    io = OpLogIO(tmp_path)
    record = _make_rename_op()
    io.append_record(record)
    io.mark_completed(record)
    # mark_completed should leave no in-flight records.
    assert io.read_in_flight() is None


def test_mark_completed_raises_corrupt_on_multiple_uncompleted(tmp_path: Path):
    """Single-in-flight is the journal-wide invariant, not just a
    property of read_in_flight. mark_completed reads the raw record
    list, so without an explicit check it would happily complete the
    first match in a corrupt 2-in-flight journal and leave the second
    record dangling — partial "healing" that hides the corruption from
    the user. Refuse instead, matching read_in_flight's behavior.
    """
    payload = json.dumps(
        [
            {
                "op": "rename",
                "from": "a",
                "to": "b",
                "started_at": "2026-05-12T10:30:00+00:00",
                "affected_ids": [],
            },
            {
                "op": "rename",
                "from": "c",
                "to": "d",
                "started_at": "2026-05-12T10:31:00+00:00",
                "affected_ids": [],
            },
        ]
    )
    (tmp_path / "oplog.json").write_text(payload, encoding="utf-8")
    io = OpLogIO(tmp_path)
    # Caller holds a reference to the first record — but the journal
    # itself is corrupt; mark_completed must refuse instead of operating.
    first = _make_rename_op("a", "b")
    with pytest.raises(OpLogCorruptError):
        io.mark_completed(first)


def test_mark_completed_raises_corrupt_on_externally_mutated_record(tmp_path: Path):
    """``mark_completed`` previously matched on disk by ``(op, started_at)``
    only. If something externally rewrote the in-flight record on disk
    while preserving that pair (e.g. flipped ``to`` from ``"b"`` to
    ``"evil"``), the caller's reference and the disk record would
    silently disagree and mark_completed would bless the mutated row
    — the exact "silent acceptance of changed journal contents" the
    immutability/audit guarantee is supposed to prevent. Treat any
    full-record mismatch (across every field except ``completed_at``)
    as corruption.
    """
    io = OpLogIO(tmp_path)
    original = _make_rename_op("a", "b")
    io.append_record(original)
    # External rewrite: same op + started_at, different `to`. Simulates
    # a hostile edit, an aborted concurrent writer, or any other source
    # of journal drift between read and mark_completed.
    mutated_payload = json.dumps(
        [
            {
                "op": "rename",
                "from": "a",
                "to": "evil",  # changed from "b"
                "started_at": "2026-05-12T10:30:00+00:00",
                "affected_ids": [],
            }
        ]
    )
    (tmp_path / "oplog.json").write_text(mutated_payload, encoding="utf-8")
    with pytest.raises(OpLogCorruptError):
        io.mark_completed(original)


def test_mark_completed_clamps_to_started_at_when_clock_skews_back(tmp_path: Path):
    """``datetime.now(UTC)`` can land *before* ``record.started_at`` if
    the host's wall clock moved backwards after the record was
    written — NTP step, manual clock change, VM time correction,
    suspend/resume drift. The ``_check_completion_ordering`` validator
    on ``_BaseOp`` would then reject the completion as "completed_at
    precedes started_at" corruption even though the journal contents
    are valid and the operation succeeded. Clamp to
    ``max(datetime.now(UTC), started_at)`` so clock skew can't strand
    a legitimate in-flight record. Simulated here by recording the
    intent with a ``started_at`` a few minutes in the future relative
    to ``now``.
    """
    future_started = datetime.now(UTC) + timedelta(minutes=5)
    record = _RenameOp.model_validate(
        {
            "op": "rename",
            "started_at": future_started.isoformat(),
            "from": "a",
            "to": "b",
            "affected_ids": [],
        }
    )
    io = OpLogIO(tmp_path)
    io.append_record(record)
    # Should succeed; without the clamp this raises OpLogCorruptError
    # via the _check_completion_ordering validator.
    io.mark_completed(record)
    on_disk: list[dict[str, Any]] = json.loads((tmp_path / "oplog.json").read_text())
    assert on_disk[0]["completed_at"] is not None


def test_mark_completed_sets_timestamp(tmp_path: Path):
    io = OpLogIO(tmp_path)
    record = _make_rename_op()
    io.append_record(record)
    io.mark_completed(record)
    on_disk: list[dict[str, Any]] = json.loads((tmp_path / "oplog.json").read_text())
    assert on_disk[0]["completed_at"] is not None


def test_vacuum_drops_completed_records(tmp_path: Path):
    io = OpLogIO(tmp_path)
    completed = _make_rename_op("a", "b")
    in_flight = _make_rename_op("c", "d")
    io.append_record(completed)
    io.mark_completed(completed)
    io.append_record(in_flight)
    io.vacuum_completed()
    on_disk: list[dict[str, Any]] = json.loads((tmp_path / "oplog.json").read_text())
    assert len(on_disk) == 1
    assert on_disk[0]["from"] == "c"


def test_vacuum_with_only_in_flight_is_noop(tmp_path: Path):
    io = OpLogIO(tmp_path)
    record = _make_rename_op()
    io.append_record(record)
    mtime_before = (tmp_path / "oplog.json").stat().st_mtime_ns
    io.vacuum_completed()
    # If nothing dropped, the file shouldn't be rewritten (mtime unchanged).
    mtime_after = (tmp_path / "oplog.json").stat().st_mtime_ns
    assert mtime_before == mtime_after


@_skip_no_file_symlinks
def test_vacuum_dangling_symlink_raises_corrupt(tmp_path: Path):
    """vacuum_completed must share read_records's corruption surface.
    Its own ``Path.exists()`` early return would otherwise hide a
    dangling oplog.json symlink under the "nothing to vacuum" branch,
    silently recovering from external interference and undercutting
    the read-path policy in a place that's easy to miss.
    """
    (tmp_path / "oplog.json").symlink_to(tmp_path / "does-not-exist")
    io = OpLogIO(tmp_path)
    with pytest.raises(OpLogCorruptError):
        io.vacuum_completed()


def test_vacuum_clears_file_when_all_completed(tmp_path: Path):
    io = OpLogIO(tmp_path)
    record = _make_rename_op()
    io.append_record(record)
    io.mark_completed(record)
    io.vacuum_completed()
    on_disk: list[dict[str, Any]] = json.loads((tmp_path / "oplog.json").read_text())
    assert on_disk == []


def test_write_wraps_oserror_into_storage_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A raw ``OSError`` from the underlying write would surface to
    the CLI as a traceback rather than as a domain error. ``_write_records``
    must wrap filesystem failures (permission denied, ENOSPC, EXDEV)
    into :class:`StorageError` so the CLI's error renderer can present
    them like every other ``SwitcherError`` subclass. Patches
    ``tempfile.mkstemp`` (a real failure point — disk full at temp-
    create time) so the wrap path is exercised without needing a
    deliberately corrupt filesystem.
    """

    def raises_oserror(**_kwargs: object) -> tuple[int, str]:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tempfile, "mkstemp", raises_oserror)
    io = OpLogIO(tmp_path)
    with pytest.raises(StorageError):
        io.append_record(_make_rename_op())


def test_write_resists_pre_existing_tmp_hardlink(tmp_path: Path):
    """Hard links are indistinguishable from regular files via
    ``is_symlink`` / ``isjunction``, so the previous shape-based
    pre-check at a fixed ``oplog.json.tmp`` couldn't defend against
    ``os.link(victim, oplog.json.tmp)`` followed by ``tmp.write_text``
    — the write would clobber the victim's inode through the
    hard-link entry. The fix is structural: never write to a
    predictable name. ``_write_records`` must use
    ``tempfile.mkstemp`` (O_CREAT|O_EXCL with a random suffix) so any
    attacker-planted entry at the old ``oplog.json.tmp`` path is
    simply not touched.
    """
    victim = tmp_path / "victim.txt"
    original_contents = "do not clobber me"
    victim.write_text(original_contents, encoding="utf-8")
    # Pre-place a hard link at the old predictable name.
    os.link(victim, tmp_path / "oplog.json.tmp")
    io = OpLogIO(tmp_path)
    io.append_record(_make_rename_op())
    # Victim's inode must not have been written through the hard link.
    assert victim.read_text(encoding="utf-8") == original_contents
    # The journal landed at the right path with valid JSON.
    on_disk: list[dict[str, Any]] = json.loads(
        (tmp_path / "oplog.json").read_text(encoding="utf-8")
    )
    assert len(on_disk) == 1
    assert on_disk[0]["op"] == "rename"


@_skip_no_file_symlinks
def test_write_resists_pre_existing_tmp_symlink(tmp_path: Path):
    """Sibling shape to the hard-link test above. A pre-placed symlink
    at the old fixed ``oplog.json.tmp`` name must not affect the new
    mkstemp-based write: the actual tmp file lives at a random
    ``oplog.json.<random>.tmp`` name, so the attacker-planted symlink
    is irrelevant and its target is untouched.
    """
    victim = tmp_path / "victim.txt"
    original_contents = "do not clobber me"
    victim.write_text(original_contents, encoding="utf-8")
    (tmp_path / "oplog.json.tmp").symlink_to(victim)
    io = OpLogIO(tmp_path)
    io.append_record(_make_rename_op())
    # Victim file must not have been written through the symlink.
    assert victim.read_text(encoding="utf-8") == original_contents
    # The journal landed at the right path with valid JSON.
    on_disk: list[dict[str, Any]] = json.loads(
        (tmp_path / "oplog.json").read_text(encoding="utf-8")
    )
    assert len(on_disk) == 1


def test_atomic_write_uses_tmp_plus_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """append_record should write via a tmp+rename pattern so a crash
    mid-write can't tear oplog.json."""
    io = OpLogIO(tmp_path)
    rename_calls: list[tuple[str, str]] = []
    real_replace = Path.replace

    def spy_replace(self: Path, target: str | Path) -> Path:
        rename_calls.append((str(self), str(target)))
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", spy_replace)
    io.append_record(_make_rename_op())
    assert rename_calls, "append_record must use atomic tmp+rename"
    src, dst = rename_calls[-1]
    assert dst.endswith("oplog.json")
    # Assert on the filename, not on the full path: a substring check
    # against `src` would match the ``/tmp/...`` directory pytest's
    # tmp_path lives under even if the actual file were misnamed.
    # mkstemp's output shape is ``<prefix><random><suffix>``, so the
    # tmp filename starts with ``oplog.json.`` and ends with ``.tmp``.
    src_name = Path(src).name
    assert src_name.startswith("oplog.json.") and src_name.endswith(".tmp"), src_name
