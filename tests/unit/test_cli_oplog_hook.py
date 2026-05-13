# pyright: reportPrivateUsage=none
"""Tests for the CLI op-log detection hook (spec §2.2).

The hook runs at the top of every command callback. Its responsibilities:
- vacuum completed records,
- auto-compensate an in-flight rename (idempotent disk-truth roll-forward;
  the service method owns mark_completed — the hook must NOT replicate it),
- surface in-flight init/rescan via typer.Exit(3) for read-only callers,
  or InitInProgressError / RescanInProgressError for mutating callers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
import typer

from switcher.cli import _detect_or_compensate_oplog, _format_in_progress_hint
from switcher.errors import InitInProgressError, RescanInProgressError
from switcher.oplog import _InitOp, _RenameOp, _RescanOp


def _now() -> datetime:
    return datetime(2026, 5, 12, 10, 30, tzinfo=UTC)


def _rename_record() -> _RenameOp:
    # model_validate accepts the JSON-side alias `from` without tripping
    # basedpyright on `**{"from": ...}` kwargs (carry-forward from PR3 batch 1).
    return _RenameOp.model_validate(
        {
            "op": "rename",
            "started_at": _now(),
            "from": "old",
            "to": "new",
            "affected_ids": ["claude"],
        }
    )


def _init_record() -> _InitOp:
    return _InitOp.model_validate(
        {
            "op": "init",
            "started_at": _now(),
            "target_ids": ["claude"],
            "profile_name": "2026-05-12-current",
            "mappings": [],
        }
    )


def _rescan_record_fresh() -> _RescanOp:
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


def _rescan_record_into() -> _RescanOp:
    return _RescanOp.model_validate(
        {
            "op": "rescan",
            "started_at": _now(),
            "target_ids": ["claude"],
            "target_profiles": {"claude": "shared"},
            "into_mode": True,
            "previous_tools": {"shared": {"claude": True}},
            "mappings": [],
        }
    )


def _make_deps(in_flight: object | None = None) -> MagicMock:
    """Build a MagicMock Deps whose oplog returns `in_flight` from read_in_flight."""
    deps = MagicMock()
    deps.oplog.read_in_flight.return_value = in_flight
    deps.oplog.vacuum_completed.return_value = None
    return deps


# -- no-op path -------------------------------------------------------------


def test_no_in_flight_read_only_returns_silently() -> None:
    deps = _make_deps(in_flight=None)
    _detect_or_compensate_oplog(deps, allow_mutation=False)
    deps.oplog.vacuum_completed.assert_called_once()
    deps.oplog.read_in_flight.assert_called_once()
    deps.service._compensate_rename.assert_not_called()
    deps.oplog.mark_completed.assert_not_called()


def test_no_in_flight_mutating_returns_silently() -> None:
    deps = _make_deps(in_flight=None)
    _detect_or_compensate_oplog(deps, allow_mutation=True)
    deps.oplog.vacuum_completed.assert_called_once()
    deps.service._compensate_rename.assert_not_called()


def test_vacuum_runs_before_read_in_flight() -> None:
    """Vacuum must happen before any decision is made on read_in_flight,
    so a previous run's completed record can't masquerade as in-flight
    via a stale snapshot."""
    deps = _make_deps(in_flight=None)
    parent = MagicMock()
    parent.attach_mock(deps.oplog.vacuum_completed, "vacuum")
    parent.attach_mock(deps.oplog.read_in_flight, "read")
    _detect_or_compensate_oplog(deps, allow_mutation=False)
    call_names = [c[0] for c in parent.mock_calls]
    assert call_names.index("vacuum") < call_names.index("read")


# -- rename path (auto-compensates regardless of allow_mutation) ------------


def test_in_flight_rename_auto_compensates_read_only() -> None:
    """Read-only callers still auto-compensate an in-flight rename — the
    compensation is idempotent disk-truth roll-forward of a state the
    user already committed (spec §2.2)."""
    record = _rename_record()
    deps = _make_deps(in_flight=record)
    _detect_or_compensate_oplog(deps, allow_mutation=False)
    deps.service._compensate_rename.assert_called_once_with(record)


def test_in_flight_rename_auto_compensates_mutating() -> None:
    record = _rename_record()
    deps = _make_deps(in_flight=record)
    _detect_or_compensate_oplog(deps, allow_mutation=True)
    deps.service._compensate_rename.assert_called_once_with(record)


def test_in_flight_rename_does_not_mark_completed_in_hook() -> None:
    """ProfileService._compensate_rename already calls mark_completed
    itself (service.py:1265). The hook must not duplicate that — a
    second mark_completed would raise on the already-completed record."""
    record = _rename_record()
    deps = _make_deps(in_flight=record)
    _detect_or_compensate_oplog(deps, allow_mutation=True)
    deps.oplog.mark_completed.assert_not_called()


# -- init in-flight ---------------------------------------------------------


def test_in_flight_init_read_only_raises_typer_exit_3() -> None:
    deps = _make_deps(in_flight=_init_record())
    with pytest.raises(typer.Exit) as excinfo:
        _detect_or_compensate_oplog(deps, allow_mutation=False)
    assert excinfo.value.exit_code == 3


def test_in_flight_init_mutating_raises_init_in_progress() -> None:
    deps = _make_deps(in_flight=_init_record())
    with pytest.raises(InitInProgressError):
        _detect_or_compensate_oplog(deps, allow_mutation=True)


# -- rescan in-flight -------------------------------------------------------


def test_in_flight_rescan_read_only_raises_typer_exit_3() -> None:
    deps = _make_deps(in_flight=_rescan_record_fresh())
    with pytest.raises(typer.Exit) as excinfo:
        _detect_or_compensate_oplog(deps, allow_mutation=False)
    assert excinfo.value.exit_code == 3


def test_in_flight_rescan_mutating_raises_rescan_in_progress() -> None:
    deps = _make_deps(in_flight=_rescan_record_fresh())
    with pytest.raises(RescanInProgressError):
        _detect_or_compensate_oplog(deps, allow_mutation=True)


# -- hint formatting --------------------------------------------------------


def test_format_in_progress_hint_init_mentions_profile_and_continue_abort() -> None:
    text = _format_in_progress_hint(_init_record())
    assert "init" in text
    assert "2026-05-12-current" in text
    assert "claude" in text
    assert "--continue" in text
    assert "--abort" in text


def test_format_in_progress_hint_rescan_fresh_mentions_fresh_profile() -> None:
    text = _format_in_progress_hint(_rescan_record_fresh())
    assert "rescan" in text
    assert "claude" in text
    assert "fresh-profile" in text
    assert "--continue" in text
    assert "--abort" in text


def test_format_in_progress_hint_rescan_into_mentions_into_target() -> None:
    text = _format_in_progress_hint(_rescan_record_into())
    assert "rescan" in text
    assert "--into shared" in text
    assert "claude" in text
    assert "--continue" in text
    assert "--abort" in text


# -- end-to-end CLI propagation smoke -------------------------------------
#
# One smoke test exercises the full hook path: callback → hook →
# typer.Exit → CliRunner exit code. The integration suite in Phase 9
# covers the matrix; this single test catches the propagation wiring
# (a regression here would silence ALL hook surface for read-only
# commands).


from pathlib import Path  # noqa: E402  (group end-to-end imports near use)

from typer.testing import CliRunner  # noqa: E402

from switcher.cli import app  # noqa: E402
from switcher.oplog import OpLogIO  # noqa: E402


def test_status_with_in_flight_init_exits_3_and_prints_hint(
    tmp_home: Path, tmp_state: Path
) -> None:
    """End-to-end smoke for spec §2.2 read-only dispatch.

    Initializes switcher (which writes + completes its own init record),
    then injects a second in-flight `_InitOp` into the journal to mimic
    an interrupted run. `status` must vacuum the completed record,
    surface the recovery hint, and exit 3 — proving the hook is wired
    in and that typer.Exit propagates through handle_errors.
    """
    runner = CliRunner()
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, setup.stdout

    oplog = OpLogIO(tmp_state)
    oplog.append_record(_init_record())

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 3, result.stdout
    assert "Interrupted `switcher init`" in result.stdout
    assert "--continue" in result.stdout
    assert "--abort" in result.stdout
