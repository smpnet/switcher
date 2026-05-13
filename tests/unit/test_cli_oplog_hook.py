# pyright: reportPrivateUsage=none, reportArgumentType=none
# reportArgumentType=none: the helper takes a Deps (frozen dataclass);
# tests pass a structurally-equivalent _FakeDeps that exposes just the
# oplog + service slice the hook touches. The runtime is fine; only the
# nominal type check would object.
"""Tests for the CLI op-log detection hook (spec §2.2).

The hook runs at the top of every command callback. Its responsibilities:
- vacuum completed records,
- auto-compensate an in-flight rename (idempotent disk-truth roll-forward;
  the service method owns mark_completed — the hook must NOT replicate it),
- surface in-flight init/rescan via typer.Exit(3) for read-only callers,
  or InitInProgressError / RescanInProgressError for mutating callers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
import typer

from switcher.cli import _detect_or_compensate_oplog, _format_in_progress_hint
from switcher.errors import InitInProgressError, RescanInProgressError
from switcher.oplog import OpLogRecord, _InitOp, _RenameOp, _RescanOp


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


# Small fakes over MagicMock: the hook touches a tiny stable surface
# (deps.oplog.{vacuum_completed,read_in_flight,mark_completed,cancel_intent}
# and deps.service._compensate_rename). Hand-rolled fakes catch interface
# drift the moment a method is renamed or added (a MagicMock attribute
# lookup always succeeds), and match the project's no-mocks-unless-
# unavoidable convention (CodeRabbit review).


@dataclass
class _FakeOpLog:
    """Captures call history without mocking — every method records a
    call so tests can assert ordering and call counts."""

    in_flight: OpLogRecord | None = None
    calls: list[str] = field(default_factory=list)
    mark_completed_args: list[OpLogRecord] = field(default_factory=list)
    cancel_intent_args: list[OpLogRecord] = field(default_factory=list)

    def vacuum_completed(self) -> None:
        self.calls.append("vacuum_completed")

    def read_in_flight(self) -> OpLogRecord | None:
        self.calls.append("read_in_flight")
        return self.in_flight

    def mark_completed(self, record: OpLogRecord) -> None:
        self.calls.append("mark_completed")
        self.mark_completed_args.append(record)

    def cancel_intent(self, record: OpLogRecord) -> None:
        self.calls.append("cancel_intent")
        self.cancel_intent_args.append(record)


@dataclass
class _FakeService:
    """Mirrors the slice of ProfileService the hook calls."""

    compensate_rename_args: list[_RenameOp] = field(default_factory=list)

    def _compensate_rename(self, record: _RenameOp) -> None:
        self.compensate_rename_args.append(record)


@dataclass
class _FakeDeps:
    """The hook only consumes `.oplog` and `.service`; nothing else is
    exercised, so the fake stays minimal. Constructor accepts an
    optional in-flight record to seed the read_in_flight return."""

    oplog: _FakeOpLog
    service: _FakeService

    @classmethod
    def with_in_flight(cls, in_flight: OpLogRecord | None) -> _FakeDeps:
        return cls(oplog=_FakeOpLog(in_flight=in_flight), service=_FakeService())


# -- no-op path -------------------------------------------------------------


def test_no_in_flight_read_only_returns_silently() -> None:
    deps = _FakeDeps.with_in_flight(None)
    _detect_or_compensate_oplog(deps, allow_mutation=False)
    assert deps.oplog.calls == ["vacuum_completed", "read_in_flight"]
    assert deps.service.compensate_rename_args == []
    assert deps.oplog.mark_completed_args == []


def test_no_in_flight_mutating_returns_silently() -> None:
    deps = _FakeDeps.with_in_flight(None)
    _detect_or_compensate_oplog(deps, allow_mutation=True)
    assert deps.oplog.calls == ["vacuum_completed", "read_in_flight"]
    assert deps.service.compensate_rename_args == []


def test_vacuum_runs_before_read_in_flight() -> None:
    """Vacuum must happen before any decision is made on read_in_flight,
    so a previous run's completed record can't masquerade as in-flight
    via a stale snapshot."""
    deps = _FakeDeps.with_in_flight(None)
    _detect_or_compensate_oplog(deps, allow_mutation=False)
    assert deps.oplog.calls.index("vacuum_completed") < deps.oplog.calls.index("read_in_flight")


# -- rename path (auto-compensates regardless of allow_mutation) ------------


def test_in_flight_rename_auto_compensates_read_only() -> None:
    """Read-only callers still auto-compensate an in-flight rename — the
    compensation is idempotent disk-truth roll-forward of a state the
    user already committed (spec §2.2)."""
    record = _rename_record()
    deps = _FakeDeps.with_in_flight(record)
    _detect_or_compensate_oplog(deps, allow_mutation=False)
    assert deps.service.compensate_rename_args == [record]


def test_in_flight_rename_auto_compensates_mutating() -> None:
    record = _rename_record()
    deps = _FakeDeps.with_in_flight(record)
    _detect_or_compensate_oplog(deps, allow_mutation=True)
    assert deps.service.compensate_rename_args == [record]


def test_in_flight_rename_does_not_mark_completed_in_hook() -> None:
    """ProfileService._compensate_rename already calls mark_completed
    itself (service.py). The hook must not duplicate that — a second
    mark_completed would raise on the already-completed record."""
    record = _rename_record()
    deps = _FakeDeps.with_in_flight(record)
    _detect_or_compensate_oplog(deps, allow_mutation=True)
    assert deps.oplog.mark_completed_args == []
    assert "mark_completed" not in deps.oplog.calls


# -- init in-flight ---------------------------------------------------------


def test_in_flight_init_read_only_raises_typer_exit_3() -> None:
    deps = _FakeDeps.with_in_flight(_init_record())
    with pytest.raises(typer.Exit) as excinfo:
        _detect_or_compensate_oplog(deps, allow_mutation=False)
    assert excinfo.value.exit_code == 3


def test_in_flight_init_mutating_raises_init_in_progress() -> None:
    deps = _FakeDeps.with_in_flight(_init_record())
    with pytest.raises(InitInProgressError):
        _detect_or_compensate_oplog(deps, allow_mutation=True)


# -- rescan in-flight -------------------------------------------------------


def test_in_flight_rescan_read_only_raises_typer_exit_3() -> None:
    deps = _FakeDeps.with_in_flight(_rescan_record_fresh())
    with pytest.raises(typer.Exit) as excinfo:
        _detect_or_compensate_oplog(deps, allow_mutation=False)
    assert excinfo.value.exit_code == 3


def test_in_flight_rescan_mutating_raises_rescan_in_progress() -> None:
    deps = _FakeDeps.with_in_flight(_rescan_record_fresh())
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


def test_status_with_in_flight_init_exits_3_and_emits_hint(tmp_home: Path, tmp_state: Path) -> None:
    """End-to-end smoke for spec §2.2 read-only dispatch.

    Initializes switcher (no op-log record produced — service.init
    doesn't write _InitOp intent records until Phase 6 / PR4), then
    injects an in-flight `_InitOp` directly into the journal to mimic
    an interrupted run. `status` must surface the recovery hint text
    and exit 3 — proving the hook is wired in and that typer.Exit
    propagates through handle_errors.

    Stream routing (stderr-not-stdout) is verified separately by
    test_in_flight_hint_goes_through_err_console — bypassing
    CliRunner's stream-capture mechanics avoids depending on Click
    version-specific mix_stderr behavior (abby review).
    """
    runner = CliRunner()
    setup = runner.invoke(app, ["init"])
    assert setup.exit_code == 0, setup.output

    oplog = OpLogIO(tmp_state)
    oplog.append_record(_init_record())

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 3, result.output
    # Hint text reaches the user. Don't constrain WHICH stream here:
    # CliRunner's stdout/stderr split depends on Click version and
    # mix_stderr config. Routing is asserted at the console-call
    # level in the err_console test below.
    assert "Interrupted `switcher init`" in result.output
    assert "Manual recovery required" in result.output


def test_in_flight_hint_goes_through_err_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct routing test: the read-only hook must call err_console
    (stderr), not console (stdout). Verifying via direct monkeypatch
    instead of CliRunner stream capture avoids Click 8.2 vs <8.2
    mix_stderr ambiguity (abby review) — the OS-level stream
    separation in a real shell is guaranteed regardless of test
    framework; we just need to confirm the code picked the right
    Console object.
    """
    from switcher import cli as cli_module

    err_calls: list[str] = []
    console_calls: list[str] = []

    def _capture_err(msg: object, *_: object, **__: object) -> None:
        err_calls.append(str(msg))

    def _capture_console(msg: object, *_: object, **__: object) -> None:
        console_calls.append(str(msg))

    monkeypatch.setattr(cli_module.err_console, "print", _capture_err)
    monkeypatch.setattr(cli_module.console, "print", _capture_console)
    deps = _FakeDeps.with_in_flight(_init_record())
    with pytest.raises(typer.Exit):
        _detect_or_compensate_oplog(deps, allow_mutation=False)
    # Routing assertion: stderr got the hint, stdout did NOT.
    assert any("Interrupted `switcher init`" in c for c in err_calls)
    assert not any("Interrupted `switcher init`" in c for c in console_calls)
