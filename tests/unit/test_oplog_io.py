# pyright: reportPrivateUsage=none
"""Tests for the OpLogIO class — read, write, append, mark_completed,
vacuum (spec §2.1, §2.6)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from switcher.errors import OpLogCorruptError
from switcher.oplog import (
    OpLogIO,
    _RenameOp,
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


def test_read_records_dangling_symlink_raises_corrupt(tmp_path: Path):
    """``Path.exists()`` follows symlinks, so a dangling symlink at the
    journal path returns False and would otherwise fall through to the
    "no journal" branch — silently recovering from external
    interference. The journal's contract is fail-fast at the corruption
    boundary; surface this as OpLogCorruptError so callers don't
    proceed as if no journal exists and overwrite the evidence.
    """
    (tmp_path / "oplog.json").symlink_to(tmp_path / "does-not-exist")
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
    assert "tmp" in src or src.endswith(".tmp")
