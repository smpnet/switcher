# pyright: reportPrivateUsage=none
"""Tests for rename op-log compensation (spec §2.3)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from switcher.errors import OpLogCorruptError, PathNotADirectoryError
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
    record = _make_record(name, "client-A", affected=[])
    service._compensate_rename(record)
    store = FileProfileStore(tmp_state)
    assert not store.profile_dir(name).exists()
    assert store.profile_dir("client-A").exists()


def test_compensate_rename_both_dirs_exist_raises_corrupt(
    service: ProfileService, tmp_state: Path
) -> None:
    """Both `from` and `to` profile dirs exist — could be a partial
    FileProfileStore.rename or external interference. Refuse loudly."""
    name = service.init().profile_name
    service.create("client-A")
    record = _make_record(name, "client-A", affected=[])
    with pytest.raises(OpLogCorruptError):
        service._compensate_rename(record)
    store = FileProfileStore(tmp_state)
    assert store.profile_dir(name).exists()
    assert store.profile_dir("client-A").exists()


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
    record = _make_record(name, "client-A", affected=["claude"])
    service._compensate_rename(record)
    store = FileProfileStore(tmp_state)
    active = store.get_active()
    assert active.get("claude") == "client-A"
    claude_live = tmp_home / ".claude"
    expected = (store.profile_dir("client-A") / "claude").resolve()
    assert claude_live.resolve() == expected


def test_compensate_rename_both_dirs_missing_raises_corrupt(
    service: ProfileService,
) -> None:
    """Neither `from` nor `to` exists. Refuse loudly — user manually
    intervened or something else went wrong."""
    record = _make_record("a", "b", affected=[])
    with pytest.raises(OpLogCorruptError):
        service._compensate_rename(record)


def test_compensate_rename_idempotent_when_already_complete(
    service: ProfileService, tmp_state: Path
) -> None:
    """Both store.rename and set_active ran cleanly. Compensation runs
    the swap_link loop (idempotent on already-correct links) and exits."""
    name = service.init().profile_name
    service.rename(name, "client-A")
    record = _make_record(name, "client-A", affected=["claude"])
    service._compensate_rename(record)
    store = FileProfileStore(tmp_state)
    assert store.profile_dir("client-A").exists()
    assert store.get_active().get("claude") == "client-A"


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
    record = _make_record(name, "client-A", affected=["unknown_tool"])
    service._compensate_rename(record)
    store = FileProfileStore(tmp_state)
    assert store.get_active().get("unknown_tool") == "client-A"


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
    # User intervened: the live symlink was unlinked and replaced with a
    # real directory between the crash and our compensation pass.
    claude_live = tmp_home / ".claude"
    if claude_live.is_symlink():
        claude_live.unlink()
    elif claude_live.exists():
        # Original conftest fixture seeded a real dir; remove the link/dir
        # however it lands so we can pre-stage the bad shape.
        if claude_live.is_dir():
            import shutil

            shutil.rmtree(claude_live)
        else:
            claude_live.unlink()
    claude_live.mkdir()
    record = _make_record(name, "client-A", affected=["claude"])
    with pytest.raises(PathNotADirectoryError):
        service._compensate_rename(record)
    # Canonical state untouched: store still at `client-A` (where step-1 left
    # it), active still at `name` (the pre-step-2 shape we staged), live still
    # the bad real dir we created.
    after = FileProfileStore(tmp_state)
    assert after.profile_dir("client-A").exists()
    assert after.get_active().get("claude") == name
    assert claude_live.is_dir() and not claude_live.is_symlink()


def test_rename_writes_intent_and_marks_completed(service: ProfileService, tmp_state: Path) -> None:
    """A successful rename appends an intent record and marks it
    completed; vacuum then drops it. End-to-end covers the writer
    side of the op-log integration."""
    name = service.init().profile_name
    service.rename(name, "client-A")
    oplog = OpLogIO(tmp_state)
    records = oplog.read_records()
    assert len(records) == 1
    assert records[0].op == "rename"
    assert records[0].completed_at is not None
    oplog.vacuum_completed()
    assert oplog.read_records() == []
