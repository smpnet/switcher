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
)
from switcher.models import Tool
from switcher.oplog import OpLogIO, _InitOp, _MappingIntent, _RenameOp, _RescanOp
from switcher.paths import PathResolver
from switcher.registry import build_registry
from switcher.service import ProfileService
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
    # Plant the dated-current profile dir (init step 4) but no per-mapping
    # capture has happened — target subdir absent.
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    profile_dir = store.profile_dir(profile_name)
    profile_dir.mkdir(parents=True)
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    service.init(continue_=True)

    assert claude_live.is_symlink()
    assert (profile_dir / "claude" / "settings.json").read_text() == '{"original": true}'
    assert store.get_active().get("claude") == profile_name
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
    profile_dir = store.profile_dir(profile_name)
    target = profile_dir / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"already_moved": true}')
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    service.init(continue_=True)

    assert claude_live.is_symlink()
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
    assert claude_live.is_dir() and not claude_live.is_symlink()
    assert (claude_live / "settings.json").read_text() == '{"live": true}'
    assert (target / "settings.json").read_text() == '{"target": true}'
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_continue_with_no_in_flight_raises_no_in_progress(
    service: ProfileService, tmp_state: Path
) -> None:
    """`switcher init --continue` against an empty journal → NoInProgressInitError."""
    with pytest.raises(NoInProgressInitError):
        service.init(continue_=True)


def test_continue_routes_user_to_rescan_when_in_flight_is_rescan(
    service: ProfileService, tmp_state: Path
) -> None:
    """If the journal holds an in-flight ``_RescanOp`` and the user
    runs ``switcher init --continue``, route them to the matching
    ``switcher rescan --continue`` command rather than silently
    refusing with a generic "wrong type" error.

    This user-facing recovery-steering branch must stay covered so a
    future refactor doesn't quietly land users on a manual-recovery
    prompt for an op that has an automated recovery command available."""
    record = _RescanOp.model_validate(
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
    OpLogIO(tmp_state).append_record(record)

    with pytest.raises(RescanInProgressError, match="rescan --continue"):
        service.init(continue_=True)
    with pytest.raises(RescanInProgressError, match="rescan --continue"):
        service.init(abort=True)
    # Journal: rescan record still in flight after both refused calls.
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_continue_refuses_with_oplog_corrupt_on_unexpected_in_flight_type(
    service: ProfileService, tmp_state: Path
) -> None:
    """If the journal holds an in-flight ``_RenameOp`` and the user
    runs ``switcher init --continue/--abort``, refuse loudly with
    ``OpLogCorruptError``. Reaching this state means a prior CLI
    command's detection hook (which auto-compensates rename) didn't
    run — likely a stale binary or a hand-edited journal. The
    defensive refusal is documented in service.init's continue/abort
    dispatch table."""
    record = _RenameOp.model_validate(
        {
            "op": "rename",
            "started_at": _now(),
            "from": "old",
            "to": "new",
            "affected_ids": ["claude"],
        }
    )
    OpLogIO(tmp_state).append_record(record)

    with pytest.raises(OpLogCorruptError, match="unexpected in-flight op type"):
        service.init(continue_=True)
    with pytest.raises(OpLogCorruptError, match="unexpected in-flight op type"):
        service.init(abort=True)
    # Journal: rename record still in flight.
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


# -- writer-side journal hygiene ---------------------------------------------


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
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """A failure AFTER ``_store.create(current_name, ...)`` already
    mutated FS state (here: a ``_capture_tool`` OSError) must NOT
    cancel the intent — compensation needs the in-flight record to
    drive the recovery surface. The cancel scope is narrow on purpose:
    only the pre-mutation ProfileExistsError window triggers it."""
    real_capture = service._capture_tool

    def _failing_capture(profile: str, tool: object) -> list[str]:
        # Run the real capture for the first call so we leave actual
        # post-mutation state behind, then fail on the second call to
        # exit init mid-loop. Without real mutation, the test would be
        # exercising the pre-mutation path again.
        if not getattr(_failing_capture, "_fired", False):
            _failing_capture._fired = True  # type: ignore[attr-defined]
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
    profile_dir = store.profile_dir(profile_name)
    target = profile_dir / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"original": true}')
    _symlink_dir(target, claude_live)
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    service.init(abort=True)

    assert claude_live.is_dir() and not claude_live.is_symlink()
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
    profile_dir = store.profile_dir(profile_name)
    target = profile_dir / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"original": true}')
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    service.init(abort=True)

    assert claude_live.is_dir() and not claude_live.is_symlink()
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
    profile_dir = store.profile_dir(profile_name)
    profile_dir.mkdir(parents=True)
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
    # is a symlink to a populated target, active map covers target_ids.
    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    profile_dir = store.profile_dir(profile_name)
    target = profile_dir / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"committed": true}')
    _symlink_dir(target, claude_live)
    store.set_active_state({"claude": profile_name}, {"claude": [str(claude_live)]})
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    service.init(abort=True)

    # Abort did NOT run: live still symlinked, target data intact,
    # profile dir present, active map untouched.
    assert claude_live.is_symlink()
    assert (target / "settings.json").read_text() == '{"committed": true}'
    assert profile_dir.exists()
    assert store.get_active().get("claude") == profile_name
    # Journal: record marked completed (next vacuum drops it).
    assert OpLogIO(tmp_state).read_in_flight() is None


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
    profile_dir = store.profile_dir(profile_name)
    profile_dir.mkdir(parents=True)
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
    profile_dir = store.profile_dir(profile_name)
    profile_dir.mkdir(parents=True)
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


def test_abort_idempotent_second_call_raises_no_in_progress(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """First abort compensates and marks completed; vacuum drops the
    record; a second abort sees no in-flight record and raises
    NoInProgressInitError."""
    claude_live = tmp_home / ".claude"
    profile_name = "2026-05-12-current"
    store = FileProfileStore(tmp_state)
    profile_dir = store.profile_dir(profile_name)
    profile_dir.mkdir(parents=True)
    record = _make_init_record(profile_name, ["claude"], [_claude_mapping(tmp_home, "real-dir")])
    OpLogIO(tmp_state).append_record(record)

    service.init(abort=True)
    OpLogIO(tmp_state).vacuum_completed()

    assert (claude_live / "settings.json").exists() is False  # live untouched (was empty)
    with pytest.raises(NoInProgressInitError):
        service.init(abort=True)


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
    profile_dir = store.profile_dir(profile_name)
    profile_dir.mkdir(parents=True)
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
    assert claude_live.is_dir() and not claude_live.is_symlink()
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
    profile_dir = store.profile_dir(profile_name)
    profile_dir.mkdir(parents=True)
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
    profile_dir = store.profile_dir(profile_name)
    profile_dir.mkdir(parents=True)
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
    assert claude_live.is_dir() and not claude_live.is_symlink()
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
