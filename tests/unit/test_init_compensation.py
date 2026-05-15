# pyright: reportPrivateUsage=none
"""Tests for init op-log compensation (spec §2.2).

`service.init(continue_=True)` and `service.init(abort=True)` drive
disk-truth compensation against the §2.1.1 four-state classifier. The
shared fixtures (`tmp_home`, `tmp_state`, `service`) come from
conftest.py — same pattern as test_rename_compensation.py."""

from __future__ import annotations

import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from switcher.errors import (
    AbortPreflightError,
    NoInProgressInitError,
    OpLogCorruptError,
    ProfileExistsError,
    RescanInProgressError,
    StorageError,
)
from switcher.models import Tool
from switcher.oplog import OpLogIO, _InitOp, _MappingIntent, _RenameOp, _RescanOp
from switcher.paths import PathResolver
from switcher.registry import build_registry
from switcher.service import InitAlreadyCompletedReport, ProfileService
from switcher.store import FileProfileStore


def _now() -> datetime:
    return datetime(2026, 5, 12, 10, 30, tzinfo=UTC)


def _symlink_dir(target: Path, live: Path) -> None:
    """Create ``live`` → ``target`` symlink with the cross-platform
    ``target_is_directory`` kwarg Windows needs and POSIX ignores.

    ``Path.symlink_to`` is the project-preferred shape (see tests/unit/
    test_rename_compensation.py for the same usage)."""
    live.symlink_to(target, target_is_directory=(sys.platform == "win32"))


def _claude_mapping(tmp_home: Path, original_kind: str) -> _MappingIntent:
    return _MappingIntent.model_validate(
        {
            "tool_id": "claude",
            "mapping_index": 0,
            "live_path": str(tmp_home / ".claude"),
            "profile_subdir": "claude",
            "original_kind": original_kind,
        }
    )


def _copilot_mapping(tmp_home: Path, original_kind: str) -> _MappingIntent:
    return _MappingIntent.model_validate(
        {
            "tool_id": "copilot",
            "mapping_index": 0,
            "live_path": str(tmp_home / ".copilot"),
            "profile_subdir": "copilot-config",
            "original_kind": original_kind,
        }
    )


def _make_init_record(
    profile_name: str, target_ids: list[str], mappings: list[_MappingIntent]
) -> _InitOp:
    return _InitOp.model_validate(
        {
            "op": "init",
            "started_at": _now(),
            "target_ids": target_ids,
            "profile_name": profile_name,
            "mappings": mappings,
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


# -- continue-path -----------------------------------------------------------


def test_continue_all_untouched_runs_full_replay(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Crash before step 5 ran for any tool. Continue must replay the
    full move_or_seed_dir + swap_link pair for every UNTOUCHED mapping
    and finish steps 6-7 (vanilla profile + active map)."""
    claude_live = tmp_home / ".claude"
    (claude_live / "settings.json").write_text('{"original": true}')
    # Plant the dated-current profile via store.create (writes the dir
    # AND metadata.json — matches what init step 4 produces). The
    # compensation refusal added in CR pass-10 / abby pass-10 requires
    # readable metadata; mkdir-only scaffolds would surface as
    # OpLogCorruptError instead of running the test's replay path.
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    store.create(profile_name, {"claude": True})
    profile_dir = store.profile_dir(profile_name)
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    service.init(continue_=True)

    # service._resolver.is_link is the project convention for "managed
    # link" (covers POSIX symlinks AND Windows junctions). Path.is_symlink
    # alone would fail on Windows because swap_link creates a junction,
    # not a symbolic link — Windows CI failure + Hermes pass-PR blocker.
    assert service._resolver.is_link(claude_live)
    assert (profile_dir / "claude" / "settings.json").read_text() == '{"original": true}'
    assert store.get_active().get("claude") == profile_name
    assert OpLogIO(tmp_state).read_in_flight() is None


def test_continue_recreates_profile_when_crash_was_before_first_store_create(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Earliest crash window: ``oplog.append_record(intent)`` succeeded
    but the first ``_store.create(record.profile_name, ...)`` never ran
    (kill-9 between those two steps). ``profile_dir`` does NOT exist
    on disk.

    Continue must recreate the profile via ``_store.create`` before
    replaying mappings — otherwise the mapping replay would mkdir
    ``profile_dir/<subdir>`` (via move_or_seed_dir) without writing
    ``metadata.json``, leaving ``active[tid] = record.profile_name``
    pointing at a dir that ``_store.get`` can't load. CR pass-PR-1
    critical: the user would dead-end on the next ``switcher use``
    with UnknownProfileError, journal already cleared.
    """
    claude_live = tmp_home / ".claude"
    (claude_live / "settings.json").write_text('{"original": true}')
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    # NO store.create — simulating the crash BEFORE step 4 ran.
    # profile_dir does not exist on disk yet.
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)
    assert not store.profile_dir(profile_name).exists()  # precondition

    service.init(continue_=True)

    # Profile recreated through the canonical store API: dir + metadata.
    profile = store.get(profile_name)
    assert profile.name == profile_name
    assert profile.tools == {"claude": True}
    # Mappings replayed: live → managed link, target populated.
    profile_dir = store.profile_dir(profile_name)
    assert service._resolver.is_link(claude_live)
    assert (profile_dir / "claude" / "settings.json").read_text() == '{"original": true}'
    # Active map covers target_ids, pointing at the now-existent profile.
    assert store.get_active().get("claude") == profile_name
    # Journal cleaned up.
    assert OpLogIO(tmp_state).read_in_flight() is None


def test_continue_skips_already_complete_mapping(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Mapping is already COMPLETE (live is a symlink to a populated
    target). Continue must NOT re-run move_or_seed_dir — that would
    fail because target already exists."""
    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    store.create(profile_name, {"claude": True})
    profile_dir = store.profile_dir(profile_name)
    target = profile_dir / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"captured": true}')
    _symlink_dir(target, claude_live)
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    service.init(continue_=True)

    assert claude_live.is_symlink()
    assert (target / "settings.json").read_text() == '{"captured": true}'
    assert OpLogIO(tmp_state).read_in_flight() is None


def test_continue_handles_move_done_link_missing_state(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """The SIGKILL window between move_or_seed_dir and swap_link: target
    populated, live absent entirely. Continue must run swap_link only —
    re-running move would fail because target already exists."""
    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    store.create(profile_name, {"claude": True})
    profile_dir = store.profile_dir(profile_name)
    target = profile_dir / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"already_moved": true}')
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    service.init(continue_=True)

    # Junction-aware check (same Windows-CI / Hermes rationale as
    # test_continue_all_untouched_runs_full_replay).
    assert service._resolver.is_link(claude_live)
    assert (target / "settings.json").read_text() == '{"already_moved": true}'
    assert OpLogIO(tmp_state).read_in_flight() is None


def test_continue_refuses_on_ambiguous_mapping(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """First-pass scan: any AMBIGUOUS mapping → OpLogCorruptError, no
    mutation. Here live is a real dir AND target is a populated real
    dir — data in two places."""
    claude_live = tmp_home / ".claude"
    (claude_live / "settings.json").write_text('{"live": true}')
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    profile_dir = store.profile_dir(profile_name)
    target = profile_dir / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"target": true}')
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    with pytest.raises(OpLogCorruptError):
        service.init(continue_=True)

    # No mutation: both still in place; intent still in flight.
    assert claude_live.is_dir()
    assert not claude_live.is_symlink()
    assert (claude_live / "settings.json").read_text() == '{"live": true}'
    assert (target / "settings.json").read_text() == '{"target": true}'
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_continue_with_no_in_flight_raises_no_in_progress(
    service: ProfileService,
) -> None:
    """`switcher init --continue` against an empty journal → NoInProgressInitError."""
    with pytest.raises(NoInProgressInitError):
        service.init(continue_=True)


def _rescan_in_flight_record() -> _RescanOp:
    """Shared in-flight record for the rescan-routing tests below.
    PR4 ships init's --continue/--abort; rescan's matching flags land
    in PR5 (Phase 7), so the error message names the COMMAND but not
    specific flags. CR pass-6 major carry-forward; CR pass-PR-1 nit
    (test-split) means continue and abort get their own test bodies."""
    return _RescanOp.model_validate(
        {
            "op": "rescan",
            "started_at": _now(),
            "target_ids": ["claude"],
            "target_profiles": {"claude": "2026-05-12-rescan-1"},
            "into_mode": False,
            "previous_tools": None,
            "mappings": [],
        }
    )


def test_continue_routes_user_to_rescan_when_in_flight_is_rescan(
    service: ProfileService, tmp_state: Path
) -> None:
    """`switcher init --continue` with an in-flight ``_RescanOp`` raises
    RescanInProgressError pointing at the rescan recovery surface (by
    command name, not by flag — PR4 doesn't ship rescan's flags)."""
    OpLogIO(tmp_state).append_record(_rescan_in_flight_record())

    with pytest.raises(RescanInProgressError) as excinfo:
        service.init(continue_=True)
    msg = str(excinfo.value)
    assert "rescan" in msg.lower()
    assert "--continue" not in msg
    assert "--abort" not in msg
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_abort_routes_user_to_rescan_when_in_flight_is_rescan(
    service: ProfileService, tmp_state: Path
) -> None:
    """`switcher init --abort` with an in-flight ``_RescanOp`` raises
    RescanInProgressError — same routing as --continue, symmetric
    behavior. Separate test body per CR pass-PR-1 test-split nit."""
    OpLogIO(tmp_state).append_record(_rescan_in_flight_record())

    with pytest.raises(RescanInProgressError) as excinfo:
        service.init(abort=True)
    msg = str(excinfo.value)
    assert "rescan" in msg.lower()
    assert "--continue" not in msg
    assert "--abort" not in msg
    assert OpLogIO(tmp_state).read_in_flight() is not None


def _rename_in_flight_record() -> _RenameOp:
    """Shared in-flight record for the OpLogCorrupt tests below.
    Reaching this state means a prior CLI command's detection hook
    (which auto-compensates rename) didn't run — stale binary or
    hand-edited journal. The defensive refusal is in service.init's
    continue/abort dispatch table."""
    return _RenameOp.model_validate(
        {
            "op": "rename",
            "started_at": _now(),
            "from": "old",
            "to": "new",
            "affected_ids": ["claude"],
        }
    )


def test_continue_refuses_with_oplog_corrupt_on_unexpected_in_flight_type(
    service: ProfileService, tmp_state: Path
) -> None:
    """`switcher init --continue` with an in-flight ``_RenameOp``
    refuses loudly with OpLogCorruptError — the auto-compensation
    contract for rename was violated, so manual recovery is required."""
    OpLogIO(tmp_state).append_record(_rename_in_flight_record())

    with pytest.raises(OpLogCorruptError, match="unexpected in-flight op type"):
        service.init(continue_=True)
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_abort_refuses_with_oplog_corrupt_on_unexpected_in_flight_type(
    service: ProfileService, tmp_state: Path
) -> None:
    """`switcher init --abort` with an in-flight ``_RenameOp`` refuses
    with OpLogCorruptError — symmetric to --continue. Separate test
    body per CR pass-PR-1 test-split nit."""
    OpLogIO(tmp_state).append_record(_rename_in_flight_record())

    with pytest.raises(OpLogCorruptError, match="unexpected in-flight op type"):
        service.init(abort=True)
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_continue_and_abort_mutually_exclusive_at_service_layer(
    service: ProfileService,
) -> None:
    """Defense-in-depth: the service-level mutex protects direct
    callers that bypass the CLI. Without it, ``continue_=True`` AND
    ``abort=True`` would silently fall through to the continue branch
    (because the dispatch is ``if continue_: ... else: ...``) — exactly
    the silent-misroute abby flagged."""
    with pytest.raises(ValueError, match="mutually exclusive"):
        service.init(continue_=True, abort=True)


def test_continue_rejects_call_site_target_ids(
    service: ProfileService,
) -> None:
    """Recovery scope comes from the in-flight journal record, NOT
    the call site. Combining ``continue_=True`` with ``target_ids``
    is a caller bug — silently ignoring the filter would let the
    caller believe recovery is scoped when it actually compensates
    the full journal entry. The service refuses with ValueError."""
    with pytest.raises(ValueError, match="continue_/abort cannot be combined"):
        service.init(target_ids=["claude"], continue_=True)


@pytest.mark.parametrize(
    "kwarg_name",
    [
        "requested_but_not_installed",
        "skipped_via_skip_flag",
        "skipped_via_interactive",
    ],
)
def test_abort_rejects_each_call_site_diff_list_arg(
    service: ProfileService, kwarg_name: str
) -> None:
    """Symmetric to ``target_ids`` rejection: the three informational
    diff lists are call-site context for fresh-init reporting and
    have no meaning during recovery. Passing them with
    ``abort=True`` would silently discard the data. Parametrized per
    behavior-named convention (CR pass-PR-2 refactor)."""
    extra_kwargs: dict[str, object] = {kwarg_name: ["foo"]}
    with pytest.raises(ValueError, match="continue_/abort cannot be combined"):
        service.init(abort=True, **extra_kwargs)  # pyright: ignore[reportCallIssue, reportArgumentType]


# -- writer-side journal hygiene ---------------------------------------------


def test_init_writes_intent_and_marks_completed(service: ProfileService, tmp_state: Path) -> None:
    """Happy path: a fresh ``service.init()`` writes an ``_InitOp``
    intent record before any FS mutation, then marks it completed
    after the final ``set_active_state``. ``vacuum_completed`` drops
    the completed record afterwards.

    Mirrors ``test_rename_writes_intent_and_marks_completed`` —
    catches regressions to the writer-side journaling that the
    compensation matrix tests would only surface obliquely (by
    accidentally seeing one journal record instead of zero)."""
    service.init()

    oplog = OpLogIO(tmp_state)
    records = oplog.read_records()
    assert len(records) == 1
    assert records[0].op == "init"
    assert records[0].completed_at is not None
    oplog.vacuum_completed()
    assert oplog.read_records() == []


def test_init_cancels_intent_on_pre_mutation_create_failure(
    service: ProfileService, tmp_state: Path
) -> None:
    """A ``ProfileExistsError`` from ``_store.create(current_name, ...)``
    is a pre-mutation failure: the disk is untouched (store.create's
    existence check raises BEFORE mkdir). The in-flight intent must
    be canceled so a retry isn't blocked behind a "compensate the init
    that never started" prompt.

    Mirrors ``test_rename_cancels_intent_on_pre_mutation_exception`` —
    same class of failure, same cancel_intent contract."""
    with (
        patch.object(
            service._store,
            "create",
            side_effect=ProfileExistsError("simulated race"),
        ),
        pytest.raises(ProfileExistsError, match="simulated race"),
    ):
        service.init()
    oplog = OpLogIO(tmp_state)
    assert oplog.read_in_flight() is None
    assert oplog.read_records() == []


def test_init_preserves_intent_on_post_mutation_failure(
    service: ProfileService, tmp_state: Path
) -> None:
    """A failure AFTER ``_store.create(current_name, ...)`` already
    mutated FS state (here: a ``_capture_tool`` OSError) must NOT
    cancel the intent — compensation needs the in-flight record to
    drive the recovery surface. The cancel scope is narrow on purpose:
    only the pre-mutation ProfileExistsError window triggers it."""
    real_capture = service._capture_tool
    call_count = 0

    def _failing_capture(profile: str, tool: object) -> list[str]:
        # First call runs the real capture so post-mutation state lands
        # on disk; the second call fails to exit init mid-loop. Without
        # the real mutation on call 1, the test would be exercising the
        # pre-mutation path again.
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return real_capture(profile, tool)  # type: ignore[arg-type]
        raise OSError("simulated post-create failure")

    with (
        patch.object(service, "_capture_tool", side_effect=_failing_capture),
        pytest.raises(OSError, match="simulated post-create failure"),
    ):
        service.init()
    oplog = OpLogIO(tmp_state)
    in_flight = oplog.read_in_flight()
    assert in_flight is not None, (
        "intent must survive a post-mutation failure — compensation "
        "needs it to drive --continue / --abort"
    )
    assert in_flight.op == "init"


# -- abort-path --------------------------------------------------------------


def test_abort_complete_mapping_real_dir(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Abort COMPLETE + original_kind=real-dir: remove the symlink, move
    target contents back to live, delete the partial profile dir."""
    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    store.create(profile_name, {"claude": True})
    profile_dir = store.profile_dir(profile_name)
    target = profile_dir / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"original": true}')
    _symlink_dir(target, claude_live)
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    service.init(abort=True)

    assert claude_live.is_dir()
    assert not claude_live.is_symlink()
    assert (claude_live / "settings.json").read_text() == '{"original": true}'
    assert not profile_dir.exists()
    assert OpLogIO(tmp_state).read_in_flight() is None


def test_abort_complete_mapping_missing_empty_target(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Abort COMPLETE + original_kind=missing + empty target: remove
    the symlink, drop the empty target subdir, live stays absent."""
    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    store.create(profile_name, {"claude": True})
    profile_dir = store.profile_dir(profile_name)
    target = profile_dir / "claude"
    target.mkdir(parents=True)
    _symlink_dir(target, claude_live)
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "missing")])
    OpLogIO(tmp_state).append_record(record)

    service.init(abort=True)

    assert not claude_live.exists()
    assert not profile_dir.exists()
    assert OpLogIO(tmp_state).read_in_flight() is None


def test_abort_complete_mapping_missing_nonempty_target_refuses(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Abort COMPLETE + original_kind=missing + NON-empty target →
    OpLogCorruptError, no mutation. This is the write-through guard —
    data may have been written through the symlink between init's swap
    and abort, and a silent delete would lose it (§2.1.1 invariant #1)."""
    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    store.create(profile_name, {"claude": True})
    profile_dir = store.profile_dir(profile_name)
    target = profile_dir / "claude"
    target.mkdir(parents=True)
    (target / "user_wrote_through.json").write_text('{"data": "important"}')
    _symlink_dir(target, claude_live)
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "missing")])
    OpLogIO(tmp_state).append_record(record)

    with pytest.raises(OpLogCorruptError):
        service.init(abort=True)

    # Data preserved; intent stays in flight for manual recovery.
    assert (target / "user_wrote_through.json").read_text() == '{"data": "important"}'
    assert profile_dir.exists()
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_abort_move_done_link_missing_real_dir(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Abort MOVE_DONE_LINK_MISSING + original_kind=real-dir: move
    target contents back to live (no symlink to remove — the crash
    happened between move and swap_link)."""
    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    store.create(profile_name, {"claude": True})
    profile_dir = store.profile_dir(profile_name)
    target = profile_dir / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"original": true}')
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    service.init(abort=True)

    assert claude_live.is_dir()
    assert not claude_live.is_symlink()
    assert (claude_live / "settings.json").read_text() == '{"original": true}'
    assert not profile_dir.exists()
    assert OpLogIO(tmp_state).read_in_flight() is None


def test_abort_untouched_skips(service: ProfileService, tmp_home: Path, tmp_state: Path) -> None:
    """Abort UNTOUCHED: nothing to reverse per-mapping, but the empty
    dated-current profile dir (created in init step 4) must still go."""
    claude_live = tmp_home / ".claude"
    (claude_live / "settings.json").write_text('{"original": true}')
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    store.create(profile_name, {"claude": True})
    profile_dir = store.profile_dir(profile_name)
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    service.init(abort=True)

    assert (claude_live / "settings.json").read_text() == '{"original": true}'
    assert not profile_dir.exists()
    assert OpLogIO(tmp_state).read_in_flight() is None


def test_abort_validates_all_mappings_before_mutating_any(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Correctness invariant: if mapping[0] is reversible and mapping[1]
    is `original_kind="missing"` with a non-empty target, the write-
    through check on mapping[1] must fail BEFORE mapping[0]'s reversal
    runs. Otherwise abort partially mutates before discovering ambiguity."""
    claude_live = tmp_home / ".claude"
    copilot_live = tmp_home / ".copilot"
    shutil.rmtree(claude_live)
    shutil.rmtree(copilot_live)
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    store.create(profile_name, {"claude": True, "copilot": True})
    profile_dir = store.profile_dir(profile_name)
    claude_target = profile_dir / "claude"
    claude_target.mkdir(parents=True)
    (claude_target / "claude-data.json").write_text('{"original": true}')
    _symlink_dir(claude_target, claude_live)
    copilot_target = profile_dir / "copilot-config"
    copilot_target.mkdir(parents=True)
    (copilot_target / "wrote-through.json").write_text('{"data": "important"}')
    _symlink_dir(copilot_target, copilot_live)
    record = _make_init_record(
        profile_name,
        ["claude", "copilot"],
        [
            _claude_mapping(tmp_home, "real-dir"),
            _copilot_mapping(tmp_home, "missing"),
        ],
    )
    OpLogIO(tmp_state).append_record(record)

    with pytest.raises(OpLogCorruptError):
        service.init(abort=True)

    # mapping[0] was NOT mutated — symlink still points at the partial
    # profile dir, claude-data still in target.
    assert claude_live.is_symlink(), (
        "mapping[0] was mutated before mapping[1]'s validation failure — "
        "partial-mutation invariant violated"
    )
    assert (claude_target / "claude-data.json").read_text() == '{"original": true}'
    # Write-through data preserved.
    assert (copilot_target / "wrote-through.json").read_text() == '{"data": "important"}'
    # Profile dir still on disk; intent still in flight.
    assert profile_dir.exists()
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_continue_short_circuit_returns_already_completed_report(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """``init(continue_=True)`` short-circuit returns
    ``InitAlreadyCompletedReport`` (kind="continue") rather than
    ``None`` — same observability contract as the abort short-circuit,
    so the CLI can print "Init was already committed; journal cleaned
    up" rather than the generic "continue completed" message."""
    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    # Go through store.create so metadata.json is written — matches what
    # a clean init produces and satisfies _check_init_already_completed's
    # metadata-readability gate. Previously mkdir'd profile_dir directly,
    # which (post-CR-pass-7) the short-circuit now correctly rejects as
    # "current profile metadata not readable".
    store.create(profile_name, {"claude": True})
    profile_dir = store.profile_dir(profile_name)
    target = profile_dir / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"committed": true}')
    _symlink_dir(target, claude_live)
    store.create("vanilla", {"claude": True})
    store.set_active_state({"claude": profile_name}, {"claude": [str(claude_live)]})
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    result = service.init(continue_=True)

    assert isinstance(result, InitAlreadyCompletedReport)
    assert result.kind == "continue"
    assert result.profile_name == profile_name
    # Short-circuit also marks the record completed (the journal-cleanup
    # step that distinguishes "committed but log-unmarked" from "fully
    # interrupted"). Without this assertion, a regression that skipped
    # mark_completed on the short-circuit branch would still pass the
    # InitAlreadyCompletedReport assertions above. CR pass-PR-3 nit.
    assert OpLogIO(tmp_state).read_in_flight() is None


def test_abort_short_circuits_when_already_completed(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """The "committed but log-unmarked" short-circuit
    (``_check_init_already_completed``) applies to BOTH ``--continue``
    AND ``--abort``: a crash between the final ``set_active_state``
    and ``mark_completed`` leaves the on-disk state already consistent,
    so the recovery flag's exact value doesn't matter — both paths
    should mark the record completed and skip per-mapping mutation.

    Regression guard: a future change that splits the short-circuit
    between continue and abort would silently reverse a committed init.
    """
    # Set up the post-set_active_state-pre-mark_completed shape: live
    # is a symlink to a populated target, vanilla profile exists,
    # active map covers target_ids.
    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    # Same store.create-for-metadata reason as
    # test_continue_short_circuit_returns_already_completed_report.
    store.create(profile_name, {"claude": True})
    profile_dir = store.profile_dir(profile_name)
    target = profile_dir / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"committed": true}')
    _symlink_dir(target, claude_live)
    # Vanilla profile is part of the "fully completed" shape — without
    # it, _check_init_already_completed correctly returns False and we
    # fall through to compensation. See
    # test_continue_does_not_short_circuit_when_vanilla_is_missing.
    store.create("vanilla", {"claude": True})
    store.set_active_state({"claude": profile_name}, {"claude": [str(claude_live)]})
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    result = service.init(abort=True)

    # The short-circuit returns an InitAlreadyCompletedReport so the
    # CLI can disambiguate from a successful rollback. Without this,
    # `init --abort` would look like a successful rollback while the
    # init is still in place — abby-review pass-15 blocker.
    assert isinstance(result, InitAlreadyCompletedReport)
    assert result.kind == "abort"
    assert result.profile_name == profile_name
    # Abort did NOT run: live still symlinked, target data intact,
    # profile dir present, active map untouched.
    assert claude_live.is_symlink()
    assert (target / "settings.json").read_text() == '{"committed": true}'
    assert profile_dir.exists()
    assert store.profile_dir("vanilla").is_dir()
    assert store.get_active().get("claude") == profile_name
    # Journal: record marked completed (next vacuum drops it).
    assert OpLogIO(tmp_state).read_in_flight() is None


def test_check_does_not_short_circuit_when_current_profile_has_foreign_tools(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Hermes pass-PR-3 blocker: ``_check_init_already_completed`` must
    verify that ``profile.tools == dict.fromkeys(record.target_ids,
    True)`` for ``record.profile_name``, not just that metadata is
    readable. Otherwise a foreign-but-readable profile occupying the
    recovery pathname satisfies the short-circuit (mappings COMPLETE,
    active matches, cache matches, both dirs readable) and the
    journal gets cleared while ``store.get(profile_name).tools``
    still says the profile manages an unrelated tool set.

    Without the fix: short-circuit fires, ``mark_completed`` runs,
    journal cleared, persisted state internally inconsistent.
    With the fix: short-circuit returns False, compensation's
    pass-PR-2 tools-equality refusal fires loudly, journal preserved.
    """
    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    # Foreign current profile: tools={"external": True}, NOT matching
    # record.target_ids=["claude"].
    store.create(profile_name, {"external": True})
    profile_dir = store.profile_dir(profile_name)
    target = profile_dir / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"committed": true}')
    _symlink_dir(target, claude_live)
    store.create("vanilla", {"claude": True})
    store.set_active_state({"claude": profile_name}, {"claude": [str(claude_live)]})
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    with pytest.raises(OpLogCorruptError):
        service.init(continue_=True)

    # Journal stays in flight — short-circuit did NOT fire.
    assert OpLogIO(tmp_state).read_in_flight() is not None
    # Foreign metadata preserved.
    assert store.get(profile_name).tools == {"external": True}


def test_check_does_not_short_circuit_when_vanilla_has_foreign_tools(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Hermes pass-PR-3 blocker, vanilla variant: same gap exists for
    ``vanilla``. A foreign-but-readable vanilla profile satisfying
    other short-circuit invariants would have its journal cleared
    while the persisted ``vanilla.tools`` stays foreign — and a
    subsequent ``switcher use vanilla --only X`` would surface
    ``ToolNotInProfileError`` on tools the user expected to be
    managed.
    """
    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    # Current profile matches; vanilla is foreign.
    store.create(profile_name, {"claude": True})
    profile_dir = store.profile_dir(profile_name)
    target = profile_dir / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"committed": true}')
    _symlink_dir(target, claude_live)
    store.create("vanilla", {"unrelated_tool": True})
    store.set_active_state({"claude": profile_name}, {"claude": [str(claude_live)]})
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    with pytest.raises(OpLogCorruptError):
        service.init(continue_=True)

    # Journal preserved + foreign vanilla untouched.
    assert OpLogIO(tmp_state).read_in_flight() is not None
    assert store.get("vanilla").tools == {"unrelated_tool": True}


def test_continue_refuses_when_existing_vanilla_has_foreign_tools(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """CR pass-PR-3 major: continue's compensation pass-PR-2 added
    a tools-equality check for ``record.profile_name`` but missed
    the symmetric check for ``vanilla``. Without it, continue's
    vanilla branch (which only verified metadata readability) would
    fall through to ``_seed_credentials``, overwriting any foreign
    credentials into a profile whose ``.tools`` doesn't list this
    init's target_ids.

    Trigger setup deliberately fails the short-circuit (mappings
    UNTOUCHED, no active map) so compensation runs and the vanilla
    branch is reached.
    """
    claude_live = tmp_home / ".claude"
    (claude_live / "settings.json").write_text('{"original": true}')
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    store.create(profile_name, {"claude": True})
    # Foreign vanilla: tools doesn't match record.target_ids.
    store.create("vanilla", {"unrelated_tool": True})
    foreign_creds = store.profile_dir("vanilla") / "unrelated_tool" / "creds.json"
    foreign_creds.parent.mkdir(parents=True)
    foreign_creds.write_text('{"preserve": true}')
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    with pytest.raises(OpLogCorruptError, match="vanilla"):
        service.init(continue_=True)

    # Foreign vanilla preserved end-to-end (NOT credential-seeded).
    assert store.get("vanilla").tools == {"unrelated_tool": True}
    assert foreign_creds.read_text() == '{"preserve": true}'
    # Journal stays.
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_check_does_not_short_circuit_when_vanilla_metadata_unreadable(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """``_check_init_already_completed`` MUST treat a vanilla profile
    with missing/corrupt metadata.json as "not fully completed" — the
    short-circuit fires only when the dir-mkdir window of
    ``_store.create("vanilla", ...)`` survived AND the rest of init
    completed. Without the metadata check, the short-circuit would
    bless an empty-leftover vanilla dir as a healthy profile and
    mark the journal completed, blocking the empty-leftover repair
    path in ``_compensate_init_continue``. CR pass-5 major
    (narrower than the pass-2/pass-3 broader metadata concern; scoped
    here to vanilla where compensation CAN heal — the current
    profile remains pushed back because compensation cannot
    regenerate user-data-bearing metadata)."""
    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    # Create the dated-current profile properly so its metadata.json
    # exists — without this, the new pass-7 check fires on the current
    # profile and the test becomes a false-positive for the vanilla
    # branch (abby pass-8 blocker). Sabotaging ONLY vanilla isolates
    # the vanilla-specific path.
    store.create(profile_name, {"claude": True})
    profile_dir = store.profile_dir(profile_name)
    target = profile_dir / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"committed": true}')
    _symlink_dir(target, claude_live)
    store.create("vanilla", {"claude": True})
    store.set_active_state({"claude": profile_name}, {"claude": [str(claude_live)]})
    # Sabotage ONLY vanilla's metadata.json to simulate the
    # rmtree-silent-failure window. Vanilla dir survives empty;
    # current profile remains fully healthy.
    (store.profile_dir("vanilla") / "metadata.json").unlink()
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    # Continue must fall through to _compensate_init_continue, where
    # the empty-leftover repair path regenerates vanilla via store.create.
    service.init(continue_=True)

    # Healed: vanilla metadata.json was rewritten through the canonical
    # store API.
    profile = store.get("vanilla")
    assert profile.name == "vanilla"
    # Journal record marked completed by compensation's tail.
    assert OpLogIO(tmp_state).read_in_flight() is None


def test_continue_refuses_and_keeps_journal_when_current_profile_metadata_missing(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """abby pass-10 blocker: the pass-7 short-circuit check keeps the
    journal record in flight on missing current-profile metadata, but
    that's only half the story — compensation must ALSO refuse, or
    the recovery path runs idempotent no-op compensation and clears
    the journal anyway, leaving the user dead-ended.

    Concrete trigger:
    - crash window: mappings COMPLETE, set_active_state done,
      mark_completed NOT yet run.
    - record.profile_name's metadata.json is missing/corrupt
      (rmtree-silent-failure on _store.create's rollback).
    - user runs `switcher init --continue`.

    Expected: OpLogCorruptError raised in compensation's validation
    pass; journal stays in flight; user can manually delete the
    broken profile dir and retry.
    """
    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    # Build the "completed except metadata" state via store.create
    # (writes metadata), then sabotage it.
    store.create(profile_name, {"claude": True})
    profile_dir = store.profile_dir(profile_name)
    target = profile_dir / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"committed": true}')
    _symlink_dir(target, claude_live)
    store.create("vanilla", {"claude": True})
    store.set_active_state({"claude": profile_name}, {"claude": [str(claude_live)]})
    # Sabotage current-profile metadata only.
    (profile_dir / "metadata.json").unlink()
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    with pytest.raises(OpLogCorruptError, match="metadata"):
        service.init(continue_=True)
    # Journal stays in flight — user can fix the dir manually and retry.
    assert OpLogIO(tmp_state).read_in_flight() is not None
    # No mark_completed reached, so the disk state is preserved as-is
    # (don't mutate when the recovery path can't actually heal).
    assert profile_dir.is_dir()
    assert claude_live.is_symlink()


def test_continue_refusal_with_missing_profile_dir_does_not_create_it(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Hermes pass-PR-2 blocker: a refused recovery must be side-effect
    free. Previously the missing-profile-dir branch recreated
    ``profile_dir`` BEFORE per-mapping validation, so a continue that
    refused on (e.g.) an AMBIGUOUS mapping still left a fresh
    ``metadata.json`` behind. A retry would then see a different
    starting state than the refused first attempt.

    Trigger: earliest crash window (intent landed, first ``_store.create``
    did not) AND one mapping has diverged from its journaled
    ``original_kind`` (e.g., ``original_kind="missing"`` but the live
    dir has been externally recreated → classifier returns AMBIGUOUS).
    """
    claude_live = tmp_home / ".claude"
    (claude_live / "settings.json").write_text('{"externally_recreated": true}')
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    # No store.create — earliest crash window.
    # original_kind="missing" + live now exists as a real dir →
    # classify_mapping returns AMBIGUOUS in compensation's validation.
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "missing")])
    OpLogIO(tmp_state).append_record(record)
    profile_dir = store.profile_dir(profile_name)
    assert not profile_dir.exists()  # precondition

    with pytest.raises(OpLogCorruptError):
        service.init(continue_=True)

    # Refusal is side-effect free: profile_dir must NOT have been created
    # by the recovery attempt. The user's next retry sees the same disk
    # state the first attempt did.
    assert not profile_dir.exists()
    # Journal stays in flight so the user can fix manually and retry.
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_continue_refuses_when_existing_profile_has_foreign_tools(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Hermes pass-PR-2 blocker: continue must not silently adopt a
    foreign profile occupying ``profiles/<record.profile_name>``.

    Trigger: a profile dir with READABLE metadata that doesn't match
    ``record.target_ids`` already exists at the recovery pathname (e.g.,
    a prior init for an unrelated tool set, or a hand-edited journal).
    Without this check, continue would replay mappings, write
    ``active = {target_id: record.profile_name}``, and leave
    ``store.get(record.profile_name).tools`` saying the profile manages
    DIFFERENT tools — internally inconsistent persisted state.
    """
    claude_live = tmp_home / ".claude"
    (claude_live / "settings.json").write_text('{"data": true}')
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    # Foreign profile: tools={"external": True}, NOT matching
    # record.target_ids=["claude"].
    store.create(profile_name, {"external": True})
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    with pytest.raises(OpLogCorruptError):
        service.init(continue_=True)

    # Foreign metadata preserved (NOT silently overwritten).
    assert store.get(profile_name).tools == {"external": True}
    # No active map mutation either.
    assert store.get_active() == {}
    # Journal stays in flight.
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_abort_refuses_when_existing_profile_has_foreign_tools(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Hermes pass-PR-2 blocker: abort must NOT ``rmtree`` a foreign
    profile dir. Previously abort's only gate was directory shape
    (is_dir + not is_link), so a profile occupying the recovery
    pathname with metadata for an unrelated tool set would be
    recursively deleted = data loss.
    """
    claude_live = tmp_home / ".claude"
    (claude_live / "settings.json").write_text('{"data": true}')
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    # Foreign profile with its own data.
    store.create(profile_name, {"external": True})
    foreign_data = store.profile_dir(profile_name) / "external" / "config.json"
    foreign_data.parent.mkdir(parents=True)
    foreign_data.write_text('{"user_data": "important"}')
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    with pytest.raises(OpLogCorruptError):
        service.init(abort=True)

    # Foreign profile preserved end-to-end: dir + metadata + user data.
    assert store.profile_dir(profile_name).is_dir()
    assert store.get(profile_name).tools == {"external": True}
    assert foreign_data.read_text() == '{"user_data": "important"}'
    # Live state untouched.
    assert (claude_live / "settings.json").read_text() == '{"data": true}'
    # Journal stays in flight.
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_abort_refuses_when_existing_vanilla_has_foreign_tools(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Hermes pass-PR-2 blocker, vanilla variant: abort's profile-dir
    delete loop also covers ``vanilla``. A foreign vanilla profile
    (different metadata) must NOT be deleted — same data-loss
    concern as the dated-current case.
    """
    claude_live = tmp_home / ".claude"
    (claude_live / "settings.json").write_text('{"data": true}')
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    # Current profile matches; vanilla is foreign.
    store.create(profile_name, {"claude": True})
    store.create("vanilla", {"unrelated_tool": True})
    foreign_data = store.profile_dir("vanilla") / "unrelated_tool" / "config.json"
    foreign_data.parent.mkdir(parents=True)
    foreign_data.write_text('{"preserve": true}')
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    with pytest.raises(OpLogCorruptError):
        service.init(abort=True)

    # Foreign vanilla preserved.
    assert store.profile_dir("vanilla").is_dir()
    assert store.get("vanilla").tools == {"unrelated_tool": True}
    assert foreign_data.read_text() == '{"preserve": true}'
    # Current profile also preserved (refusal is two-pass: validate first).
    assert store.profile_dir(profile_name).is_dir()
    # Journal stays in flight.
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_continue_preflights_config_before_any_filesystem_mutation(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """``_compensate_init_continue`` must read config.json in its
    validation pass — BEFORE move_or_seed_dir / swap_link / vanilla
    create / credential seeding run. If config.json is malformed, the
    StorageError must surface BEFORE any destructive work, leaving
    the disk recoverable for a manual retry.

    Symmetric to test_abort_preflights_config_before_any_filesystem_mutation.
    Hermes pass-PR-1 blocker: previously continue would replay mappings
    (convert live to a symlink), create profiles/vanilla, reseed
    credentials, and only then call set_active_state — which loads
    config.json. Malformed config raised StorageError half-way through,
    leaving live converted but journal still in flight.
    """
    # Pre-state: untouched live dir, profile already created (so the
    # short-circuit's metadata check passes), mapping classifies UNTOUCHED.
    # This means short-circuit returns False (mapping not COMPLETE) and
    # compensation runs.
    claude_live = tmp_home / ".claude"
    (claude_live / "settings.json").write_text('{"original": true}')
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    store.create(profile_name, {"claude": True})
    store.create("vanilla", {"claude": True})
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    # Corrupt config.json AFTER setup. set_active_state at the tail of
    # compensation would otherwise raise StorageError ONLY after mutating
    # live + creating profile content. Preflight should surface it now.
    (tmp_state / "config.json").write_text("{not valid json")

    with pytest.raises(StorageError):
        service.init(continue_=True)

    # Pre-mutation state preserved: live still a real dir, not converted
    # to a symlink/junction by a half-applied swap_link.
    assert claude_live.is_dir()
    assert not service._resolver.is_link(claude_live)
    assert (claude_live / "settings.json").read_text() == '{"original": true}'
    # Journal stays in flight — no mark_completed reached.
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_abort_preflights_config_before_any_filesystem_mutation(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """``_compensate_init_abort`` must read config.json in its validation
    pass — BEFORE it deletes profile dirs or restores live state. If
    config.json is malformed, the StorageError must surface BEFORE any
    destructive work, leaving the disk recoverable for a manual retry
    after the user fixes the config.

    Without preflight, abort restores live dirs and deletes the profile
    dirs (mutation pass), then reads config.json (cleanup pass) — if
    that read raises StorageError, abort is half-applied: live state
    restored, profile dirs gone, journal still in flight, but active
    map still references the deleted profile. CR pass-7 major.

    Scenario: a partial-complete state where the short-circuit returns
    False (mapping isn't COMPLETE) so abort actually runs, AND
    config.json is corrupted at FS-truth-read time.
    """
    # Pre-state: live dir present (real), no init mutation applied yet.
    claude_live = tmp_home / ".claude"
    (claude_live / "user-data.json").write_text('{"user": true}')
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    # Build the init pre-flight state with proper metadata so the short-
    # circuit's metadata check passes; the mapping classifier will then
    # see UNTOUCHED (live still real-dir, target empty) → short-circuit
    # fails on the mapping-not-COMPLETE check, abort proceeds.
    store.create(profile_name, {"claude": True})
    store.create("vanilla", {"claude": True})
    store.set_active_state({"claude": profile_name}, {"claude": [str(claude_live)]})
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    # Corrupt config.json AFTER setup. The short-circuit's get_active()
    # read happens FIRST (against the malformed config) and short-
    # circuits the check — but the abort path reads config again as
    # part of its cleanup. The fix moves that read into the validation
    # pass so it raises BEFORE any mutation.
    config_path = tmp_state / "config.json"
    config_path.write_text("{not valid json")

    # Abort must raise (StorageError from malformed config) BEFORE any
    # FS mutation. The short-circuit reads get_active too, so the
    # error path may surface there OR in abort's validation — either
    # is acceptable as long as the disk state is untouched.
    with pytest.raises(StorageError):
        service.init(abort=True)

    # Pre-mutation state preserved end-to-end.
    assert store.profile_dir(profile_name).is_dir()
    assert store.profile_dir("vanilla").is_dir()
    # Journal still in flight — no mark_completed reached.
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_continue_regenerates_vanilla_when_empty_leftover_dir(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Vanilla profile dir present but empty (no metadata.json) =
    mkdir-only leftover from a ``_store.create("vanilla", ...)``
    failure whose rollback was silenced (rmtree-ignore-errors window).
    Continue MUST detect this case and regenerate vanilla through the
    canonical ``_store.create`` path instead of treating an empty
    leftover as a healthy profile.

    Without this, the next ``switcher use`` would surface
    UnknownProfileError on what looked like a successful recovery.
    CR pass-3 major (narrower restatement of the pass-2 metadata
    concern, scoped to vanilla where compensation CAN heal — the
    dir is in our control, unlike the dated-current profile which
    holds user data we cannot regenerate).
    """
    claude_live = tmp_home / ".claude"
    (claude_live / "user-data.json").write_text('{"user": true}')
    profile_name = "2026-05-12-current"
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)
    # Set up the partial state: empty vanilla profile dir (mkdir-only
    # leftover), nothing else. _compensate_init_continue will:
    # 1. validate profile-dir shapes (vanilla dir exists, not link → ok)
    # 2. run per-mapping mutation (live → target migration)
    # 3. reach the vanilla check: dir exists but no metadata → recreate.
    store = FileProfileStore(tmp_state)
    vanilla_dir = store.profile_dir("vanilla")
    vanilla_dir.mkdir(parents=True)

    service.init(continue_=True)

    # Vanilla regenerated through store.create — metadata.json now
    # exists and store.get returns a valid Profile.
    profile = store.get("vanilla")
    assert profile.name == "vanilla"
    assert profile.tools == {"claude": True}


def test_continue_refuses_vanilla_with_data_but_no_metadata(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Vanilla profile dir with content but no metadata.json is
    unsafe to auto-regenerate: that would silently overwrite the
    user's data. Refuse loudly so the user manually inspects.
    Distinct from the empty-leftover case above.
    """
    claude_live = tmp_home / ".claude"
    (claude_live / "user-data.json").write_text('{"user": true}')
    profile_name = "2026-05-12-current"
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)
    store = FileProfileStore(tmp_state)
    vanilla_dir = store.profile_dir("vanilla")
    vanilla_dir.mkdir(parents=True)
    (vanilla_dir / "stray-content.txt").write_text("user file we cannot lose")

    with pytest.raises(OpLogCorruptError, match="vanilla"):
        service.init(continue_=True)
    # Refusal happens BEFORE mark_completed: journal record stays in
    # flight so the user can retry after manual cleanup.
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_continue_serializes_empty_cache_entry_for_zero_mapping_tool(
    service: ProfileService, tmp_state: Path
) -> None:
    """A target_id with zero mappings (a registry-only tool with no
    ``config_dirs``) must serialize as ``cache[tid] = []`` on disk
    — matching the shape a clean init writes (``_capture_tool``
    returns ``[]`` for those tools). Without an explicit per-target
    seed, continue would silently drop the cache key while leaving
    active populated — asymmetric serialized state.

    ``get_active_live_paths`` normalizes ``[]`` to "absent" at read
    time, so the assertion has to read raw ``config.json`` rather
    than going through the store API."""
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    store.create(profile_name, {"registry-only-tool": True})
    record = _make_init_record(profile_name, ["registry-only-tool"], mappings=[])
    OpLogIO(tmp_state).append_record(record)

    service.init(continue_=True)

    config = json.loads((tmp_state / "config.json").read_text())
    # active populated for the zero-mapping tool.
    assert config["active"]["registry-only-tool"] == profile_name
    # cache key present with [] value — the serialized symmetry.
    assert "registry-only-tool" in config["active_live_paths"]
    assert config["active_live_paths"]["registry-only-tool"] == []


def test_continue_writes_symmetric_active_and_cache_for_orphan_tool(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """If a tool is unregistered between intent-write and recovery,
    ``_compensate_init_continue`` must still write the
    ``live_paths_cache`` entry for it — the journal holds the
    ``live_path`` snapshot the resolver can no longer reproduce, and
    a missing cache entry alongside a present active entry creates
    inconsistent state.

    Use a fabricated tool id (``"phantom"``) that the registry has
    never heard of so ``find_tool`` returns None, simulating the
    "registered at intent time, not at recovery time" drift.
    """
    phantom_live = tmp_home / ".phantom"
    phantom_live.mkdir()
    (phantom_live / "settings.json").write_text('{"phantom": true}')
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    store.create(profile_name, {"phantom": True})
    phantom_mapping = _MappingIntent.model_validate(
        {
            "tool_id": "phantom",
            "mapping_index": 0,
            "live_path": str(phantom_live),
            "profile_subdir": "phantom",
            "original_kind": "real-dir",
        }
    )
    record = _make_init_record(profile_name, ["phantom"], [phantom_mapping])
    OpLogIO(tmp_state).append_record(record)

    service.init(continue_=True)

    # Active map has phantom; cache also has phantom with the journal's
    # live_path. (The previous resolver-based cache would have skipped
    # phantom — only active would carry it.)
    active = store.get_active()
    cache = store.get_active_live_paths()
    assert active.get("phantom") == profile_name
    assert cache.get("phantom") == [str(phantom_live)]


def test_abort_clears_active_and_cache_for_target_ids(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Init progressed far enough to call ``set_active_state``, then
    drift (extra active entry, stale cache) made
    ``_check_init_already_completed`` return False. Abort runs the
    full compensation path and MUST clear the active/cache entries
    for the target_ids it owns — leaving them in place would have
    active reference a deleted profile and cache hold a path that
    is no longer a switcher symlink.

    External-state preservation: entries OUTSIDE ``record.target_ids``
    (e.g. an unrelated tool that got into active via external
    mutation) stay untouched. The journal only owns its
    target_ids."""
    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    store.create(profile_name, {"claude": True})
    profile_dir = store.profile_dir(profile_name)
    target = profile_dir / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"data": true}')
    _symlink_dir(target, claude_live)
    store.create("vanilla", {"claude": True})
    # Stage drift: extra "stale" active entry that fails the
    # _check_init_already_completed strict-equality test. Abort
    # falls through to the full compensation path.
    store.set_active_state(
        {"claude": profile_name, "stale": profile_name},
        {"claude": [str(claude_live)]},
    )
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    service.init(abort=True)

    # Owned target_id was cleared.
    active = store.get_active()
    cache = store.get_active_live_paths()
    assert "claude" not in active, f"abort must clear owned target_ids from active; got {active!r}"
    assert "claude" not in cache, f"abort must clear owned target_ids from cache; got {cache!r}"
    # External-state entries preserved.
    assert active.get("stale") == profile_name, (
        f"abort must leave non-target_id active entries alone; got {active!r}"
    )
    # Profile dirs deleted; live restored.
    assert not profile_dir.exists()
    assert not store.profile_dir("vanilla").exists()
    assert claude_live.is_dir()
    assert not claude_live.is_symlink()
    assert (claude_live / "settings.json").read_text() == '{"data": true}'
    assert OpLogIO(tmp_state).read_in_flight() is None


def test_abort_preserves_unrelated_zero_mapping_cache_entry(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """A zero-mapping tool unrelated to the in-flight init has its
    cache serialized as ``[]``. Abort's cleanup must preserve that
    entry on disk — without the raw accessor, ``get_active_live_paths``
    would have normalized ``[]`` to absent and the rewrite would have
    silently dropped the key. External state outside
    ``record.target_ids`` MUST stay untouched."""
    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    store.create(profile_name, {"claude": True})
    profile_dir = store.profile_dir(profile_name)
    target = profile_dir / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"data": true}')
    _symlink_dir(target, claude_live)
    store.create("vanilla", {"claude": True})
    # Stage: active has "claude" (the journal's target_id) AND
    # "registry-only-tool" (unrelated, zero-mapping). cache has
    # "claude": [path] AND "registry-only-tool": []. Abort's cleanup
    # should clear "claude" and leave "registry-only-tool": [] alone.
    store.set_active_state(
        {"claude": profile_name, "registry-only-tool": "other-profile"},
        {"claude": [str(claude_live)], "registry-only-tool": []},
    )
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    service.init(abort=True)

    # External state preserved: active still has the unrelated entry,
    # cache still serializes [] for the unrelated zero-mapping tool.
    config = json.loads((tmp_state / "config.json").read_text())
    assert config["active"].get("registry-only-tool") == "other-profile"
    assert "registry-only-tool" in config["active_live_paths"]
    assert config["active_live_paths"]["registry-only-tool"] == []
    # Owned target_id cleared.
    assert "claude" not in config["active"]
    assert "claude" not in config["active_live_paths"]


def test_abort_idempotent_second_call_raises_no_in_progress(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """First abort compensates and marks completed; vacuum drops the
    record; a second abort sees no in-flight record and raises
    NoInProgressInitError."""
    claude_live = tmp_home / ".claude"
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    store.create(profile_name, {"claude": True})
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    service.init(abort=True)
    OpLogIO(tmp_state).vacuum_completed()

    assert not (claude_live / "settings.json").exists()  # live untouched (was empty)
    with pytest.raises(NoInProgressInitError):
        service.init(abort=True)


def test_continue_does_not_short_circuit_when_cache_is_stale(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """The "committed but log-unmarked" short-circuit must also
    validate ``active_live_paths`` against the journal's expected
    shape — a stale cache entry would otherwise short-circuit
    recovery and permanently skip the cache-rebuild step.

    Test stages every other completion signal (mapping COMPLETE,
    active matches, vanilla present) BUT writes a wrong path into
    the cache. Continue must fall through to compensation, which
    rebuilds the cache from the journal."""
    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    store.create(profile_name, {"claude": True})
    profile_dir = store.profile_dir(profile_name)
    target = profile_dir / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"committed": true}')
    _symlink_dir(target, claude_live)
    store.create("vanilla", {"claude": True})
    # Stage a STALE cache value (wrong path) — the journal's expected
    # path is str(claude_live); we deliberately write a different one
    # so the short-circuit's cache-shape check fails.
    store.set_active_state({"claude": profile_name}, {"claude": ["/wrong/path"]})
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    service.init(continue_=True)

    # Compensation rebuilt the cache from the journal.
    config = json.loads((tmp_state / "config.json").read_text())
    assert config["active_live_paths"]["claude"] == [str(claude_live)]
    # Record marked completed.
    assert OpLogIO(tmp_state).read_in_flight() is None


def test_continue_does_not_short_circuit_when_active_has_extra_entry(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Active map invariant: dict equality, not subset. A clean
    init's ``set_active_state`` REPLACES the active map, so an
    extra entry on disk is drift since intent (external mutation,
    previous-run residue). Short-circuiting under that drift would
    mark the journal completed and let the stale entry survive.

    Test stages every other completion signal (mapping COMPLETE,
    matching cache, vanilla present) BUT plants an EXTRA active
    entry for a tool the journal doesn't reference. Continue must
    fall through to compensation, which REPLACES the active map
    with target_ids only."""
    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    store.create(profile_name, {"claude": True})
    profile_dir = store.profile_dir(profile_name)
    target = profile_dir / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"committed": true}')
    _symlink_dir(target, claude_live)
    store.create("vanilla", {"claude": True})
    # Active map carries the legitimate "claude" entry AND an extra
    # "stale-tool" entry that target_ids doesn't reference. Cache
    # mirrors the expectation only for claude. Short-circuit MUST
    # fall through.
    store.set_active_state(
        {"claude": profile_name, "stale-tool": profile_name},
        {"claude": [str(claude_live)]},
    )
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    service.init(continue_=True)

    # Compensation REPLACED active — only claude remains.
    active = store.get_active()
    assert active == {"claude": profile_name}, (
        f"continue should have replaced active with target_ids only; got {active!r}"
    )
    assert OpLogIO(tmp_state).read_in_flight() is None


def test_continue_does_not_short_circuit_when_zero_mapping_tool_has_stale_cache(
    service: ProfileService, tmp_state: Path
) -> None:
    """Symmetric to ``test_..._when_cache_is_stale`` but covers the
    zero-mapping tool case: a target_id with no mappings expects
    ``cache[tid] = []`` (normalized to "absent" at read). If a
    crashed init left ``cache[tid] = ["/wrong/path"]`` behind, the
    short-circuit must fall through so the cache-rebuild step
    overwrites the stale entry.

    Dict-equality check on ``(actual_cache == expected_nonempty)``
    catches this — the actual cache has an extra key the expected
    map doesn't carry."""
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    store.create(profile_name, {"registry-only-tool": True})
    store.create("vanilla", {"registry-only-tool": True})
    # Stale cache: zero-mapping tool expects [] but actual cache has
    # a non-empty path. Short-circuit MUST fall through.
    store.set_active_state(
        {"registry-only-tool": profile_name},
        {"registry-only-tool": ["/wrong/stale/path"]},
    )
    record = _make_init_record(profile_name, ["registry-only-tool"], mappings=[])
    OpLogIO(tmp_state).append_record(record)

    service.init(continue_=True)

    # Compensation rebuilt the cache: zero-mapping tool now has []
    # (which appears as absent via get_active_live_paths).
    config = json.loads((tmp_state / "config.json").read_text())
    assert config["active_live_paths"].get("registry-only-tool") == []
    assert OpLogIO(tmp_state).read_in_flight() is None


def test_continue_does_not_short_circuit_when_vanilla_is_missing(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """The "committed but log-unmarked" short-circuit must require
    BOTH the dated-current AND the vanilla profile dirs to be present.

    Vanilla is part of the init workflow (step 6), so a missing
    vanilla means the work isn't fully complete. Short-circuiting
    here would mark the journal completed and skip the vanilla
    recovery step — leaving state that recovery considers "done" but
    is actually incomplete.

    Test stages every other completion signal (mapping COMPLETE,
    active map covers target_ids) BUT deletes the vanilla profile.
    Continue must fall through to compensation, which re-creates
    vanilla."""
    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    store.create(profile_name, {"claude": True})
    profile_dir = store.profile_dir(profile_name)
    target = profile_dir / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"committed": true}')
    _symlink_dir(target, claude_live)
    store.set_active_state({"claude": profile_name}, {"claude": [str(claude_live)]})
    # Don't create vanilla. _check_init_already_completed sees mapping
    # COMPLETE + active matches BUT vanilla missing → return False →
    # continue runs the compensation path → vanilla gets created.
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    service.init(continue_=True)

    # Vanilla was created by the fallthrough path.
    assert store.profile_dir("vanilla").is_dir()
    # Mapping still COMPLETE; live still symlinked; record completed.
    assert claude_live.is_symlink()
    assert (target / "settings.json").read_text() == '{"committed": true}'
    assert OpLogIO(tmp_state).read_in_flight() is None


def test_continue_refuses_when_target_subdir_is_symlink(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """A symlink/junction at ``profile_dir/subdir`` is corruption —
    the journal's invariant is "target is a real profile subdirectory".
    The classifier's AMBIGUOUS fall-through would already block
    mutation here, but the explicit per-target shape refusal gives a
    clearer error and protects future refactors from missing the
    AMBIGUOUS catch.

    Stages a non-profile-store directory and symlinks
    ``profile_dir/claude`` at it; continue must refuse before
    ``move_or_seed_dir`` or ``swap_link`` could operate on the
    escaped path."""
    claude_live = tmp_home / ".claude"
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    store.create(profile_name, {"claude": True})
    profile_dir = store.profile_dir(profile_name)
    # Plant a symlink at profile_dir/claude pointing somewhere unrelated.
    elsewhere = tmp_state / "elsewhere"
    elsewhere.mkdir(parents=True)
    (elsewhere / "important.txt").write_text("user data")
    (profile_dir / "claude").symlink_to(elsewhere, target_is_directory=True)
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    with pytest.raises(OpLogCorruptError, match="symlink or junction"):
        service.init(continue_=True)

    # No mutation: live untouched, elsewhere data preserved, intent
    # still in flight.
    assert claude_live.is_dir()
    assert not claude_live.is_symlink()
    assert (elsewhere / "important.txt").read_text() == "user data"
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_abort_refuses_when_target_subdir_is_symlink(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """A symlink/junction at ``profile_dir/subdir`` is corruption.
    Abort must refuse before ``shutil.rmtree`` or ``move_or_seed_dir``
    could operate on the escaped path. Same defense-in-depth as
    ``test_continue_refuses_when_target_subdir_is_symlink``."""
    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    store.create(profile_name, {"claude": True})
    profile_dir = store.profile_dir(profile_name)
    elsewhere = tmp_state / "elsewhere"
    elsewhere.mkdir(parents=True)
    (elsewhere / "important.txt").write_text("user data")
    (profile_dir / "claude").symlink_to(elsewhere, target_is_directory=True)
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "missing")])
    OpLogIO(tmp_state).append_record(record)

    with pytest.raises(OpLogCorruptError, match="symlink or junction"):
        service.init(abort=True)

    # No mutation: elsewhere data preserved, profile dir still
    # present with the corrupt symlink, intent in flight.
    assert (elsewhere / "important.txt").read_text() == "user data"
    assert (profile_dir / "claude").is_symlink()
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_continue_refuses_when_vanilla_profile_dir_is_symlink(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """A symlink/junction at ``profiles/vanilla`` is corruption — the
    profile-store invariant is "profile_dir is a real directory", and
    a symlink there would let ``_seed_credentials`` copy files into
    unrelated data. Continue must refuse with ``OpLogCorruptError``
    rather than skip create-and-proceed."""
    claude_live = tmp_home / ".claude"
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    # Use store.create so the current-profile metadata is valid; the
    # only corruption under test here is the vanilla symlink. Without
    # this the current-profile metadata-readability check (pass-7)
    # could fire BEFORE the vanilla shape check, making the test pass
    # for the wrong reason. CR pass-PR-3 nit.
    store.create(profile_name, {"claude": True})
    # Plant a symlink at profiles/vanilla pointing somewhere unrelated.
    elsewhere = tmp_state / "elsewhere"
    elsewhere.mkdir(parents=True)
    vanilla_path = store.profile_dir("vanilla")
    vanilla_path.parent.mkdir(parents=True, exist_ok=True)
    vanilla_path.symlink_to(elsewhere, target_is_directory=True)
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    with pytest.raises(OpLogCorruptError, match="symlink or junction"):
        service.init(continue_=True)

    # No mutation: live still real dir, vanilla still symlink, intent
    # still in flight.
    assert claude_live.is_dir()
    assert not claude_live.is_symlink()
    assert vanilla_path.is_symlink()
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_abort_refuses_when_profile_dir_is_symlink(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """A symlink/junction at ``profiles/<profile_name>`` makes the
    abort profile-delete path unsafe — ``shutil.rmtree`` would follow
    the symlink and trash unrelated data. Refuse with
    ``OpLogCorruptError``; leave the partial state alone for manual
    inspection."""
    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    # Plant a symlink at profiles/<profile_name>.
    elsewhere = tmp_state / "elsewhere"
    elsewhere.mkdir(parents=True)
    (elsewhere / "important.txt").write_text("user data")
    profile_path = store.profile_dir(profile_name)
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.symlink_to(elsewhere, target_is_directory=True)
    record = _make_init_record(
        profile_name, ["claude"], mappings=[]
    )  # untouched mapping shape would classify UNTOUCHED via target absence
    OpLogIO(tmp_state).append_record(record)

    with pytest.raises(OpLogCorruptError, match="symlink or junction"):
        service.init(abort=True)

    # No mutation: symlink still in place, elsewhere intact, intent
    # still in flight.
    assert profile_path.is_symlink()
    assert (elsewhere / "important.txt").read_text() == "user data"
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_abort_rejects_link_original_kind_defensively(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Defensive guard inside ``_compensate_init_abort`` rejects
    ``original_kind`` outside {"missing","real-dir"} as
    ``AbortPreflightError``.

    Unreachable via the normal journal lifecycle: ``_MappingIntent``
    narrows ``original_kind`` to a ``Literal["missing","real-dir"]``,
    so the load-side validator rejects anything else as
    ``OpLogCorruptError`` before this method ever sees it. The
    in-method check is defense-in-depth — a future schema widening
    (or a direct caller bypassing the journal) lands here, and
    silently mutating on an unrecognized value would defeat the spec
    §2.1.1 abort-dispatch invariant.

    Test exercises the guard by calling the private method with a
    ``model_construct`` record (same pattern the rename compensation
    suite uses for unreachable-via-load defensive branches)."""
    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    store.create(profile_name, {"claude": True})
    profile_dir = store.profile_dir(profile_name)
    target = profile_dir / "claude"
    target.mkdir(parents=True)
    _symlink_dir(target, claude_live)
    bad_mapping = _MappingIntent.model_construct(
        tool_id="claude",
        mapping_index=0,
        live_path=str(claude_live),
        profile_subdir="claude",
        original_kind="link",  # type: ignore[arg-type]
    )
    record = _InitOp.model_construct(
        op="init",
        started_at=_now(),
        target_ids=["claude"],
        profile_name=profile_name,
        mappings=[bad_mapping],
    )

    with pytest.raises(AbortPreflightError):
        service._compensate_init_abort(record)

    # No mutation: profile dir + symlink still in place.
    assert claude_live.is_symlink()
    assert profile_dir.exists()
