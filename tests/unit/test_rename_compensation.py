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
from switcher.links import remove_link
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
    # Conftest seeds multiple tools (claude/copilot/github-copilot) all
    # at `name` after init. Real `service.rename` would compute
    # affected_ids = every tid with active[tid] == name. Mirror that
    # snapshot in the test record so the new phase-coherence /
    # extra-ref drift guards see a legitimate pre-step-1 state.
    affected = sorted(
        tid for tid, profile in FileProfileStore(tmp_state).get_active().items() if profile == name
    )
    record = _make_record(name, "client-A", affected=affected)
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
    # affected_ids snapshot must cover EVERY tid the real
    # `service.rename` would have captured (extra-refs guard).
    affected = sorted(cfg["active"].keys())
    record = _make_record(name, "client-A", affected=affected)
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
    # affected_ids must mirror every tid that was at `name` pre-rename
    # (every tool the conftest seeded). After service.rename, those
    # entries now point at "client-A"; the journal record's
    # affected_ids snapshot must match the real-rename invariant.
    affected = sorted(FileProfileStore(tmp_state).get_active().keys())
    record = _make_record(name, "client-A", affected=affected)
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
    # affected_ids covers every active tid pointing at `name` —
    # the orphan and the conftest-seeded registered tools (extra-refs
    # guard requires the snapshot to be exhaustive).
    affected = sorted(tid for tid, profile in cfg["active"].items() if profile == name)
    record = _make_record(name, "client-A", affected=affected)
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
    # User intervened: the live link was replaced with a real directory
    # between the crash and our compensation pass. Use the project's
    # link helpers — `Path.is_symlink()` returns False on Windows
    # junctions, so the prior shutil.rmtree path would either delete
    # THROUGH the junction (leaving the junction intact) or refuse,
    # then fail the subsequent mkdir with FileExistsError (Windows CI
    # blocker on 25858754403; CodeRabbit review).
    claude_live = tmp_home / ".claude"
    if service._resolver.is_link(claude_live):
        remove_link(claude_live)
    elif claude_live.is_dir():
        shutil.rmtree(claude_live)
    elif claude_live.exists():
        claude_live.unlink()
    assert not claude_live.exists()
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


def test_compensate_rename_refuses_when_affected_id_drifted_to_third_profile(
    service: ProfileService, tmp_state: Path
) -> None:
    """An affected tool's active entry was externally changed to some
    third profile (not from_ or to). The previous logic silently skipped
    it and still marked the intent completed — clearing the journal
    while leaving the rename only partially reconciled (the third-profile
    pointer survives, the user's rename request is forgotten). Refuse
    loudly instead: this is corruption that the user must resolve
    manually (reviewer convergence: abby blocking, CodeRabbit recurring)."""
    name = service.init().profile_name
    service.create("third-profile")
    store = FileProfileStore(tmp_state)
    store.rename(name, "client-A")
    config_path = tmp_state / "config.json"
    cfg = json.loads(config_path.read_text())
    # claude was managed under `name` originally; simulate external
    # drift: it now points at "third-profile" instead of either
    # rename endpoint.
    cfg["active"] = {"claude": "third-profile"}
    cfg["active_live_paths"] = {}
    config_path.write_text(json.dumps(cfg))
    record = _make_record(name, "client-A", affected=["claude"])
    oplog = OpLogIO(tmp_state)
    oplog.append_record(record)
    with pytest.raises(OpLogCorruptError):
        service._compensate_rename(record)
    # Failure path: intent stays in-flight for manual recovery.
    assert oplog.read_in_flight() is not None
    # active map untouched: still points at the third profile, NOT
    # silently re-pointed at "client-A".
    after = FileProfileStore(tmp_state)
    assert after.get_active().get("claude") == "third-profile"


def test_compensate_rename_drift_check_runs_before_store_rename_replay(
    service: ProfileService, tmp_state: Path
) -> None:
    """When the intent was written but store.rename never ran AND the
    active map has already drifted, the drift check must fire BEFORE
    store.rename is replayed. Otherwise compensation would mutate the
    profile dir (from → to) and only then refuse, turning a detectable
    corruption case into a different partial state (abby blocking
    review). All corruption guards run before any further canonical-
    state mutation."""
    name = service.init().profile_name
    # Stage the pre-step-1 shape: from dir exists, to dir does NOT.
    # The intent says we should roll-forward to `to`, but the active
    # map has drifted to a third profile already.
    service.create("third-profile")
    config_path = tmp_state / "config.json"
    cfg = json.loads(config_path.read_text())
    cfg["active"] = {"claude": "third-profile"}
    cfg["active_live_paths"] = {}
    config_path.write_text(json.dumps(cfg))
    record = _make_record(name, "client-A", affected=["claude"])
    oplog = OpLogIO(tmp_state)
    oplog.append_record(record)
    # Capture profile dir state BEFORE compensation.
    store_before = FileProfileStore(tmp_state)
    assert store_before.profile_dir(name).exists()
    assert not store_before.profile_dir("client-A").exists()

    with pytest.raises(OpLogCorruptError):
        service._compensate_rename(record)

    # Disk-truth invariant: the FROM dir must still exist with its
    # original name; the TO dir must not have been created. The drift
    # check fired before any further FS mutation, so the "intent
    # written, store.rename pending" shape is preserved for the user
    # to investigate.
    store_after = FileProfileStore(tmp_state)
    assert store_after.profile_dir(name).exists()
    assert not store_after.profile_dir("client-A").exists()
    assert oplog.read_in_flight() is not None


def test_compensate_rename_refuses_when_affected_id_missing_from_active(
    service: ProfileService, tmp_state: Path
) -> None:
    """An affected_id captured at intent time was externally removed
    from the active map (e.g. via partial-state corruption, unmanage,
    or hand-edit between crash and compensation). The previous logic
    silently skipped it and marked the intent completed. Refuse loudly."""
    name = service.init().profile_name
    store = FileProfileStore(tmp_state)
    store.rename(name, "client-A")
    config_path = tmp_state / "config.json"
    cfg = json.loads(config_path.read_text())
    # claude was in affected_ids when the intent was written, but the
    # active map no longer has it.
    cfg["active"] = {}
    cfg["active_live_paths"] = {}
    config_path.write_text(json.dumps(cfg))
    record = _make_record(name, "client-A", affected=["claude"])
    oplog = OpLogIO(tmp_state)
    oplog.append_record(record)
    with pytest.raises(OpLogCorruptError):
        service._compensate_rename(record)
    assert oplog.read_in_flight() is not None


def test_compensate_rename_refuses_when_affected_set_split_across_endpoints(
    service: ProfileService, tmp_state: Path
) -> None:
    """`store.set_active` is atomic — a real rename can't produce an
    active map where some affected_ids point at `from_` and others at
    `to`. Reject the split state as corruption (Hermes PR review).
    Stages two managed tools, then hand-edits the active map to put one
    of them at the post-rename name while the other stays at the
    pre-rename name."""
    name = service.init().profile_name
    # Add a second managed tool so affected_ids has >1 entry to split.
    store_pre = FileProfileStore(tmp_state)
    active_pre = dict(store_pre.get_active())
    active_pre["copilot"] = name
    store_pre.set_active(active_pre)
    # Post-step-1 dir state: store.rename has run.
    store_pre.rename(name, "client-A")
    # Split phase: claude at old, copilot at new.
    config_path = tmp_state / "config.json"
    cfg = json.loads(config_path.read_text())
    cfg["active"] = {"claude": name, "copilot": "client-A"}
    cfg["active_live_paths"] = {}
    config_path.write_text(json.dumps(cfg))
    record = _make_record(name, "client-A", affected=["claude", "copilot"])
    oplog = OpLogIO(tmp_state)
    oplog.append_record(record)
    with pytest.raises(OpLogCorruptError, match="split"):
        service._compensate_rename(record)
    assert oplog.read_in_flight() is not None


def test_compensate_rename_refuses_when_from_dir_exists_but_affected_at_to(
    service: ProfileService, tmp_state: Path
) -> None:
    """Phase coherence: if the `from` dir still exists, step (1) of the
    real rename sequence never finished, so step (2) (in-memory
    active rewrite) couldn't have run. Any affected_id already
    pointing at `to` is therefore externally mutated state, not a
    crash window the journal predicted (Hermes PR review)."""
    name = service.init().profile_name
    # Pre-step-1 dir state: `from` dir still exists (no store.rename).
    # But the active map already references `to` for the affected id.
    config_path = tmp_state / "config.json"
    cfg = json.loads(config_path.read_text())
    cfg["active"] = {"claude": "client-A"}  # ahead of dir state
    cfg["active_live_paths"] = {}
    config_path.write_text(json.dumps(cfg))
    # Need profile_dir("client-A") to NOT exist for the from-only branch
    # to engage. (init() only created the current-named profile.)
    record = _make_record(name, "client-A", affected=["claude"])
    oplog = OpLogIO(tmp_state)
    oplog.append_record(record)
    with pytest.raises(OpLogCorruptError):
        service._compensate_rename(record)
    assert oplog.read_in_flight() is not None
    # Source dir untouched: drift guard ran before store.rename replay.
    assert FileProfileStore(tmp_state).profile_dir(name).exists()


def test_compensate_rename_refuses_extra_active_refs_outside_affected_ids(
    service: ProfileService, tmp_state: Path
) -> None:
    """A tool outside record.affected_ids that still references the
    rename's source or target profile is corruption that compensation
    must not silently bless (Hermes PR review robustness gap;
    CodeRabbit recurring). The journal's affected_ids snapshot is what
    the compensation knows to repair; an unexpected extra reference
    would survive the rename pointing at a profile name that's about
    to disappear (or pre-claim the destination)."""
    name = service.init().profile_name
    # Add an unmanaged tool's active entry that points at the same
    # profile but isn't in affected_ids. Hand-edit it in.
    store_pre = FileProfileStore(tmp_state)
    store_pre.rename(name, "client-A")  # post-step-1 dir state
    config_path = tmp_state / "config.json"
    cfg = json.loads(config_path.read_text())
    # claude is in affected_ids; copilot is an unexpected extra ref.
    cfg["active"] = {"claude": name, "copilot": name}
    cfg["active_live_paths"] = {}
    config_path.write_text(json.dumps(cfg))
    record = _make_record(name, "client-A", affected=["claude"])
    oplog = OpLogIO(tmp_state)
    oplog.append_record(record)
    with pytest.raises(OpLogCorruptError, match=r"outside record\.affected_ids"):
        service._compensate_rename(record)
    assert oplog.read_in_flight() is not None


def test_compensate_rename_refuses_when_profile_dir_is_symlink(
    service: ProfileService, tmp_state: Path
) -> None:
    """The profile store invariant is 'profile_dir is a real directory'.
    A symlink (or Windows junction) at profile_dir(from_) — pointing
    anywhere — is external mutation and must not be auto-healed
    (CodeRabbit PR review). is_dir() alone follows symlinks through to
    the target, so we'd otherwise treat a redirected profile dir as
    valid recovery state."""
    name = service.init().profile_name
    # Replace the profile dir with a symlink pointing at the same path's
    # contents copied elsewhere. Easier: just unlink and symlink-to a
    # newly created sibling dir.
    profile_dir = FileProfileStore(tmp_state).profile_dir(name)
    shadow = tmp_state / "shadow"
    shutil.move(str(profile_dir), str(shadow))
    try:
        profile_dir.symlink_to(shadow)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted (likely Windows without Developer Mode)")
    record = _make_record(name, "client-A", affected=["claude"])
    oplog = OpLogIO(tmp_state)
    oplog.append_record(record)
    with pytest.raises(OpLogCorruptError, match="symlink or junction"):
        service._compensate_rename(record)


def test_rename_writes_intent_and_marks_completed(service: ProfileService, tmp_state: Path) -> None:
    """A successful rename appends an intent record and marks it
    completed; vacuum then drops it. End-to-end covers the writer
    side of the op-log integration.

    v0.1.5 PR4: init now also writes+completes an _InitOp record, so
    we vacuum after init to isolate the rename's contribution to the
    journal."""
    name = service.init().profile_name
    OpLogIO(tmp_state).vacuum_completed()  # drop init's completed record
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
    state — abby-review batch-1 pass-2 finding.

    v0.1.5 PR4: init writes+completes its own _InitOp record, so we
    vacuum after init to isolate the rename's contribution."""
    name = service.init().profile_name
    OpLogIO(tmp_state).vacuum_completed()  # drop init's completed record
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
