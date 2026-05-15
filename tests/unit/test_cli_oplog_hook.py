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
from typing import Any

import pytest
import typer

from switcher.cli import _detect_or_compensate_oplog, _format_in_progress_hint
from switcher.errors import (
    InitInProgressError,
    NoInProgressInitError,
    RescanInProgressError,
)
from switcher.oplog import OpLogRecord, _InitOp, _RenameOp, _RescanOp
from switcher.service import InitAlreadyCompletedReport, RescanAlreadyCompletedReport


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
    """Mirrors the slice of ProfileService the hook calls.

    v0.1.5 PR4: also records ``service.init(continue_=..., abort=...)``
    invocations and a configurable return value so the init-recovery CLI
    tests can exercise the InitAlreadyCompletedReport vs None branches
    without MagicMock (project convention, per coding guidelines).

    v0.1.5 PR5: extended with the same shape for ``service.rescan(
    continue_=..., abort=...)`` so the rescan-recovery CLI tests can
    cover the same matrix.
    """

    compensate_rename_args: list[_RenameOp] = field(default_factory=list)
    init_calls: list[dict[str, Any]] = field(default_factory=list)
    init_return_value: Any = None
    init_side_effect: BaseException | None = None
    rescan_calls: list[dict[str, Any]] = field(default_factory=list)
    rescan_return_value: Any = None
    rescan_side_effect: BaseException | None = None

    def _compensate_rename(self, record: _RenameOp) -> None:
        self.compensate_rename_args.append(record)

    def init(
        self,
        target_ids: object | None = None,
        **kwargs: object,
    ) -> Any:
        # Capture full call shape so tests can assert ordering AND the
        # exact (continue_, abort, target_ids) tuple — a regression where
        # the CLI mistakenly forwarded --only / --skip would show up as
        # a non-None target_ids in the recorded dict.
        self.init_calls.append({"target_ids": target_ids, **kwargs})
        if self.init_side_effect is not None:
            raise self.init_side_effect
        return self.init_return_value

    def rescan(self, **kwargs: object) -> Any:
        self.rescan_calls.append(dict(kwargs))
        if self.rescan_side_effect is not None:
            raise self.rescan_side_effect
        return self.rescan_return_value


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
    """Read-only paths never write to disk — vacuum is intentionally
    skipped (CodeRabbit review). The hook only reads in_flight; the
    next mutating command tidies up completed records."""
    deps = _FakeDeps.with_in_flight(None)
    _detect_or_compensate_oplog(deps, allow_mutation=False)
    assert deps.oplog.calls == ["read_in_flight"]
    assert deps.service.compensate_rename_args == []
    assert deps.oplog.mark_completed_args == []


def test_no_in_flight_mutating_returns_silently() -> None:
    deps = _FakeDeps.with_in_flight(None)
    _detect_or_compensate_oplog(deps, allow_mutation=True)
    assert deps.oplog.calls == ["vacuum_completed", "read_in_flight"]
    assert deps.service.compensate_rename_args == []


def test_read_only_path_does_not_vacuum() -> None:
    """Vacuum is mutating-only — `switcher list` / `status` / `which` /
    `tools` must not write to disk even when there are completed records
    to drop. The next mutating command picks up the cleanup."""
    deps = _FakeDeps.with_in_flight(None)
    _detect_or_compensate_oplog(deps, allow_mutation=False)
    assert "vacuum_completed" not in deps.oplog.calls


def test_mutating_path_vacuums_before_read_in_flight() -> None:
    """On mutating paths, vacuum must happen before any decision is made
    on read_in_flight, so a previous run's completed record can't
    masquerade as in-flight via a stale snapshot."""
    deps = _FakeDeps.with_in_flight(None)
    _detect_or_compensate_oplog(deps, allow_mutation=True)
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


def test_format_in_progress_hint_init_names_recovery_flags() -> None:
    """v0.1.5 PR4: init's --continue / --abort flags ship in this PR, so
    the recovery hint names them directly. The string must point the
    user at both options (continue replays mappings, abort reverses
    pre-init state — the user picks based on whether they want the
    init to succeed or be undone)."""
    text = _format_in_progress_hint(_init_record())
    assert "init" in text
    assert "2026-05-12-current" in text
    assert "claude" in text
    # Names BOTH flags — the user needs the choice surfaced, not just
    # one option.
    assert "switcher init --continue" in text
    assert "switcher init --abort" in text


def test_format_in_progress_hint_rescan_fresh_mentions_fresh_profile() -> None:
    """v0.1.5 PR5: hint still labels the mode + lists target tools.
    Recovery-flag wording is asserted by the dedicated
    test_format_in_progress_hint_rescan_fresh_names_recovery_flags test."""
    text = _format_in_progress_hint(_rescan_record_fresh())
    assert "rescan" in text
    assert "claude" in text
    assert "fresh-profile" in text


def test_format_in_progress_hint_rescan_into_mentions_into_target() -> None:
    text = _format_in_progress_hint(_rescan_record_into())
    assert "rescan" in text
    assert "shared" in text
    assert "claude" in text


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
    # v0.1.5 PR4: init hint now names the recovery flags directly.
    assert "switcher init --continue" in result.output


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


# -- v0.1.5 PR4: init --continue / --abort flag dispatch ------------------
#
# Cover the new init CLI recovery surface (spec §2.2):
# - Mutex matrix: --continue ⊥ --abort, and both ⊥ --only/--skip/--interactive.
# - Dispatch ordering: the recovery branch MUST run BEFORE the generic
#   _detect_or_compensate_oplog hook fires. Otherwise the hook would raise
#   InitInProgressError on the in-flight _InitOp and the recovery flags
#   would never reach service.init().
# - Return-shape handling: InitAlreadyCompletedReport(kind="continue") vs
#   kind="abort"; the abort kind must point the user at `switcher uninstall`
#   (the committed init is still on disk; abort can't reverse it). None
#   return = compensation ran; print success.


def _combined(result: object) -> str:
    """stdout + stderr concatenation for substring assertions.

    Click 8.3 separates the two streams by default; BadParameter errors
    land on stderr while service-layer SwitcherError messages land on
    stderr through handle_errors. Tests that don't care which stream a
    message hits use this helper — mirrors test_cli_init_flags._combined.
    """
    stdout = getattr(result, "stdout", "") or ""
    stderr = getattr(result, "stderr", "") or ""
    return stdout + stderr


@pytest.fixture
def patched_deps(monkeypatch: pytest.MonkeyPatch) -> _FakeDeps:
    """Patch ``cli.get_deps`` to return a fake Deps with NO in-flight
    record by default; tests that need an in-flight _InitOp set
    ``fake.oplog.in_flight`` themselves.
    """
    from switcher import cli as cli_module

    fake = _FakeDeps.with_in_flight(None)
    monkeypatch.setattr(cli_module, "get_deps", lambda: fake)
    return fake


# Mutex matrix --------------------------------------------------------------


def test_init_continue_and_abort_mutex(patched_deps: _FakeDeps) -> None:
    """`--continue --abort` raises typer.BadParameter (exit 2). The mutex
    check fires BEFORE get_deps reads any state, so no journal calls
    happen on a usage error."""
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--continue", "--abort"])
    assert result.exit_code != 0
    assert "mutually exclusive" in _combined(result).lower()
    assert patched_deps.service.init_calls == []


@pytest.mark.parametrize(
    "filter_args",
    [
        ["--only", "claude"],
        ["--skip", "claude"],
        ["--interactive"],
    ],
)
def test_init_continue_with_filter_flag_rejected(
    patched_deps: _FakeDeps, filter_args: list[str]
) -> None:
    """Recovery scope is taken from the in-flight journal record, NOT
    the call site. Combining --continue with --only/--skip/--interactive
    is a usage error — service.init() also refuses these combinations,
    but the CLI's BadParameter gives a typer-formatted error before
    reaching the service (cleaner UX than a SwitcherError traceback)."""
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--continue", *filter_args])
    assert result.exit_code != 0
    assert "mutually exclusive" in _combined(result).lower()
    assert patched_deps.service.init_calls == []


@pytest.mark.parametrize(
    "filter_args",
    [
        ["--only", "claude"],
        ["--skip", "claude"],
        ["--interactive"],
    ],
)
def test_init_abort_with_filter_flag_rejected(
    patched_deps: _FakeDeps, filter_args: list[str]
) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--abort", *filter_args])
    assert result.exit_code != 0
    assert "mutually exclusive" in _combined(result).lower()
    assert patched_deps.service.init_calls == []


# Dispatch ordering ---------------------------------------------------------


def test_init_continue_vacuum_does_not_affect_in_flight_dispatch(
    patched_deps: _FakeDeps,
) -> None:
    """abby pass-9 concern push-back: ``vacuum_completed`` before
    ``service.init`` cannot drop an in-flight record, because vacuum
    only acts on records where ``completed_at is not None`` (see
    OpLogIO.vacuum_completed). ``read_in_flight`` filters on
    ``completed_at is None`` independently — so the
    InitAlreadyCompletedReport path (which fires when read_in_flight
    returns a non-None record AND on-disk invariants match completed
    state) is unaffected by vacuum timing.

    Lock this contract: with a fake oplog that returns an _InitOp from
    read_in_flight regardless of vacuum calls, the recovery dispatch
    must still see the in-flight record and call service.init."""
    patched_deps.oplog.in_flight = _init_record()
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--continue"])
    assert result.exit_code == 0, _combined(result)
    # vacuum_completed ran first (journal hygiene); read_in_flight
    # observed the in-flight record AFTER vacuum.
    assert patched_deps.oplog.calls == ["vacuum_completed", "read_in_flight"]
    # Dispatch reached service.init with the recovery flag, not
    # short-circuited via NoInProgressInitError.
    assert len(patched_deps.service.init_calls) == 1


def test_init_continue_dispatches_to_service_with_in_flight(
    patched_deps: _FakeDeps,
) -> None:
    """`switcher init --continue` with an in-flight _InitOp must bypass
    the generic _detect_or_compensate_oplog hook (which would otherwise
    raise InitInProgressError) and dispatch to service.init(continue_=True).
    Journal hygiene (vacuum_completed + a read_in_flight probe for the
    rename-drain check) still runs."""
    patched_deps.oplog.in_flight = _init_record()
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--continue"])
    assert result.exit_code == 0, _combined(result)
    # vacuum_completed (journal hygiene) + read_in_flight (rename-drain
    # probe; harmless when the in-flight op is an _InitOp).
    assert patched_deps.oplog.calls == ["vacuum_completed", "read_in_flight"]
    assert patched_deps.service.init_calls == [
        {"target_ids": None, "continue_": True, "abort": False}
    ]
    # Rename auto-compensation MUST NOT fire when the in-flight op is
    # an init — only an _InitOp is in flight here.
    assert patched_deps.service.compensate_rename_args == []


def test_init_abort_dispatches_to_service_with_in_flight(
    patched_deps: _FakeDeps,
) -> None:
    patched_deps.oplog.in_flight = _init_record()
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--abort"])
    assert result.exit_code == 0, _combined(result)
    assert patched_deps.oplog.calls == ["vacuum_completed", "read_in_flight"]
    assert patched_deps.service.init_calls == [
        {"target_ids": None, "continue_": False, "abort": True}
    ]
    assert patched_deps.service.compensate_rename_args == []


# Rename auto-compensation must STILL fire on the recovery path -----------


def test_init_continue_drains_in_flight_rename_before_dispatch(
    patched_deps: _FakeDeps,
) -> None:
    """A stale in-flight _RenameOp (left from a crash on a prior rename)
    must be transparently auto-compensated BEFORE init recovery dispatches.
    Without this, `switcher init --continue/--abort` would hit the
    service's "not an _InitOp" guard and raise OpLogCorruptError on a
    state every OTHER CLI command silently fixes (spec §2.3 contract:
    rename is auto-compensated transparently on every other command).

    Both reviewers (CR + abby) flagged this as a blocking regression in
    the initial recovery wiring; the fix re-runs rename's idempotent
    disk-truth roll-forward via service._compensate_rename, mirroring
    the read-only/mutating branch of _detect_or_compensate_oplog.
    """
    rename = _rename_record()
    patched_deps.oplog.in_flight = rename
    # Simulate the real OpLog's post-drain state: after rename is
    # marked completed by service._compensate_rename, the next
    # read_in_flight returns None, and service.init(continue_=True)
    # raises NoInProgressInitError. The fake mimics that by clearing
    # in_flight inside the compensate hook and arming init_side_effect.
    real_compensate = patched_deps.service._compensate_rename

    def drain_and_clear(record: _RenameOp) -> None:
        real_compensate(record)
        patched_deps.oplog.in_flight = None

    patched_deps.service._compensate_rename = drain_and_clear  # type: ignore[method-assign]
    patched_deps.service.init_side_effect = NoInProgressInitError(
        "no interrupted init detected; nothing to continue/abort"
    )

    runner = CliRunner()
    result = runner.invoke(app, ["init", "--continue"])
    # The contract: rename drained FIRST, then init dispatched.
    assert patched_deps.service.compensate_rename_args == [rename]
    assert len(patched_deps.service.init_calls) == 1
    # And the surfaced error is NoInProgressInitError (clean signal),
    # NOT OpLogCorruptError (scary corruption) — that was abby's
    # blocking concern with the original wiring.
    out = _combined(result).lower()
    assert "no interrupted init" in out
    assert "corrupt" not in out
    assert result.exit_code == 1


def test_init_abort_drains_in_flight_rename_before_dispatch(
    patched_deps: _FakeDeps,
) -> None:
    """Same contract for --abort. Rename auto-compensation is symmetric
    across continue and abort because the rename drain is independent
    of the init's recovery direction."""
    rename = _rename_record()
    patched_deps.oplog.in_flight = rename
    real_compensate = patched_deps.service._compensate_rename

    def drain_and_clear(record: _RenameOp) -> None:
        real_compensate(record)
        patched_deps.oplog.in_flight = None

    patched_deps.service._compensate_rename = drain_and_clear  # type: ignore[method-assign]
    patched_deps.service.init_side_effect = NoInProgressInitError(
        "no interrupted init detected; nothing to continue/abort"
    )

    runner = CliRunner()
    result = runner.invoke(app, ["init", "--abort"])
    assert patched_deps.service.compensate_rename_args == [rename]
    assert len(patched_deps.service.init_calls) == 1
    out = _combined(result).lower()
    assert "no interrupted init" in out
    assert "corrupt" not in out
    assert result.exit_code == 1


def test_init_continue_refuses_when_rename_drain_did_not_clear_journal(
    patched_deps: _FakeDeps,
) -> None:
    """Defensive refresh after the rename drain: if the journal still
    holds a _RenameOp after ``_compensate_rename`` returns, the service
    method's contract (idempotent disk-truth roll-forward, including
    mark_completed) has regressed. The CLI must NOT fall through to
    ``service.init(...)`` — that would surface as OpLogCorruptError
    from the service's "not an _InitOp" guard, masking the real
    contract breakage. Raise the corruption signal HERE so the broken
    contract surfaces at the CLI layer with a clear hint about the
    rename-drain regression. abby pass-2 blocker.
    """
    rename = _rename_record()
    patched_deps.oplog.in_flight = rename
    # _compensate_rename is a no-op in this fake — simulates the
    # regression abby is defending against: the method returns without
    # touching the journal, so read_in_flight still surfaces the rename.
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--continue"])
    assert patched_deps.service.compensate_rename_args == [rename]
    # service.init MUST NOT be called: the contract breakage surfaces
    # before dispatch.
    assert patched_deps.service.init_calls == []
    out = _combined(result).lower()
    # The surfaced error names the rename-drain regression specifically
    # (not the generic "unexpected in-flight op type" the service would
    # produce). Asserting on the rename-specific wording catches a
    # future change that downgrades back to the generic guard.
    assert "rename" in out
    assert "contract" in out or "did not drain" in out
    # Exit 1 (handle_errors-routed SwitcherError), not 2 (typer
    # BadParameter) or 3 (read-only hint exit).
    assert result.exit_code == 1


# Return-shape handling -----------------------------------------------------


def test_init_continue_already_completed_prints_journal_hint(
    patched_deps: _FakeDeps,
) -> None:
    """InitAlreadyCompletedReport(kind='continue') signals the journal
    record was committed before the crash — mark_completed ran without
    invoking compensation. CLI tells the user the init is already
    complete; no uninstall hint (init succeeded, nothing to reverse)."""
    patched_deps.oplog.in_flight = _init_record()
    patched_deps.service.init_return_value = InitAlreadyCompletedReport(
        profile_name="2026-05-12-current", kind="continue"
    )
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--continue"])
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "2026-05-12-current" in out
    assert "already" in out.lower()
    # continue-kind message MUST NOT mention uninstall — that's only
    # relevant for the abort kind, where the user expected reversal.
    assert "switcher uninstall" not in out


def test_init_abort_already_completed_points_at_uninstall(
    patched_deps: _FakeDeps,
) -> None:
    """InitAlreadyCompletedReport(kind='abort') signals the user tried
    to abort an init that had already committed. The CLI MUST tell
    them: (a) the init is on disk, (b) abort can't reverse it,
    (c) use `switcher uninstall` if they actually want reversal.
    Without this, abort silently no-ops and the user thinks they
    reversed an init that's still in place (spec §2.2 distinction)."""
    patched_deps.oplog.in_flight = _init_record()
    patched_deps.service.init_return_value = InitAlreadyCompletedReport(
        profile_name="2026-05-12-current", kind="abort"
    )
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--abort"])
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "2026-05-12-current" in out
    assert "switcher uninstall" in out


def test_init_continue_compensation_ran_prints_success(
    patched_deps: _FakeDeps,
) -> None:
    """service.init(continue_=True) returning None means compensation
    actually ran (replayed mappings + step 6-7). CLI prints a
    non-empty success indicator and exits 0."""
    patched_deps.oplog.in_flight = _init_record()
    patched_deps.service.init_return_value = None
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--continue"])
    assert result.exit_code == 0, _combined(result)
    assert _combined(result).strip() != ""


def test_init_abort_compensation_ran_prints_success(
    patched_deps: _FakeDeps,
) -> None:
    patched_deps.oplog.in_flight = _init_record()
    patched_deps.service.init_return_value = None
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--abort"])
    assert result.exit_code == 0, _combined(result)
    assert _combined(result).strip() != ""


# -- v0.1.5 PR5: rescan --continue / --abort flag dispatch ----------------
#
# Mirrors the init recovery surface, with rescan-specific differences:
# - Rescan has FOUR flags that conflict with --continue/--abort:
#   --only, --into, --all, --dry-run.
# - The recovery hint now NAMES the rescan flags (PR5 ships them).


def test_rescan_continue_and_abort_mutex(patched_deps: _FakeDeps) -> None:
    """`rescan --continue --abort` raises typer.BadParameter (exit 2)."""
    runner = CliRunner()
    result = runner.invoke(app, ["rescan", "--continue", "--abort"])
    assert result.exit_code != 0
    assert "mutually exclusive" in _combined(result).lower()
    assert patched_deps.service.rescan_calls == []


@pytest.mark.parametrize(
    "filter_args",
    [
        ["--only", "claude"],
        ["--into", "shared"],
        ["--all"],
        ["--dry-run"],
    ],
)
def test_rescan_continue_with_filter_flag_rejected(
    patched_deps: _FakeDeps, filter_args: list[str]
) -> None:
    """Recovery scope is taken from the in-flight journal record, NOT the
    call site. --continue is mutually exclusive with each of the rescan
    filter flags (--only / --into / --all / --dry-run)."""
    runner = CliRunner()
    result = runner.invoke(app, ["rescan", "--continue", *filter_args])
    assert result.exit_code != 0
    assert "mutually exclusive" in _combined(result).lower()
    assert patched_deps.service.rescan_calls == []


@pytest.mark.parametrize(
    "filter_args",
    [
        ["--only", "claude"],
        ["--into", "shared"],
        ["--all"],
        ["--dry-run"],
    ],
)
def test_rescan_abort_with_filter_flag_rejected(
    patched_deps: _FakeDeps, filter_args: list[str]
) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["rescan", "--abort", *filter_args])
    assert result.exit_code != 0
    assert "mutually exclusive" in _combined(result).lower()
    assert patched_deps.service.rescan_calls == []


def test_rescan_continue_dispatches_to_service_with_in_flight(
    patched_deps: _FakeDeps,
) -> None:
    """`rescan --continue` with an in-flight _RescanOp bypasses the
    generic _detect_or_compensate_oplog hook and dispatches to
    service.rescan(continue_=True)."""
    patched_deps.oplog.in_flight = _rescan_record_fresh()
    runner = CliRunner()
    result = runner.invoke(app, ["rescan", "--continue"])
    assert result.exit_code == 0, _combined(result)
    # vacuum (journal hygiene) + read_in_flight (rename-drain probe).
    assert patched_deps.oplog.calls == ["vacuum_completed", "read_in_flight"]
    assert patched_deps.service.rescan_calls == [{"continue_": True, "abort": False}]
    assert patched_deps.service.compensate_rename_args == []


def test_rescan_abort_dispatches_to_service_with_in_flight(
    patched_deps: _FakeDeps,
) -> None:
    patched_deps.oplog.in_flight = _rescan_record_fresh()
    runner = CliRunner()
    result = runner.invoke(app, ["rescan", "--abort"])
    assert result.exit_code == 0, _combined(result)
    assert patched_deps.oplog.calls == ["vacuum_completed", "read_in_flight"]
    assert patched_deps.service.rescan_calls == [{"continue_": False, "abort": True}]


def test_rescan_continue_drains_in_flight_rename_before_dispatch(
    patched_deps: _FakeDeps,
) -> None:
    """A stale in-flight _RenameOp must be transparently auto-
    compensated BEFORE rescan recovery dispatches. Same contract init's
    --continue / --abort uses."""
    rename = _rename_record()
    patched_deps.oplog.in_flight = rename
    real_compensate = patched_deps.service._compensate_rename

    def drain_and_clear(record: _RenameOp) -> None:
        real_compensate(record)
        patched_deps.oplog.in_flight = None

    patched_deps.service._compensate_rename = drain_and_clear  # type: ignore[method-assign]
    from switcher.errors import NoInProgressRescanError

    patched_deps.service.rescan_side_effect = NoInProgressRescanError(
        "no interrupted rescan detected; nothing to continue/abort"
    )

    runner = CliRunner()
    result = runner.invoke(app, ["rescan", "--continue"])
    assert patched_deps.service.compensate_rename_args == [rename]
    assert len(patched_deps.service.rescan_calls) == 1
    out = _combined(result).lower()
    assert "no interrupted rescan" in out
    assert "corrupt" not in out
    assert result.exit_code == 1


def test_rescan_continue_refuses_when_rename_drain_did_not_clear_journal(
    patched_deps: _FakeDeps,
) -> None:
    """Defensive refresh: if the journal still holds a _RenameOp after
    ``_compensate_rename`` returns, surface the contract breakage at
    the CLI layer instead of falling through to service.rescan (which
    would mask it as a generic OpLogCorruptError)."""
    rename = _rename_record()
    patched_deps.oplog.in_flight = rename
    # _compensate_rename is a no-op in the default fake — simulates the
    # contract regression.
    runner = CliRunner()
    result = runner.invoke(app, ["rescan", "--continue"])
    assert patched_deps.service.compensate_rename_args == [rename]
    assert patched_deps.service.rescan_calls == []
    out = _combined(result).lower()
    assert "rename" in out
    assert "contract" in out or "did not drain" in out
    assert result.exit_code == 1


def test_rescan_continue_already_completed_prints_journal_hint(
    patched_deps: _FakeDeps,
) -> None:
    """RescanAlreadyCompletedReport(kind='continue') tells the user the
    rescan was already on disk; journal cleaned up; no further action."""
    patched_deps.oplog.in_flight = _rescan_record_fresh()
    patched_deps.service.rescan_return_value = RescanAlreadyCompletedReport(
        target_profiles={"claude": "2026-05-12-rescan-1"}, kind="continue"
    )
    runner = CliRunner()
    result = runner.invoke(app, ["rescan", "--continue"])
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "2026-05-12-rescan-1" in out
    assert "already" in out.lower()


def test_rescan_abort_already_completed_signals_committed(
    patched_deps: _FakeDeps,
) -> None:
    """RescanAlreadyCompletedReport(kind='abort') tells the user the
    rescan was already committed before the crash; abort can't reverse
    a committed rescan (the user would need to unmanage / re-init).
    The CLI MUST tell them this — silent no-op would let them think
    abort actually reversed the rescan."""
    patched_deps.oplog.in_flight = _rescan_record_fresh()
    patched_deps.service.rescan_return_value = RescanAlreadyCompletedReport(
        target_profiles={"claude": "2026-05-12-rescan-1"}, kind="abort"
    )
    runner = CliRunner()
    result = runner.invoke(app, ["rescan", "--abort"])
    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "2026-05-12-rescan-1" in out
    assert "already" in out.lower()
    # CR pass-PR minor: verify abort-specific wording, not just
    # generic "already". Without this, continue and abort could
    # regress to identical messaging.
    out_lower = out.lower()
    assert "committed" in out_lower or "cannot" in out_lower


def test_rescan_continue_compensation_ran_prints_success(
    patched_deps: _FakeDeps,
) -> None:
    """service.rescan(continue_=True) returning None means compensation
    actually ran. CLI prints a non-empty success indicator and exits 0."""
    patched_deps.oplog.in_flight = _rescan_record_fresh()
    patched_deps.service.rescan_return_value = None
    runner = CliRunner()
    result = runner.invoke(app, ["rescan", "--continue"])
    assert result.exit_code == 0, _combined(result)
    assert _combined(result).strip() != ""


def test_rescan_abort_compensation_ran_prints_success(
    patched_deps: _FakeDeps,
) -> None:
    patched_deps.oplog.in_flight = _rescan_record_fresh()
    patched_deps.service.rescan_return_value = None
    runner = CliRunner()
    result = runner.invoke(app, ["rescan", "--abort"])
    assert result.exit_code == 0, _combined(result)
    assert _combined(result).strip() != ""


# -- v0.1.5 PR5: rescan hint NOW names the recovery flags ----------------


def test_format_in_progress_hint_rescan_fresh_names_recovery_flags() -> None:
    """v0.1.5 PR5 supersedes the PR4 ``test_format_in_progress_hint_
    rescan_does_not_reference_nonexistent_flags`` carry-forward: the
    rescan --continue / --abort flags ship in this PR, so the hint
    must now NAME them (symmetric to init's hint)."""
    text = _format_in_progress_hint(_rescan_record_fresh())
    assert "rescan" in text
    assert "claude" in text
    assert "fresh-profile" in text
    assert "switcher rescan --continue" in text
    assert "switcher rescan --abort" in text


def test_format_in_progress_hint_rescan_into_names_recovery_flags() -> None:
    text = _format_in_progress_hint(_rescan_record_into())
    assert "rescan" in text
    assert "shared" in text
    assert "switcher rescan --continue" in text
    assert "switcher rescan --abort" in text
