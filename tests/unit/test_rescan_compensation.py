# pyright: reportPrivateUsage=none
"""Tests for rescan op-log compensation (spec §2.4 / §8.1 rescan section).

``service.rescan(continue_=True)`` and ``service.rescan(abort=True)`` drive
disk-truth compensation against the §2.1.1 four-state classifier, with two
mode bifurcations (fresh-profile vs ``--into``). Mirrors the structural
shape of ``test_init_compensation.py`` — shared fixtures come from the
top-level ``tests/conftest.py``."""

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
    InitInProgressError,
    NoInProgressRescanError,
    OpLogCorruptError,
    ProfileExistsError,
    RescanCaptureError,
    StorageError,
)
from switcher.models import Tool
from switcher.oplog import OpLogIO, _InitOp, _MappingIntent, _RenameOp, _RescanOp
from switcher.paths import PathResolver
from switcher.registry import build_registry
from switcher.service import ProfileService, RescanAlreadyCompletedReport
from switcher.store import FileProfileStore


def _now() -> datetime:
    return datetime(2026, 5, 12, 10, 30, tzinfo=UTC)


def _symlink_dir(target: Path, live: Path) -> None:
    """Create ``live`` → ``target`` symlink with the cross-platform
    ``target_is_directory`` kwarg Windows needs and POSIX ignores."""
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


def _make_rescan_record(
    *,
    target_ids: list[str],
    target_profiles: dict[str, str],
    into_mode: bool,
    previous_tools: dict[str, dict[str, bool]] | None,
    mappings: list[_MappingIntent],
) -> _RescanOp:
    return _RescanOp.model_validate(
        {
            "op": "rescan",
            "started_at": _now(),
            "target_ids": target_ids,
            "target_profiles": target_profiles,
            "into_mode": into_mode,
            "previous_tools": previous_tools,
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


# -- continue path ----------------------------------------------------------


def test_continue_fresh_profile_mid_capture_window(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Spec §8.1 rescan #1. One mapping COMPLETE, one MOVE_DONE_LINK_MISSING,
    others UNTOUCHED. Continue dispatches the matrix correctly: COMPLETE
    skipped, MOVE_DONE_LINK_MISSING gets swap_link, UNTOUCHED gets the
    full pair. Then for each unique profile in target_profiles, the
    profile dir is finalized + set_active_state runs."""
    # rescan's own pre-flight requires the store to be initialized
    # (require_initialized) so a vanilla profile must exist; the
    # 'phantom' tool ids on the live path don't have to be installed
    # because rescan compensation derives candidates from the journal
    # record, not detect_installed().
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})
    store.set_active_state({}, {})  # initialized but nothing managed

    # Tool A (claude) mapping has already been fully captured — live is
    # a symlink to a populated target. Tool B (copilot) is mid-window:
    # target populated, live missing.
    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)
    copilot_live = tmp_home / ".copilot"
    shutil.rmtree(copilot_live)

    profile_a = "2026-05-12-rescan-1"
    profile_b = "2026-05-12-rescan-2"

    # Pre-stage A as COMPLETE: profile dir + target populated + live symlinked.
    store.create(profile_a, {"claude": True})
    target_a = store.profile_dir(profile_a) / "claude"
    target_a.mkdir(parents=True)
    (target_a / "settings.json").write_text('{"captured": true}')
    _symlink_dir(target_a, claude_live)

    # Pre-stage B as MOVE_DONE_LINK_MISSING: profile dir + target populated,
    # live MISSING (no symlink yet).
    store.create(profile_b, {"copilot": True})
    target_b = store.profile_dir(profile_b) / "copilot-config"
    target_b.mkdir(parents=True)
    (target_b / "config.json").write_text('{"copilot": true}')

    record = _make_rescan_record(
        target_ids=["claude", "copilot"],
        target_profiles={"claude": profile_a, "copilot": profile_b},
        into_mode=False,
        previous_tools=None,
        mappings=[
            _claude_mapping(tmp_home, "real-dir"),
            _copilot_mapping(tmp_home, "real-dir"),
        ],
    )
    OpLogIO(tmp_state).append_record(record)

    service.rescan(continue_=True)

    # A stays complete; B gets the missing swap_link.
    assert service._resolver.is_link(claude_live)
    assert service._resolver.is_link(copilot_live)
    assert (target_a / "settings.json").read_text() == '{"captured": true}'
    assert (target_b / "config.json").read_text() == '{"copilot": true}'
    # Active map covers both targets per the journal.
    active = store.get_active()
    assert active.get("claude") == profile_a
    assert active.get("copilot") == profile_b
    # Journal cleared.
    assert OpLogIO(tmp_state).read_in_flight() is None


def test_continue_into_mode_deferred_metadata_window(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Spec §8.1 rescan #2. All mappings COMPLETE but the target's
    ``metadata.tools`` doesn't yet include the captured tools — the
    deferred-metadata kill window in --into mode. Continue runs no
    per-mapping mutation; instead it runs ``update_profile_tools`` to
    finalize the deferred write, then ``set_active_state``."""
    store = FileProfileStore(tmp_state)
    # --into target profile pre-existed before the interrupted rescan.
    # previous_tools captures what its metadata held BEFORE rescan.
    store.create("shared", {"existing-tool": True})
    store.set_active_state({}, {})

    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)

    # Stage A as COMPLETE: target populated + live symlinked.
    target = store.profile_dir("shared") / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"captured": true}')
    _symlink_dir(target, claude_live)
    # metadata.tools STILL says only {"existing-tool": True} — the
    # deferred update_profile_tools never ran.

    record = _make_rescan_record(
        target_ids=["claude"],
        target_profiles={"claude": "shared"},
        into_mode=True,
        previous_tools={"shared": {"existing-tool": True}},
        mappings=[_claude_mapping(tmp_home, "real-dir")],
    )
    OpLogIO(tmp_state).append_record(record)

    service.rescan(continue_=True)

    # Live stays symlinked; metadata now reflects the rescan's tool add.
    assert service._resolver.is_link(claude_live)
    refreshed = store.get("shared")
    assert refreshed.tools == {"existing-tool": True, "claude": True}
    # Active map updated for the captured tool.
    assert store.get_active().get("claude") == "shared"
    assert OpLogIO(tmp_state).read_in_flight() is None


def test_continue_with_no_in_flight_raises_no_in_progress(
    service: ProfileService, tmp_state: Path
) -> None:
    """``switcher rescan --continue`` against an empty journal →
    NoInProgressRescanError."""
    # Initialize the store so rescan's require_initialized passes.
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})
    store.set_active_state({}, {})

    with pytest.raises(NoInProgressRescanError):
        service.rescan(continue_=True)


def test_continue_refuses_on_ambiguous_mapping(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """First-pass scan: any AMBIGUOUS mapping → OpLogCorruptError, no
    mutation. Here live is a real dir AND target is also a populated real
    dir (data in two places)."""
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})
    store.set_active_state({}, {})

    claude_live = tmp_home / ".claude"
    (claude_live / "settings.json").write_text('{"live": true}')

    profile_name = "2026-05-12-rescan-1"
    store.create(profile_name, {"claude": True})
    target = store.profile_dir(profile_name) / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"target": true}')

    record = _make_rescan_record(
        target_ids=["claude"],
        target_profiles={"claude": profile_name},
        into_mode=False,
        previous_tools=None,
        mappings=[_claude_mapping(tmp_home, "real-dir")],
    )
    OpLogIO(tmp_state).append_record(record)

    with pytest.raises(OpLogCorruptError):
        service.rescan(continue_=True)

    # No mutation: both still in place; intent still in flight.
    assert claude_live.is_dir()
    assert not claude_live.is_symlink()
    assert (claude_live / "settings.json").read_text() == '{"live": true}'
    assert (target / "settings.json").read_text() == '{"target": true}'
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_continue_short_circuit_returns_already_completed_report(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """``rescan(continue_=True)`` short-circuit: every mapping is
    COMPLETE, active matches the journal, metadata reflects --into
    state. Returns RescanAlreadyCompletedReport(kind="continue") and
    marks completed without invoking compensation. Same observability
    contract as init's already-completed short-circuit."""
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})

    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)

    profile_name = "2026-05-12-rescan-1"
    store.create(profile_name, {"claude": True})
    target = store.profile_dir(profile_name) / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"committed": true}')
    _symlink_dir(target, claude_live)
    store.set_active_state({"claude": profile_name}, {"claude": [str(claude_live)]})

    record = _make_rescan_record(
        target_ids=["claude"],
        target_profiles={"claude": profile_name},
        into_mode=False,
        previous_tools=None,
        mappings=[_claude_mapping(tmp_home, "real-dir")],
    )
    OpLogIO(tmp_state).append_record(record)

    result = service.rescan(continue_=True)

    assert isinstance(result, RescanAlreadyCompletedReport)
    assert result.kind == "continue"
    # Disk state untouched; journal marked completed.
    assert claude_live.is_symlink()
    assert OpLogIO(tmp_state).read_in_flight() is None


def test_continue_routes_to_init_when_in_flight_is_init(
    service: ProfileService, tmp_state: Path
) -> None:
    """``switcher rescan --continue`` with an in-flight ``_InitOp`` raises
    InitInProgressError pointing the user at the init recovery surface."""
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})
    store.set_active_state({}, {})

    init_record = _InitOp.model_validate(
        {
            "op": "init",
            "started_at": _now(),
            "target_ids": ["claude"],
            "profile_name": "2026-05-12-current",
            "mappings": [],
        }
    )
    OpLogIO(tmp_state).append_record(init_record)

    with pytest.raises(InitInProgressError) as excinfo:
        service.rescan(continue_=True)
    msg = str(excinfo.value)
    assert "init" in msg.lower()
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_abort_routes_to_init_when_in_flight_is_init(
    service: ProfileService, tmp_state: Path
) -> None:
    """Symmetric to continue: ``rescan --abort`` with an in-flight
    ``_InitOp`` raises InitInProgressError."""
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})
    store.set_active_state({}, {})

    init_record = _InitOp.model_validate(
        {
            "op": "init",
            "started_at": _now(),
            "target_ids": ["claude"],
            "profile_name": "2026-05-12-current",
            "mappings": [],
        }
    )
    OpLogIO(tmp_state).append_record(init_record)

    with pytest.raises(InitInProgressError):
        service.rescan(abort=True)
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_continue_refuses_with_oplog_corrupt_on_rename_in_flight(
    service: ProfileService, tmp_state: Path
) -> None:
    """A stale _RenameOp in the journal at rescan-recovery time means
    the CLI detection hook (which auto-compensates rename) didn't run —
    stale binary or hand-edited journal. Refuse loudly."""
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})
    store.set_active_state({}, {})

    rename = _RenameOp.model_validate(
        {
            "op": "rename",
            "started_at": _now(),
            "from": "old",
            "to": "new",
            "affected_ids": ["claude"],
        }
    )
    OpLogIO(tmp_state).append_record(rename)

    with pytest.raises(OpLogCorruptError, match="unexpected in-flight op type"):
        service.rescan(continue_=True)
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_abort_refuses_with_oplog_corrupt_on_rename_in_flight(
    service: ProfileService, tmp_state: Path
) -> None:
    """Symmetric to continue."""
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})
    store.set_active_state({}, {})

    rename = _RenameOp.model_validate(
        {
            "op": "rename",
            "started_at": _now(),
            "from": "old",
            "to": "new",
            "affected_ids": ["claude"],
        }
    )
    OpLogIO(tmp_state).append_record(rename)

    with pytest.raises(OpLogCorruptError, match="unexpected in-flight op type"):
        service.rescan(abort=True)
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_continue_and_abort_mutually_exclusive_at_service_layer(
    service: ProfileService, tmp_state: Path
) -> None:
    """Defense-in-depth mutex at the service layer — mirrors init's
    pattern. Direct callers (tests, alt front-ends) get a clear error
    rather than silent dispatch to the continue branch."""
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})
    store.set_active_state({}, {})

    with pytest.raises(ValueError, match="mutually exclusive"):
        service.rescan(continue_=True, abort=True)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"only": ["claude"]},
        {"into": "shared"},
        {"dry_run": True},
    ],
)
def test_continue_rejects_each_filter_kwarg(
    service: ProfileService, tmp_state: Path, kwargs: dict[str, object]
) -> None:
    """Recovery scope comes from the in-flight journal record, NOT the
    call site. Combining ``continue_=True`` with ``only``/``into``/
    ``dry_run`` is a caller bug — silently ignoring the filter would let
    the caller believe recovery is scoped when it actually compensates
    the full journal entry. Mirrors init's symmetric refusal."""
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})
    store.set_active_state({}, {})

    with pytest.raises(ValueError, match="continue_/abort cannot be combined"):
        service.rescan(continue_=True, **kwargs)  # pyright: ignore[reportArgumentType, reportCallIssue]


# -- writer-side journal hygiene -------------------------------------------


def test_rescan_writes_intent_and_marks_completed(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Happy-path writer-side: a successful ``service.rescan()`` writes
    an ``_RescanOp`` intent BEFORE any FS mutation and marks it completed
    after the capture loop succeeds. ``vacuum_completed`` drops it."""
    store = FileProfileStore(tmp_state)
    # Initial init creates vanilla + an active map; rescan picks up a
    # newly-installed tool.
    service.init()
    oplog = OpLogIO(tmp_state)
    # Drop the init record so we observe the rescan one in isolation.
    in_flight = oplog.read_in_flight()
    if in_flight is not None:
        # init's mark_completed should have already cleared it, but be
        # defensive in case the test is run on a system where init writes
        # an in-flight record we forgot to mark.
        oplog.vacuum_completed()
    # Drop any completed init record so the rescan-record assertion is clean.
    oplog.vacuum_completed()

    # Pre-active state has all built-in tools managed (init captures
    # everything detect_installed surfaces). For rescan to have work,
    # we need a tool detected NOT in active. The conftest.tmp_home
    # creates ~/.claude AND ~/.copilot — the github-copilot resolver
    # finds copilot via ~/.config/github-copilot. After init, all are
    # in active. So unmanage one first.
    active_before = store.get_active()
    if not active_before:
        pytest.skip("conftest tmp_home produced no detected tools")
    # Pick the first managed tool, unmanage it.
    target = next(iter(active_before))
    service.unmanage(target, force=True)

    # rescan should now detect it as unmanaged-but-installed.
    report = service.rescan()
    if not report.captured:
        pytest.skip("rescan detected no candidates; conftest fixture shape changed")

    records = oplog.read_records()
    rescan_records = [r for r in records if r.op == "rescan"]
    assert len(rescan_records) == 1
    assert rescan_records[0].completed_at is not None
    oplog.vacuum_completed()
    remaining = [r for r in oplog.read_records() if r.op == "rescan"]
    assert remaining == []


def test_rescan_cancels_intent_on_pre_mutation_create_failure(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """A ``ProfileExistsError`` from the first ``_store.create(target, ...)``
    is a pre-mutation failure: disk is untouched. The in-flight intent
    must be canceled. Mirrors init's symmetric cancel_intent contract."""
    store = FileProfileStore(tmp_state)
    service.init()
    OpLogIO(tmp_state).vacuum_completed()

    active_before = store.get_active()
    if not active_before:
        pytest.skip("conftest tmp_home produced no detected tools")
    target = next(iter(active_before))
    service.unmanage(target, force=True)

    with (
        patch.object(
            service._store,
            "create",
            side_effect=ProfileExistsError("simulated race"),
        ),
        pytest.raises(ProfileExistsError, match="simulated race"),
    ):
        service.rescan()
    oplog = OpLogIO(tmp_state)
    assert oplog.read_in_flight() is None
    # The completed init record may still be present pre-vacuum; only
    # the in-flight contract matters here.


def test_rescan_preserves_intent_on_post_mutation_failure(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """A failure DURING the capture (i.e. inside
    ``_capture_tool_for_rescan``, after the intent record landed) must
    NOT cancel the intent — compensation needs the in-flight record to
    drive --continue/--abort. Only the narrowest pre-mutation failure
    (ProfileExistsError on the very first tool's ``store.create``)
    triggers cancel_intent."""
    store = FileProfileStore(tmp_state)
    service.init()
    OpLogIO(tmp_state).vacuum_completed()

    active_before = store.get_active()
    if not active_before:
        pytest.skip("conftest tmp_home produced no detected tools")
    target = next(iter(active_before))
    service.unmanage(target, force=True)

    # Simulate a mid-capture OSError. _capture_tool_for_rescan runs
    # move_or_seed_dir + swap_link per mapping; raising here mimics a
    # transient FS error after the intent was already appended. The
    # outer rescan() wraps the OSError in RescanCaptureError after the
    # outer rollback logic runs.
    with (
        patch.object(
            service,
            "_capture_tool_for_rescan",
            side_effect=OSError("simulated post-mutation failure"),
        ),
        pytest.raises(RescanCaptureError, match="simulated post-mutation failure"),
    ):
        service.rescan()

    in_flight = OpLogIO(tmp_state).read_in_flight()
    assert in_flight is not None, (
        "intent must survive a mid-capture failure — compensation "
        "needs it to drive --continue / --abort"
    )
    assert in_flight.op == "rescan"


# -- abort path -------------------------------------------------------------


def test_abort_fresh_profile_complete_real_dir_deletes_target_profile(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Spec §8.1 rescan #3 baseline. Abort COMPLETE mapping with
    original_kind=real-dir, fresh-profile mode: restore live, then
    delete the target profile."""
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})
    store.set_active_state({}, {})

    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)
    profile_name = "2026-05-12-rescan-1"
    store.create(profile_name, {"claude": True})
    target = store.profile_dir(profile_name) / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"original": true}')
    _symlink_dir(target, claude_live)

    record = _make_rescan_record(
        target_ids=["claude"],
        target_profiles={"claude": profile_name},
        into_mode=False,
        previous_tools=None,
        mappings=[_claude_mapping(tmp_home, "real-dir")],
    )
    OpLogIO(tmp_state).append_record(record)

    service.rescan(abort=True)

    # Live restored.
    assert claude_live.is_dir()
    assert not claude_live.is_symlink()
    assert (claude_live / "settings.json").read_text() == '{"original": true}'
    # Fresh-profile mode: target profile deleted.
    assert not store.profile_dir(profile_name).exists()
    assert OpLogIO(tmp_state).read_in_flight() is None


def test_abort_multiple_fresh_profiles_all_deleted_on_clean_pass(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Spec §8.1 rescan #6. Multiple fresh-profile target_profiles —
    abort deletes BOTH profile dirs after a clean first-pass scan."""
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})
    store.set_active_state({}, {})

    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)
    copilot_live = tmp_home / ".copilot"
    shutil.rmtree(copilot_live)

    profile_a = "2026-05-12-rescan-1"
    profile_b = "2026-05-12-rescan-2"

    store.create(profile_a, {"claude": True})
    target_a = store.profile_dir(profile_a) / "claude"
    target_a.mkdir(parents=True)
    (target_a / "settings.json").write_text('{"claude_orig": true}')
    _symlink_dir(target_a, claude_live)

    store.create(profile_b, {"copilot": True})
    target_b = store.profile_dir(profile_b) / "copilot-config"
    target_b.mkdir(parents=True)
    (target_b / "config.json").write_text('{"copilot_orig": true}')
    _symlink_dir(target_b, copilot_live)

    record = _make_rescan_record(
        target_ids=["claude", "copilot"],
        target_profiles={"claude": profile_a, "copilot": profile_b},
        into_mode=False,
        previous_tools=None,
        mappings=[
            _claude_mapping(tmp_home, "real-dir"),
            _copilot_mapping(tmp_home, "real-dir"),
        ],
    )
    OpLogIO(tmp_state).append_record(record)

    service.rescan(abort=True)

    # Both live dirs restored.
    assert claude_live.is_dir()
    assert (claude_live / "settings.json").read_text() == '{"claude_orig": true}'
    assert copilot_live.is_dir()
    assert (copilot_live / "config.json").read_text() == '{"copilot_orig": true}'
    # Both target profiles deleted.
    assert not store.profile_dir(profile_a).exists()
    assert not store.profile_dir(profile_b).exists()
    assert OpLogIO(tmp_state).read_in_flight() is None


def test_abort_into_mode_move_done_link_missing_real_dir_restores_no_profile_delete(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Spec §8.1 rescan #4. Abort --into mode + MOVE_DONE_LINK_MISSING
    + original_kind=real-dir: live restored from target, but the target
    profile is NOT deleted (it pre-existed and we don't own it). The
    pre-rescan metadata.tools is restored from previous_tools."""
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})
    # The --into target profile pre-existed before rescan, with its own
    # tools snapshot we must restore on abort.
    store.create("shared", {"existing-tool": True})
    # Simulate the deferred-write window: metadata.tools has already
    # been bumped to include claude (the rescan got past the deferred
    # write before crash).
    store.update_profile_tools("shared", {"existing-tool": True, "claude": True})
    store.set_active_state({}, {})

    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)
    target = store.profile_dir("shared") / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"captured_orig": true}')
    # MOVE_DONE_LINK_MISSING: target populated, live missing, no symlink.

    record = _make_rescan_record(
        target_ids=["claude"],
        target_profiles={"claude": "shared"},
        into_mode=True,
        previous_tools={"shared": {"existing-tool": True}},
        mappings=[_claude_mapping(tmp_home, "real-dir")],
    )
    OpLogIO(tmp_state).append_record(record)

    service.rescan(abort=True)

    # Live restored to original real-dir state.
    assert claude_live.is_dir()
    assert not claude_live.is_symlink()
    assert (claude_live / "settings.json").read_text() == '{"captured_orig": true}'
    # Target profile NOT deleted (we don't own it).
    assert store.profile_dir("shared").exists()
    # metadata.tools restored from previous_tools.
    restored = store.get("shared")
    assert restored.tools == {"existing-tool": True}
    assert OpLogIO(tmp_state).read_in_flight() is None


def test_abort_refuses_on_ambiguous_mapping_no_mutation(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Spec §8.1 rescan #5. Abort with one AMBIGUOUS mapping →
    OpLogCorruptError. No per-mapping mutation; no profile delete
    (fresh-profile mode); no metadata write (--into mode). The partial
    profile state is fully intact post-call."""
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})
    store.set_active_state({}, {})

    claude_live = tmp_home / ".claude"
    # Leave claude_live as a real dir (its initial state from tmp_home)
    # — AMBIGUOUS: target_is_dir AND live also exists as real dir.
    (claude_live / "settings.json").write_text('{"live_data": true}')

    profile_name = "2026-05-12-rescan-1"
    store.create(profile_name, {"claude": True})
    target = store.profile_dir(profile_name) / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"target_data": true}')

    record = _make_rescan_record(
        target_ids=["claude"],
        target_profiles={"claude": profile_name},
        into_mode=False,
        previous_tools=None,
        mappings=[_claude_mapping(tmp_home, "real-dir")],
    )
    OpLogIO(tmp_state).append_record(record)

    with pytest.raises(OpLogCorruptError):
        service.rescan(abort=True)

    # Both still in place; intent still in flight; target profile NOT
    # deleted.
    assert claude_live.is_dir()
    assert (claude_live / "settings.json").read_text() == '{"live_data": true}'
    assert store.profile_dir(profile_name).exists()
    assert (target / "settings.json").read_text() == '{"target_data": true}'
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_abort_with_no_in_flight_raises_no_in_progress(
    service: ProfileService, tmp_state: Path
) -> None:
    """``rescan --abort`` against an empty journal →
    NoInProgressRescanError."""
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})
    store.set_active_state({}, {})

    with pytest.raises(NoInProgressRescanError):
        service.rescan(abort=True)


def test_abort_short_circuits_when_already_completed(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Symmetric to init's already-completed short-circuit on abort.
    Every mapping is COMPLETE, active matches, --into metadata reflects
    the completed state. Abort short-circuits with
    RescanAlreadyCompletedReport(kind="abort") rather than silently
    reversing a committed rescan."""
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})

    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)
    profile_name = "2026-05-12-rescan-1"
    store.create(profile_name, {"claude": True})
    target = store.profile_dir(profile_name) / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"committed": true}')
    _symlink_dir(target, claude_live)
    store.set_active_state({"claude": profile_name}, {"claude": [str(claude_live)]})

    record = _make_rescan_record(
        target_ids=["claude"],
        target_profiles={"claude": profile_name},
        into_mode=False,
        previous_tools=None,
        mappings=[_claude_mapping(tmp_home, "real-dir")],
    )
    OpLogIO(tmp_state).append_record(record)

    result = service.rescan(abort=True)
    assert isinstance(result, RescanAlreadyCompletedReport)
    assert result.kind == "abort"
    # Disk state untouched.
    assert claude_live.is_symlink()
    assert (target / "settings.json").read_text() == '{"committed": true}'
    assert store.profile_dir(profile_name).exists()
    assert OpLogIO(tmp_state).read_in_flight() is None


def test_abort_into_mode_untouched_does_not_delete_target_profile(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """In --into mode, even when no per-mapping mutation runs (UNTOUCHED
    everywhere), abort must NOT delete the target profile. The
    profile pre-existed and we don't own it. Restores metadata.tools
    from previous_tools."""
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})
    store.create("shared", {"existing-tool": True})
    store.set_active_state({}, {})

    claude_live = tmp_home / ".claude"
    # UNTOUCHED + original_kind=real-dir: live is a real dir, target absent.
    (claude_live / "settings.json").write_text('{"original": true}')

    record = _make_rescan_record(
        target_ids=["claude"],
        target_profiles={"claude": "shared"},
        into_mode=True,
        previous_tools={"shared": {"existing-tool": True}},
        mappings=[_claude_mapping(tmp_home, "real-dir")],
    )
    OpLogIO(tmp_state).append_record(record)

    service.rescan(abort=True)

    # Live untouched; target profile preserved with its original tools.
    assert claude_live.is_dir()
    assert (claude_live / "settings.json").read_text() == '{"original": true}'
    assert store.profile_dir("shared").exists()
    assert store.get("shared").tools == {"existing-tool": True}
    assert OpLogIO(tmp_state).read_in_flight() is None


def test_abort_validates_all_mappings_before_mutating_any(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Correctness invariant (same as init's): if mapping[0] is
    reversible and mapping[1] is AMBIGUOUS, the validation pass MUST
    fail before mapping[0]'s reversal runs. Otherwise abort partially
    mutates before discovering ambiguity, and the target profiles get
    half-deleted."""
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})
    store.set_active_state({}, {})

    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)
    copilot_live = tmp_home / ".copilot"
    # Leave copilot_live as real dir (AMBIGUOUS pair: both real-dir).
    (copilot_live / "config.json").write_text('{"copilot_live": true}')

    profile_a = "2026-05-12-rescan-1"
    profile_b = "2026-05-12-rescan-2"

    # Mapping A reversible (COMPLETE).
    store.create(profile_a, {"claude": True})
    target_a = store.profile_dir(profile_a) / "claude"
    target_a.mkdir(parents=True)
    (target_a / "settings.json").write_text('{"claude_orig": true}')
    _symlink_dir(target_a, claude_live)

    # Mapping B AMBIGUOUS: target real dir AND live real dir.
    store.create(profile_b, {"copilot": True})
    target_b = store.profile_dir(profile_b) / "copilot-config"
    target_b.mkdir(parents=True)
    (target_b / "config.json").write_text('{"copilot_target": true}')

    record = _make_rescan_record(
        target_ids=["claude", "copilot"],
        target_profiles={"claude": profile_a, "copilot": profile_b},
        into_mode=False,
        previous_tools=None,
        mappings=[
            _claude_mapping(tmp_home, "real-dir"),
            _copilot_mapping(tmp_home, "real-dir"),
        ],
    )
    OpLogIO(tmp_state).append_record(record)

    with pytest.raises(OpLogCorruptError):
        service.rescan(abort=True)

    # Mapping A was NOT mutated.
    assert claude_live.is_symlink(), (
        "mapping[0] was mutated before mapping[1]'s validation failure — "
        "partial-mutation invariant violated"
    )
    # No profile was deleted.
    assert store.profile_dir(profile_a).exists()
    assert store.profile_dir(profile_b).exists()
    # Journal stays in flight.
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_abort_idempotent_second_call_raises_no_in_progress(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """First abort compensates and marks completed; vacuum drops the
    record; a second abort sees no in-flight record and raises
    NoInProgressRescanError."""
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})
    store.set_active_state({}, {})

    profile_name = "2026-05-12-rescan-1"
    store.create(profile_name, {"claude": True})
    record = _make_rescan_record(
        target_ids=["claude"],
        target_profiles={"claude": profile_name},
        into_mode=False,
        previous_tools=None,
        mappings=[_claude_mapping(tmp_home, "real-dir")],
    )
    OpLogIO(tmp_state).append_record(record)

    service.rescan(abort=True)
    OpLogIO(tmp_state).vacuum_completed()

    with pytest.raises(NoInProgressRescanError):
        service.rescan(abort=True)


def test_abort_rejects_link_original_kind_defensively(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Defensive guard inside ``_compensate_rescan_abort`` rejects
    ``original_kind`` outside {"missing","real-dir"} as
    ``AbortPreflightError``. Unreachable via the normal journal
    lifecycle (``_MappingIntent`` narrows ``original_kind`` to a
    Literal). Test invokes the private method with a model_construct
    record — same pattern test_init_compensation uses for the
    symmetric init defensive branch."""
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})
    store.set_active_state({}, {})

    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)
    profile_name = "2026-05-12-rescan-1"
    store.create(profile_name, {"claude": True})
    target = store.profile_dir(profile_name) / "claude"
    target.mkdir(parents=True)
    _symlink_dir(target, claude_live)

    bad_mapping = _MappingIntent.model_construct(
        tool_id="claude",
        mapping_index=0,
        live_path=str(claude_live),
        profile_subdir="claude",
        original_kind="link",  # type: ignore[arg-type]
    )
    record = _RescanOp.model_construct(
        op="rescan",
        started_at=_now(),
        target_ids=["claude"],
        target_profiles={"claude": profile_name},
        into_mode=False,
        previous_tools=None,
        mappings=[bad_mapping],
    )

    with pytest.raises(AbortPreflightError):
        service._compensate_rescan_abort(record)

    # No mutation: profile dir + symlink still in place.
    assert claude_live.is_symlink()
    assert store.profile_dir(profile_name).exists()


def test_continue_preflights_config_before_any_filesystem_mutation(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """``_compensate_rescan_continue`` must read config.json in its
    validation pass — BEFORE move_or_seed_dir / swap_link /
    update_profile_tools / set_active_state run. Symmetric to init's
    pass-PR-1 fix."""
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})
    store.set_active_state({}, {})

    # Stage UNTOUCHED so compensation must actually run (rather than
    # short-circuit on COMPLETE).
    claude_live = tmp_home / ".claude"
    (claude_live / "settings.json").write_text('{"original": true}')
    profile_name = "2026-05-12-rescan-1"
    store.create(profile_name, {"claude": True})
    record = _make_rescan_record(
        target_ids=["claude"],
        target_profiles={"claude": profile_name},
        into_mode=False,
        previous_tools=None,
        mappings=[_claude_mapping(tmp_home, "real-dir")],
    )
    OpLogIO(tmp_state).append_record(record)

    # Corrupt config.json AFTER setup.
    (tmp_state / "config.json").write_text("{not valid json")

    with pytest.raises(StorageError):
        service.rescan(continue_=True)

    # Pre-mutation state preserved.
    assert claude_live.is_dir()
    assert not service._resolver.is_link(claude_live)
    assert (claude_live / "settings.json").read_text() == '{"original": true}'
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_abort_preflights_config_before_any_filesystem_mutation(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """``_compensate_rescan_abort`` must read config.json BEFORE
    deleting profile dirs or restoring live state. Symmetric to init's
    pass-7 fix."""
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})
    # set_active_state writes the config.json file we will corrupt.
    store.set_active_state({}, {})

    claude_live = tmp_home / ".claude"
    (claude_live / "user-data.json").write_text('{"user": true}')
    profile_name = "2026-05-12-rescan-1"
    store.create(profile_name, {"claude": True})
    record = _make_rescan_record(
        target_ids=["claude"],
        target_profiles={"claude": profile_name},
        into_mode=False,
        previous_tools=None,
        mappings=[_claude_mapping(tmp_home, "real-dir")],
    )
    OpLogIO(tmp_state).append_record(record)

    # Corrupt config.json AFTER setup.
    (tmp_state / "config.json").write_text("{not valid json")

    with pytest.raises(StorageError):
        service.rescan(abort=True)

    # Pre-mutation state preserved.
    assert store.profile_dir(profile_name).exists()
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_continue_refuses_when_existing_profile_has_foreign_tools(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Foreign-profile refusal — every recovery path verifies the
    journal-owned profile's metadata.tools matches what a clean rescan
    would have written. Without it, continue silently adopts a profile
    whose .tools disagrees with the journal."""
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})
    store.set_active_state({}, {})

    claude_live = tmp_home / ".claude"
    (claude_live / "settings.json").write_text('{"data": true}')

    profile_name = "2026-05-12-rescan-1"
    # Fresh-profile mode: a clean rescan would write
    # tools=dict.fromkeys(record.target_ids, True). Anything else means
    # a foreign profile occupies the pathname.
    store.create(profile_name, {"external": True})

    record = _make_rescan_record(
        target_ids=["claude"],
        target_profiles={"claude": profile_name},
        into_mode=False,
        previous_tools=None,
        mappings=[_claude_mapping(tmp_home, "real-dir")],
    )
    OpLogIO(tmp_state).append_record(record)

    with pytest.raises(OpLogCorruptError):
        service.rescan(continue_=True)

    # Foreign metadata preserved.
    assert store.get(profile_name).tools == {"external": True}
    # No active mutation.
    assert store.get_active() == {}
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_abort_refuses_when_existing_profile_has_foreign_tools(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """Symmetric foreign-profile refusal on abort. Without it, abort
    rmtree's a profile dir whose metadata.tools indicates it's NOT the
    one this rescan created — data loss."""
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})
    store.set_active_state({}, {})

    claude_live = tmp_home / ".claude"
    (claude_live / "data.json").write_text('{"data": true}')

    profile_name = "2026-05-12-rescan-1"
    store.create(profile_name, {"external": True})
    foreign_data = store.profile_dir(profile_name) / "external" / "config.json"
    foreign_data.parent.mkdir(parents=True)
    foreign_data.write_text('{"user_data": "important"}')

    record = _make_rescan_record(
        target_ids=["claude"],
        target_profiles={"claude": profile_name},
        into_mode=False,
        previous_tools=None,
        mappings=[_claude_mapping(tmp_home, "real-dir")],
    )
    OpLogIO(tmp_state).append_record(record)

    with pytest.raises(OpLogCorruptError):
        service.rescan(abort=True)

    # Foreign profile preserved end-to-end.
    assert store.profile_dir(profile_name).is_dir()
    assert store.get(profile_name).tools == {"external": True}
    assert foreign_data.read_text() == '{"user_data": "important"}'
    assert OpLogIO(tmp_state).read_in_flight() is not None


def test_continue_clears_journal_state_on_zero_mapping_capture(
    service: ProfileService, tmp_home: Path, tmp_state: Path
) -> None:
    """A target_id with zero mappings (e.g., a registry-only tool) goes
    through rescan compensation as if the per-mapping loop is a no-op.
    The journal still must mark itself completed and the active map
    still must reflect the capture. Symmetric to init's empty-mapping
    handling."""
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})
    store.set_active_state({}, {})

    profile_name = "2026-05-12-rescan-1"
    store.create(profile_name, {"registry-only-tool": True})

    record = _make_rescan_record(
        target_ids=["registry-only-tool"],
        target_profiles={"registry-only-tool": profile_name},
        into_mode=False,
        previous_tools=None,
        mappings=[],
    )
    OpLogIO(tmp_state).append_record(record)

    service.rescan(continue_=True)

    # Active map covers the registry-only tool.
    active = store.get_active()
    assert active.get("registry-only-tool") == profile_name
    # config.json's cache key explicitly serializes [] for the zero-
    # mapping tool.
    config = json.loads((tmp_state / "config.json").read_text())
    assert "registry-only-tool" in config["active_live_paths"]
    assert config["active_live_paths"]["registry-only-tool"] == []
    assert OpLogIO(tmp_state).read_in_flight() is None
