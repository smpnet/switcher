# pyright: reportPrivateUsage=none
"""End-to-end op-log recovery (spec §8.2).

Drives the recovery surface across a process boundary: the test stages
a partial on-disk state + an in-flight ``_RescanOp`` / ``_InitOp`` /
``_RenameOp`` journal record, then spawns ``python -m switcher`` to
exercise the recovery flag dispatch (init / rescan) or the
auto-compensation hook (rename) from a fresh process. Confirms:

  - the journal survives across processes (the partial state on disk
    is what's there when the recovery process starts up),
  - the CLI flag dispatch wires up to the service compensation paths
    correctly (typer arg parsing → recovery branch → service.* →
    mark_completed),
  - the rename auto-compensation runs on the FIRST CLI command after
    the crash, even read-only ones (`switcher status`),
  - the journal is cleared (vacuum_completed drops the marked record)
    by the time the recovery process exits.

These tests deliberately avoid SIGKILL-of-a-mid-mutation-subprocess.
The plan flagged that approach, but timing the kill between
``move_or_seed_dir`` and ``swap_link`` is non-deterministic and
platform-flaky. Force-injecting the post-crash state via direct
journal writes + manual disk staging hits the same recovery code
path with deterministic test setup.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from switcher.oplog import OpLogIO, _InitOp, _RenameOp, _RescanOp
from switcher.paths import IS_WINDOWS
from switcher.registry import build_registry
from switcher.service import ProfileService
from switcher.store import FileProfileStore

pytestmark = pytest.mark.integration


def _now() -> datetime:
    return datetime(2026, 5, 12, 10, 30, tzinfo=UTC)


def _is_link(p: Path) -> bool:
    return p.is_symlink() or (IS_WINDOWS and os.path.isjunction(p))


def _symlink_dir(target: Path, live: Path) -> None:
    live.symlink_to(target, target_is_directory=IS_WINDOWS)


def _run(args: list[str], home: Path, state: Path) -> subprocess.CompletedProcess[str]:
    """Boot ``python -m switcher <args>`` against an isolated env.

    Mirrors tests/e2e/test_cli.py's helper — re-sets HOME / state-dir
    so the subprocess sees the same isolation the in-process tests get
    from monkeypatch."""
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    if IS_WINDOWS:
        env["USERPROFILE"] = str(home)
        env["LOCALAPPDATA"] = str(home / "AppData" / "Local")
        env["APPDATA"] = str(home / "AppData" / "Roaming")
        env.pop("HOME", None)
        env.pop("HOMEDRIVE", None)
        env.pop("HOMEPATH", None)
        env.pop("XDG_CONFIG_HOME", None)
    else:
        env["HOME"] = str(home)
        env.pop("XDG_CONFIG_HOME", None)
    env["SWITCHER_STATE_DIR"] = str(state)
    return subprocess.run(
        [sys.executable, "-m", "switcher", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
        timeout=30,
        cwd=str(home),
    )


def _flatten(text: str) -> str:
    """Collapse Rich-style line wraps so substring assertions can
    survive ``switcher init --abort`` getting split across two lines
    when stderr is wrapped at the console width."""
    return re.sub(r"\s+", " ", text)


def _service(tmp_state: Path, tmp_home: Path) -> ProfileService:
    return ProfileService(
        FileProfileStore(tmp_state),
        # Use the live PathResolver shape integration tests use elsewhere.
        __import__("switcher.paths", fromlist=["PathResolver"]).PathResolver(home=tmp_home),
        build_registry(tmp_state / "registry.d"),
    )


# -- init recovery ----------------------------------------------------------


def test_interrupted_init_continue_via_subprocess(tmp_home: Path, tmp_state: Path) -> None:
    """Stage the post-move-pre-swap_link crash window for one mapping
    + the journal record, then run ``switcher init --continue`` in a
    fresh process. Confirms cross-process recovery: the journal is
    read from disk by the new process, the service compensation runs,
    and final state matches what a clean init produces."""
    store = FileProfileStore(tmp_state)
    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)

    # Stage the post-mid-window state: profile dir created + target
    # populated (move_or_seed_dir ran) but live missing (swap_link
    # never ran). Vanilla NOT yet created (init step 6 didn't reach).
    profile_name = "2026-05-12-current"
    store.create(profile_name, {"claude": True})
    target = store.profile_dir(profile_name) / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"original": true}')

    # Inject the in-flight journal record.
    record = _InitOp.model_validate(
        {
            "op": "init",
            "started_at": _now(),
            "target_ids": ["claude"],
            "profile_name": profile_name,
            "mappings": [
                {
                    "tool_id": "claude",
                    "mapping_index": 0,
                    "live_path": str(claude_live),
                    "profile_subdir": "claude",
                    "original_kind": "real-dir",
                }
            ],
        }
    )
    OpLogIO(tmp_state).append_record(record)

    # Run `switcher init --continue` in a fresh process.
    r = _run(["init", "--continue"], tmp_home, tmp_state)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"

    # Final state: live is a managed link to target, vanilla exists,
    # active map points at the dated-current profile, journal cleared.
    assert _is_link(claude_live), f"live={claude_live} not a managed link"
    assert (target / "settings.json").read_text() == '{"original": true}'
    assert store.profile_dir("vanilla").is_dir()
    assert store.get_active().get("claude") == profile_name
    assert OpLogIO(tmp_state).read_in_flight() is None


def test_interrupted_init_abort_via_subprocess(tmp_home: Path, tmp_state: Path) -> None:
    """Symmetric to the continue case: same staged partial state, but
    drive the abort path. Verifies live restoration + profile-dir
    deletion + journal clear via a fresh process."""
    store = FileProfileStore(tmp_state)
    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)

    profile_name = "2026-05-12-current"
    store.create(profile_name, {"claude": True})
    target = store.profile_dir(profile_name) / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"original": true}')

    record = _InitOp.model_validate(
        {
            "op": "init",
            "started_at": _now(),
            "target_ids": ["claude"],
            "profile_name": profile_name,
            "mappings": [
                {
                    "tool_id": "claude",
                    "mapping_index": 0,
                    "live_path": str(claude_live),
                    "profile_subdir": "claude",
                    "original_kind": "real-dir",
                }
            ],
        }
    )
    OpLogIO(tmp_state).append_record(record)

    r = _run(["init", "--abort"], tmp_home, tmp_state)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"

    # Live restored, profile dirs gone, journal cleared.
    assert claude_live.is_dir() and not _is_link(claude_live)
    assert (claude_live / "settings.json").read_text() == '{"original": true}'
    assert not store.profile_dir(profile_name).exists()
    assert OpLogIO(tmp_state).read_in_flight() is None


def test_status_surfaces_interrupted_init_with_exit_3(tmp_home: Path, tmp_state: Path) -> None:
    """Read-only commands surface an in-flight init as exit 3 + hint
    text on stderr. The hint must name both recovery flags so the
    user knows their next step."""
    # Initialize properly first so config.json is valid (status's
    # read-only path doesn't error on missing config).
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})
    store.set_active_state({}, {})

    record = _InitOp.model_validate(
        {
            "op": "init",
            "started_at": _now(),
            "target_ids": ["claude"],
            "profile_name": "2026-05-12-current",
            "mappings": [],
        }
    )
    OpLogIO(tmp_state).append_record(record)

    r = _run(["status"], tmp_home, tmp_state)
    assert r.returncode == 3, f"expected exit 3, got {r.returncode}; stderr={r.stderr!r}"
    combined = _flatten(r.stdout + r.stderr)
    assert "Interrupted `switcher init`" in combined
    assert "switcher init --continue" in combined
    assert "switcher init --abort" in combined
    # Read-only hook MUST NOT mutate state — journal stays in flight.
    assert OpLogIO(tmp_state).read_in_flight() is not None


# -- rescan recovery --------------------------------------------------------


def test_interrupted_rescan_continue_via_subprocess(tmp_home: Path, tmp_state: Path) -> None:
    """Stage post-mid-window state for rescan + journal, run
    ``switcher rescan --continue`` in a fresh process. Verifies the
    PR5 CLI dispatch path."""
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})
    store.set_active_state({}, {})  # initialized

    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)

    profile_name = "2026-05-12-rescan-1"
    store.create(profile_name, {"claude": True})
    target = store.profile_dir(profile_name) / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"captured": true}')
    # MOVE_DONE_LINK_MISSING: target populated, live missing.

    record = _RescanOp.model_validate(
        {
            "op": "rescan",
            "started_at": _now(),
            "target_ids": ["claude"],
            "target_profiles": {"claude": profile_name},
            "into_mode": False,
            "previous_tools": None,
            "mappings": [
                {
                    "tool_id": "claude",
                    "mapping_index": 0,
                    "live_path": str(claude_live),
                    "profile_subdir": "claude",
                    "original_kind": "real-dir",
                }
            ],
        }
    )
    OpLogIO(tmp_state).append_record(record)

    r = _run(["rescan", "--continue"], tmp_home, tmp_state)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"

    # Final state: live → managed link, active map updated, journal cleared.
    assert _is_link(claude_live)
    assert (target / "settings.json").read_text() == '{"captured": true}'
    assert store.get_active().get("claude") == profile_name
    assert OpLogIO(tmp_state).read_in_flight() is None


def test_interrupted_rescan_abort_via_subprocess(tmp_home: Path, tmp_state: Path) -> None:
    """Symmetric abort path. Live restored, fresh-profile target
    deleted, journal cleared."""
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})
    store.set_active_state({}, {})

    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)

    profile_name = "2026-05-12-rescan-1"
    store.create(profile_name, {"claude": True})
    target = store.profile_dir(profile_name) / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"captured": true}')
    _symlink_dir(target, claude_live)  # COMPLETE state for variety

    record = _RescanOp.model_validate(
        {
            "op": "rescan",
            "started_at": _now(),
            "target_ids": ["claude"],
            "target_profiles": {"claude": profile_name},
            "into_mode": False,
            "previous_tools": None,
            "mappings": [
                {
                    "tool_id": "claude",
                    "mapping_index": 0,
                    "live_path": str(claude_live),
                    "profile_subdir": "claude",
                    "original_kind": "real-dir",
                }
            ],
        }
    )
    OpLogIO(tmp_state).append_record(record)

    r = _run(["rescan", "--abort"], tmp_home, tmp_state)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"

    # Live restored, fresh-mode target deleted, journal cleared.
    assert claude_live.is_dir() and not _is_link(claude_live)
    assert (claude_live / "settings.json").read_text() == '{"captured": true}'
    assert not store.profile_dir(profile_name).exists()
    assert OpLogIO(tmp_state).read_in_flight() is None


def test_status_surfaces_interrupted_rescan_with_exit_3(tmp_home: Path, tmp_state: Path) -> None:
    """Read-only command surfaces in-flight rescan as exit 3 + hint
    naming the new flags."""
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})
    store.set_active_state({}, {})

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

    r = _run(["status"], tmp_home, tmp_state)
    assert r.returncode == 3, f"expected exit 3, got {r.returncode}"
    combined = _flatten(r.stdout + r.stderr)
    assert "Interrupted `switcher rescan`" in combined
    assert "switcher rescan --continue" in combined
    assert "switcher rescan --abort" in combined
    assert OpLogIO(tmp_state).read_in_flight() is not None


# -- rename auto-compensation ----------------------------------------------


def test_interrupted_rename_auto_compensates_on_next_status(
    tmp_home: Path, tmp_state: Path
) -> None:
    """Rename's auto-compensation contract: a stale ``_RenameOp`` in
    the journal is transparently rolled forward by the detection hook
    on the next CLI command — even a read-only one (``switcher status``).
    Spec §2.3 + the §2.2 hook contract.

    Stages the post-store.rename-pre-set_active state: profile dir
    has been renamed on disk, but the active map still references the
    OLD name. The next CLI command's hook detects this, runs
    ``service._compensate_rename`` (which sets active to the new
    name + re-runs swap_link to point live at the new dir), and marks
    the journal complete — all without user input."""
    store = FileProfileStore(tmp_state)
    claude_live = tmp_home / ".claude"

    # Full init through service so we have a real captured state to rename.
    service = _service(tmp_state, tmp_home)
    service.init()
    active_before = store.get_active()
    # conftest.tmp_home seeds both claude and copilot live paths, so
    # init captures both. affected_ids needs to cover every tool whose
    # active entry references the renaming profile — compensation
    # refuses on any orphan entry pointing at the rename endpoints.
    old_name = active_before["claude"]
    affected = sorted(tid for tid, prof in active_before.items() if prof == old_name)
    new_name = "renamed-profile"
    # _store.rename moves the dir AND would atomic-write the active
    # map. Drive the dir move manually to simulate the post-step-1
    # pre-step-2 crash window.
    old_dir = store.profile_dir(old_name)
    new_dir = store.profile_dir(new_name)
    old_dir.rename(new_dir)
    # Active map still references old_name (the bug window).
    assert store.get_active()["claude"] == old_name

    rename_record = _RenameOp.model_validate(
        {
            "op": "rename",
            "started_at": _now(),
            "from": old_name,
            "to": new_name,
            "affected_ids": affected,
        }
    )
    OpLogIO(tmp_state).append_record(rename_record)

    # Run a read-only command — `switcher status` triggers the hook,
    # which transparently rolls the rename forward.
    r = _run(["status"], tmp_home, tmp_state)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"

    # Active map now points at the new name; live still resolves
    # correctly (auto-compensation re-pointed the symlink).
    assert store.get_active()["claude"] == new_name
    assert _is_link(claude_live)
    assert OpLogIO(tmp_state).read_in_flight() is None


# -- mutating-command refusal ----------------------------------------------


def test_use_refuses_with_in_flight_init(tmp_home: Path, tmp_state: Path) -> None:
    """Mutating commands (here: ``switcher use``) refuse with exit 1
    + the same recovery hint when an in-flight init is detected. The
    error routes through handle_errors, so stderr carries the hint."""
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {"claude": True})
    store.set_active_state({"claude": "vanilla"}, {})

    record = _InitOp.model_validate(
        {
            "op": "init",
            "started_at": _now(),
            "target_ids": ["claude"],
            "profile_name": "2026-05-12-current",
            "mappings": [],
        }
    )
    OpLogIO(tmp_state).append_record(record)

    r = _run(["use", "vanilla"], tmp_home, tmp_state)
    assert r.returncode == 1, f"expected exit 1, got {r.returncode}"
    combined = r.stdout + r.stderr
    assert "Interrupted `switcher init`" in combined
    assert "switcher init --continue" in combined
    # Mutating refusal must not have changed state.
    assert OpLogIO(tmp_state).read_in_flight() is not None


# -- vacuum hygiene --------------------------------------------------------


def test_completed_record_vacuumed_on_next_mutating_command(
    tmp_home: Path, tmp_state: Path
) -> None:
    """A completed (mark_completed'd) record is dropped by vacuum at
    the top of the next mutating CLI command. Verifies the journal
    hygiene contract end-to-end: after a recovery completes
    successfully, subsequent commands work normally and the journal
    stays clean."""
    store = FileProfileStore(tmp_state)
    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)

    profile_name = "2026-05-12-current"
    store.create(profile_name, {"claude": True})
    target = store.profile_dir(profile_name) / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"data": true}')

    record = _InitOp.model_validate(
        {
            "op": "init",
            "started_at": _now(),
            "target_ids": ["claude"],
            "profile_name": profile_name,
            "mappings": [
                {
                    "tool_id": "claude",
                    "mapping_index": 0,
                    "live_path": str(claude_live),
                    "profile_subdir": "claude",
                    "original_kind": "real-dir",
                }
            ],
        }
    )
    OpLogIO(tmp_state).append_record(record)

    # Recovery: marks record completed, but vacuum hasn't run yet.
    r = _run(["init", "--continue"], tmp_home, tmp_state)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"

    # Pre-vacuum state: read_records returns the completed record
    # (mark_completed ran, but no subsequent vacuum has dropped it).
    pre_vacuum = OpLogIO(tmp_state).read_records()
    assert len(pre_vacuum) == 1
    assert pre_vacuum[0].completed_at is not None

    # First mutating command after recovery: vacuum runs at the top
    # of the hook, drops the completed record. Use ``rescan --dry-run``
    # — it runs through the allow_mutation=True hook (so vacuum fires)
    # but the dry_run flag prevents any actual capture. ``list`` is
    # read-only and intentionally doesn't vacuum (spec §2.2: vacuum
    # is journal hygiene gated on mutation).
    r2 = _run(["rescan", "--dry-run"], tmp_home, tmp_state)
    assert r2.returncode == 0, f"stdout={r2.stdout!r} stderr={r2.stderr!r}"
    oplog = OpLogIO(tmp_state)
    assert oplog.read_in_flight() is None
    assert oplog.read_records() == []


# -- corruption surfacing --------------------------------------------------


def test_corrupt_oplog_surfaces_with_exit_1(tmp_home: Path, tmp_state: Path) -> None:
    """A malformed oplog.json fails loud — the hook raises
    OpLogCorruptError, handle_errors prints to stderr, exit 1.
    Confirms the spec §2.1 "fail-loud not fail-silent" stance from
    the CLI surface."""
    store = FileProfileStore(tmp_state)
    store.create("vanilla", {})
    store.set_active_state({}, {})

    # Write a corrupt oplog.json. Any non-JSON or JSON that doesn't
    # match the discriminated-union shape triggers OpLogCorruptError
    # at read time.
    (tmp_state / "oplog.json").write_text("{not valid json")

    # Any command that runs the detection hook surfaces the error.
    r = _run(["status"], tmp_home, tmp_state)
    assert r.returncode == 1, f"expected exit 1, got {r.returncode}"
    combined = r.stdout + r.stderr
    assert "oplog" in combined.lower() or "corrupt" in combined.lower()


# -- crash-classification: committed-but-log-unmarked ---------------------


def test_continue_short_circuit_on_committed_init(tmp_home: Path, tmp_state: Path) -> None:
    """The spec §2.2 "committed but log-unmarked" path: crash between
    the final set_active_state and mark_completed leaves disk
    consistent. ``init --continue`` short-circuits with the
    already-completed report rather than re-running compensation."""
    store = FileProfileStore(tmp_state)
    claude_live = tmp_home / ".claude"
    shutil.rmtree(claude_live)

    # Build the post-set_active_state state via the service API so
    # everything is consistent (proper metadata + active + cache).
    profile_name = "2026-05-12-current"
    store.create(profile_name, {"claude": True})
    target = store.profile_dir(profile_name) / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text('{"data": true}')
    _symlink_dir(target, claude_live)
    store.create("vanilla", {"claude": True})
    store.set_active_state({"claude": profile_name}, {"claude": [str(claude_live)]})

    # Inject the in-flight init record AFTER the disk is committed —
    # simulates the post-set_active-pre-mark_completed crash.
    record = _InitOp.model_validate(
        {
            "op": "init",
            "started_at": _now(),
            "target_ids": ["claude"],
            "profile_name": profile_name,
            "mappings": [
                {
                    "tool_id": "claude",
                    "mapping_index": 0,
                    "live_path": str(claude_live),
                    "profile_subdir": "claude",
                    "original_kind": "real-dir",
                }
            ],
        }
    )
    OpLogIO(tmp_state).append_record(record)

    r = _run(["init", "--continue"], tmp_home, tmp_state)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    combined = r.stdout + r.stderr
    assert "already" in combined.lower()
    # Disk untouched, journal cleared.
    assert _is_link(claude_live)
    config = json.loads((tmp_state / "config.json").read_text())
    assert config["active"]["claude"] == profile_name
    assert OpLogIO(tmp_state).read_in_flight() is None
