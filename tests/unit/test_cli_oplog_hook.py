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


def test_format_in_progress_hint_init_mentions_profile_and_manual_recovery() -> None:
    text = _format_in_progress_hint(_init_record())
    assert "init" in text
    assert "2026-05-12-current" in text
    assert "claude" in text
    assert "Manual recovery required" in text
    # init's --continue/--abort flags don't ship until Phase 6 / PR4;
    # in a PR3-only deployment they're unknown to the CLI. The hint
    # documents the future surface without pointing the user at flags
    # the running binary doesn't have (abby blocking review).
    assert "follow-on release" in text


def test_format_in_progress_hint_rescan_fresh_mentions_fresh_profile() -> None:
    text = _format_in_progress_hint(_rescan_record_fresh())
    assert "rescan" in text
    assert "claude" in text
    assert "fresh-profile" in text
    assert "Manual recovery required" in text
    assert "follow-on release" in text


def test_format_in_progress_hint_rescan_into_mentions_into_target() -> None:
    text = _format_in_progress_hint(_rescan_record_into())
    assert "rescan" in text
    # No "--into shared" bare-flag wording — that flag is fine on its
    # own as a rescan invocation, but the hint stays version-agnostic:
    # name the target profile, don't tell the user to run any specific
    # CLI shape that varies by switcher version.
    assert "shared" in text
    assert "claude" in text
    assert "Manual recovery required" in text


def test_format_in_progress_hint_does_not_reference_nonexistent_flags() -> None:
    """A PR3 binary doesn't expose `init --continue/--abort` or
    `rescan --continue/--abort`. The hint must not direct the user to
    run them — they would fail with "no such option" and burn the
    user's first recovery attempt (abby blocking review). When PR4 /
    PR5 add the flags, the hint text gets updated alongside."""
    init_text = _format_in_progress_hint(_init_record())
    rescan_fresh_text = _format_in_progress_hint(_rescan_record_fresh())
    rescan_into_text = _format_in_progress_hint(_rescan_record_into())
    # Substring `--continue` / `--abort` (with leading hyphens) is the
    # CLI-flag form the user would actually type; the prose mentions
    # the *commands* without the flag-prefix to document the
    # forthcoming compensation surface.
    for text in (init_text, rescan_fresh_text, rescan_into_text):
        assert "--continue" not in text
        assert "--abort" not in text


def test_format_in_progress_hint_rescan_into_multi_value_marks_corrupt() -> None:
    """A hand-edited journal could carry into_mode=True with multiple
    distinct target_profiles values. _RescanOp's validators don't enforce
    the spec's singleton invariant, so the hint must not emit a single
    bogus profile name (abby review). Marker-text only — the
    profiles_desc line below still shows the full mapping."""
    record = _RescanOp.model_validate(
        {
            "op": "rescan",
            "started_at": _now(),
            "target_ids": ["claude", "copilot"],
            "target_profiles": {"claude": "alpha", "copilot": "beta"},
            "into_mode": True,
            "previous_tools": {
                "alpha": {"claude": True},
                "beta": {"copilot": True},
            },
            "mappings": [],
        }
    )
    text = _format_in_progress_hint(record)
    assert "corrupt" in text
    assert "alpha" in text
    assert "beta" in text


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

    Initializes switcher (no op-log record produced — service.init
    doesn't write _InitOp intent records until Phase 6 / PR4), then
    injects an in-flight `_InitOp` directly into the journal to mimic
    an interrupted run. `status` must surface the recovery hint and
    exit 3 — proving the hook is wired in and that typer.Exit
    propagates through handle_errors.
    """
    runner = CliRunner()
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, setup.stdout

    oplog = OpLogIO(tmp_state)
    oplog.append_record(_init_record())

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 3, result.stdout
    assert "Interrupted `switcher init`" in result.stdout
    assert "Manual recovery required" in result.stdout
