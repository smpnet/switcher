# pyright: reportPrivateUsage=none
"""Tests for init op-log compensation (spec §2.2).

`service.init(continue_=True)` and `service.init(abort=True)` drive
disk-truth compensation against the §2.1.1 four-state classifier. The
shared fixtures (`tmp_home`, `tmp_state`, `service`) come from
conftest.py — same pattern as test_rename_compensation.py."""

from __future__ import annotations

import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from switcher.errors import (
    AbortPreflightError,
    NoInProgressInitError,
    OpLogCorruptError,
)
from switcher.models import Tool
from switcher.oplog import OpLogIO, _InitOp, _MappingIntent
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
