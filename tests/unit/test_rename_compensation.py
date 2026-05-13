# pyright: reportPrivateUsage=none
"""Tests for rename op-log compensation (spec §2.3)."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from switcher.errors import OpLogCorruptError, PathNotADirectoryError, ProfileExistsError
from switcher.models import Tool
from switcher.oplog import OpLogIO, _RenameOp
from switcher.paths import PathResolver
from switcher.registry import build_registry
from switcher.service import ProfileService
from switcher.store import FileProfileStore


def _now() -> datetime:
    return datetime(2026, 5, 12, 10, 30, tzinfo=UTC)


def _make_record(from_name: str, to_name: str, affected: list[str]) -> _RenameOp:
    # model_validate (rather than the kwargs ctor) lets us pass the JSON-side
    # alias `from` directly without tripping basedpyright on `**{"from": ...}`.
    return _RenameOp.model_validate(
        {
            "op": "rename",
            "started_at": _now(),
            "from": from_name,
            "to": to_name,
            "affected_ids": affected,
        }
    )


@pytest.fixture
def registry() -> tuple[Tool, ...]:
    return tuple(build_registry(Path("/nonexistent")))  # builtins only


@pytest.fixture
def service(tmp_home: Path, tmp_state: Path, registry: tuple[Tool, ...]) -> ProfileService:
    store = FileProfileStore(tmp_state)
    resolver = PathResolver(home=tmp_home)
    return ProfileService(store, resolver, registry)


def test_compensate_rename_rolls_forward_when_intent_written_before_store_rename(
    service: ProfileService, tmp_state: Path
) -> None:
    """Intent record was written but store.rename never ran. Compensation
    rolls forward: runs store.rename itself, then completes normally.

    Critical correctness test — a no-op interpretation here would silently
    drop the user's rename request after the intent record committed to it.
    """
    name = service.init().profile_name
    record = _make_record(name, "client-A", affected=[])
    oplog = OpLogIO(tmp_state)
    oplog.append_record(record)
    service._compensate_rename(record)
    store = FileProfileStore(tmp_state)
    assert not store.profile_dir(name).exists()
    assert store.profile_dir("client-A").exists()
    # Recovery succeeded — the in-flight record must be marked completed
    # so the next vacuum can drop it; otherwise every subsequent CLI
    # command re-enters compensation for the same record.
    assert oplog.read_in_flight() is None


def test_compensate_rename_both_dirs_exist_raises_corrupt(
    service: ProfileService, tmp_state: Path
) -> None:
    """Both `from` and `to` profile dirs exist — could be a partial
    FileProfileStore.rename or external interference. Refuse loudly."""
    name = service.init().profile_name
    service.create("client-A")
    record = _make_record(name, "client-A", affected=[])
    oplog = OpLogIO(tmp_state)
    oplog.append_record(record)
    with pytest.raises(OpLogCorruptError):
        service._compensate_rename(record)
    store = FileProfileStore(tmp_state)
    assert store.profile_dir(name).exists()
    assert store.profile_dir("client-A").exists()
    # Failure path: intent stays in-flight so the next compensation pass
    # (e.g. after the user resolves the ambiguity) can pick it up.
    assert oplog.read_in_flight() is not None


def test_compensate_rename_step1_done_step2_pending(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Crash between store.rename and set_active. Compensation finishes
    set_active and the swap_link loop.

    Stages the FULL post-step-1 state: store.rename has already run (so
    profile dir lives at `client-A`) but the active map still references
    the original name and the live symlink still points at the new
    target (which is the post-rename shape compensation expects).
    """
    name = service.init().profile_name
    store = FileProfileStore(tmp_state)
    # Simulate: store.rename ran but set_active never did.
    store.rename(name, "client-A")
    # Re-write active map to the pre-step-2 shape (still points at `name`).
    config_path = tmp_state / "config.json"
    cfg = json.loads(config_path.read_text())
    cfg["active"] = dict.fromkeys(cfg["active"], name)
    cfg["active_live_paths"] = {}
    config_path.write_text(json.dumps(cfg))
    record = _make_record(name, "client-A", affected=["claude"])
    oplog = OpLogIO(tmp_state)
    oplog.append_record(record)
    service._compensate_rename(record)
    store = FileProfileStore(tmp_state)
    active = store.get_active()
    assert active.get("claude") == "client-A"
    claude_live = tmp_home / ".claude"
    expected = (store.profile_dir("client-A") / "claude").resolve()
    assert claude_live.resolve() == expected
    assert oplog.read_in_flight() is None


def test_compensate_rename_both_dirs_missing_raises_corrupt(
    service: ProfileService, tmp_state: Path
) -> None:
    """Neither `from` nor `to` exists. Refuse loudly — user manually
    intervened or something else went wrong."""
    record = _make_record("a", "b", affected=[])
    oplog = OpLogIO(tmp_state)
    oplog.append_record(record)
    with pytest.raises(OpLogCorruptError):
        service._compensate_rename(record)
    assert oplog.read_in_flight() is not None


def test_compensate_rename_idempotent_when_already_complete(
    service: ProfileService, tmp_state: Path
) -> None:
    """Both store.rename and set_active ran cleanly. Compensation runs
    the swap_link loop (idempotent on already-correct links) and exits.

    The original `service.rename` call already wrote AND completed AND
    vacuumed its own intent record, so the journal is empty at the top
    of this test. Compensation needs its own in-flight record to mark
    completed (mirroring the production flow where the CLI hook reads
    an in-flight record before invoking _compensate_rename); append one
    explicitly to model "user manually triggered re-compensation"."""
    name = service.init().profile_name
    service.rename(name, "client-A")
    OpLogIO(tmp_state).vacuum_completed()
    record = _make_record(name, "client-A", affected=["claude"])
    oplog = OpLogIO(tmp_state)
    oplog.append_record(record)
    service._compensate_rename(record)
    store = FileProfileStore(tmp_state)
    assert store.profile_dir("client-A").exists()
    assert store.get_active().get("claude") == "client-A"
    assert oplog.read_in_flight() is None


def test_compensate_rename_handles_orphan_tool_ids(
    service: ProfileService, tmp_state: Path
) -> None:
    """A tool id in affected_ids that's NOT in the registry must still
    have its active-map entry re-pointed to `to`. Orphan tools skip the
    link-fixup loop (find_tool returns None) but the active-map repoint
    still has to happen so the canonical state stays consistent."""
    name = service.init().profile_name
    store = FileProfileStore(tmp_state)
    store.rename(name, "client-A")
    config_path = tmp_state / "config.json"
    cfg = json.loads(config_path.read_text())
    cfg["active"]["unknown_tool"] = name
    config_path.write_text(json.dumps(cfg))
    record = _make_record(name, "client-A", affected=["unknown_tool"])
    oplog = OpLogIO(tmp_state)
    oplog.append_record(record)
    service._compensate_rename(record)
    store = FileProfileStore(tmp_state)
    assert store.get_active().get("unknown_tool") == "client-A"
    assert oplog.read_in_flight() is None


def test_compensate_rename_refuses_when_live_path_is_real_dir(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Between intent-write and compensation, the user manually recreated a
    real directory at a managed live path (e.g. they ran `mkdir ~/.claude`
    after noticing the symlink was missing post-crash). Compensation must
    refuse BEFORE mutating canonical state — otherwise it would run
    store.rename / set_active and then fail mid-loop on swap_link, leaving
    the active map at `to` while a stale live link still references `from`.

    Same "validate, then mutate" discipline as service.rename's preflight."""
    name = service.init().profile_name
    # Pre-stage the post-step-1 shape and then break the live path.
    store = FileProfileStore(tmp_state)
    store.rename(name, "client-A")
    config_path = tmp_state / "config.json"
    cfg = json.loads(config_path.read_text())
    cfg["active"] = dict.fromkeys(cfg["active"], name)
    cfg["active_live_paths"] = {}
    config_path.write_text(json.dumps(cfg))
    # User intervened: the live symlink was unlinked and replaced with a
    # real directory between the crash and our compensation pass.
    claude_live = tmp_home / ".claude"
    if claude_live.is_symlink():
        claude_live.unlink()
    elif claude_live.exists():
        # Original conftest fixture seeded a real dir; remove the link/dir
        # however it lands so we can pre-stage the bad shape.
        if claude_live.is_dir():
            shutil.rmtree(claude_live)
        else:
            claude_live.unlink()
    claude_live.mkdir()
    record = _make_record(name, "client-A", affected=["claude"])
    oplog = OpLogIO(tmp_state)
    oplog.append_record(record)
    with pytest.raises(PathNotADirectoryError):
        service._compensate_rename(record)
    # Canonical state untouched: store still at `client-A` (where step-1 left
    # it), active still at `name` (the pre-step-2 shape we staged), live still
    # the bad real dir we created.
    after = FileProfileStore(tmp_state)
    assert after.profile_dir("client-A").exists()
    assert after.get_active().get("claude") == name
    assert claude_live.is_dir() and not claude_live.is_symlink()
    # Failure path: intent stays in-flight for the next compensation pass.
    assert oplog.read_in_flight() is not None


def test_rename_writes_intent_and_marks_completed(service: ProfileService, tmp_state: Path) -> None:
    """A successful rename appends an intent record and marks it
    completed; vacuum then drops it. End-to-end covers the writer
    side of the op-log integration."""
    name = service.init().profile_name
    service.rename(name, "client-A")
    oplog = OpLogIO(tmp_state)
    records = oplog.read_records()
    assert len(records) == 1
    assert records[0].op == "rename"
    assert records[0].completed_at is not None
    oplog.vacuum_completed()
    assert oplog.read_records() == []


def test_rename_cancels_intent_on_pre_mutation_exception(
    service: ProfileService, tmp_state: Path
) -> None:
    """A synchronous exception from store.rename's explicit pre-mutation
    guards (UnknownProfileError / ProfileExistsError — raised before any
    FS write) must drop the in-flight intent record. The disk is
    untouched; the next CLI command should NOT auto-compensate work
    that never started.

    Without this, a normal failed rename becomes a persistent recovery
    state — abby-review batch-1 pass-2 finding."""
    name = service.init().profile_name
    # Race a ProfileExistsError out of store.rename: pre-flight passed
    # because `client-A` did not exist, but it appeared between preflight
    # and store.rename. ProfileExistsError raises BEFORE any mutation in
    # store.rename, so cancellation is safe.
    with (
        patch.object(
            service._store,
            "rename",
            side_effect=ProfileExistsError("simulated race"),
        ),
        pytest.raises(ProfileExistsError, match="simulated race"),
    ):
        service.rename(name, "client-A")
    oplog = OpLogIO(tmp_state)
    assert oplog.read_in_flight() is None
    assert oplog.read_records() == []


def test_rename_preserves_intent_on_post_mutation_exception(
    service: ProfileService, tmp_state: Path
) -> None:
    """A synchronous exception that lands AFTER store.rename has already
    mutated persistent state must NOT cancel the intent — compensation
    needs the in-flight record to roll forward from the partial state.

    abby-review batch-1 pass-3 blocking finding: the broad
    `except Exception` originally introduced for pass-2 swallowed the
    recovery record for partial-mutation failures. The narrowed cancel
    path now only fires for store.rename's explicit pre-mutation guards;
    set_active / swap_link / mark_completed failures leave the intent
    intact."""
    name = service.init().profile_name
    # Let store.rename succeed (so persistent state mutates), then force
    # a failure on the very next step (set_active). The intent record
    # must survive so the next CLI command can roll the rename forward.
    with (
        patch.object(
            service._store,
            "set_active",
            side_effect=OSError("simulated post-rename failure"),
        ),
        pytest.raises(OSError, match="simulated post-rename failure"),
    ):
        service.rename(name, "client-A")
    oplog = OpLogIO(tmp_state)
    in_flight = oplog.read_in_flight()
    assert in_flight is not None, (
        "intent must survive mid-mutation failure — compensation needs it "
        "to roll the rename forward"
    )
    assert in_flight.op == "rename"
    # Sanity: store.rename DID happen — the partial-state shape compensation
    # is supposed to handle.
    after = FileProfileStore(tmp_state)
    assert after.profile_dir("client-A").exists()
    assert not after.profile_dir(name).exists()


def test_rename_pre_mutation_cancel_propagates_oplog_corruption(
    service: ProfileService, tmp_state: Path
) -> None:
    """If the journal was externally corrupted between append_record and the
    pre-mutation cancel_intent attempt, the OpLogCorruptError must surface
    — not be silently swallowed by the best-effort suppression. The race
    exception (ProfileExistsError) becomes the implicit __context__ so
    the user has the full picture in the traceback, but corruption is
    the urgent thing to surface — abby-review batch-1 pass-4 finding."""
    name = service.init().profile_name
    # Patch cancel_intent to raise OpLogCorruptError (simulating: external
    # rewrite of the journal between our append_record and our cancel
    # attempt). Patch store.rename to raise ProfileExistsError to drive
    # the pre-mutation branch.
    with (
        patch.object(
            service._store,
            "rename",
            side_effect=ProfileExistsError("simulated race"),
        ),
        patch.object(
            OpLogIO,
            "cancel_intent",
            side_effect=OpLogCorruptError("journal corrupted between append and cancel"),
        ),
        pytest.raises(OpLogCorruptError, match="journal corrupted"),
    ):
        service.rename(name, "client-A")


def test_rename_preserves_intent_when_mark_completed_fails(
    service: ProfileService, tmp_state: Path
) -> None:
    """If oplog.mark_completed itself fails after every rename mutation
    succeeded, the intent record must still survive — same reason as
    post-mutation: compensation can verify the disk state is consistent
    and retry the mark_completed on the next CLI invocation. Cancelling
    here would erase journal evidence of a fully-applied rename and
    leave the user with no recovery path if they did need to audit it."""
    name = service.init().profile_name
    real_mark_completed = OpLogIO.mark_completed

    def fail_mark_completed(self: OpLogIO, *args: object, **kwargs: object) -> None:
        raise OSError("simulated mark_completed failure")

    with (
        patch.object(OpLogIO, "mark_completed", new=fail_mark_completed),
        pytest.raises(OSError, match="simulated mark_completed failure"),
    ):
        service.rename(name, "client-A")
    # Restore the real implementation so the assertion below uses it.
    OpLogIO.mark_completed = real_mark_completed  # type: ignore[method-assign]
    oplog = OpLogIO(tmp_state)
    in_flight = oplog.read_in_flight()
    assert in_flight is not None
    assert in_flight.op == "rename"
