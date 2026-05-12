# pyright: reportPrivateUsage=none
"""Tests for the per-mapping disk-state classifier (spec §2.1.1)."""

from __future__ import annotations

import sys
from pathlib import Path

from switcher.oplog import (
    MappingDiskState,
    _MappingIntent,
    classify_mapping,
)


def _make_intent(
    *,
    tool_id: str = "claude",
    mapping_index: int = 0,
    live_path: str | Path,
    profile_subdir: str = "claude",
    original_kind: str = "real-dir",
) -> _MappingIntent:
    return _MappingIntent(
        tool_id=tool_id,
        mapping_index=mapping_index,
        live_path=str(live_path),
        profile_subdir=profile_subdir,
        original_kind=original_kind,  # type: ignore[arg-type]
    )


def _make_link(src: Path, dst: Path) -> None:
    """Create a symlink at src pointing to dst.

    `target_is_directory=True` is a no-op on POSIX but required on
    Windows so the link is created as a directory symlink (not a file
    symlink) when CreateSymbolicLink falls back to that distinction.
    """
    src.symlink_to(dst, target_is_directory=sys.platform == "win32")


def test_complete_state_symlink_resolves_to_target(tmp_path: Path):
    profile_dir = tmp_path / "profiles" / "2026-05-12-current"
    target = profile_dir / "claude"
    target.mkdir(parents=True)
    live = tmp_path / ".claude"
    _make_link(live, target)
    intent = _make_intent(live_path=live)
    state = classify_mapping(intent, profile_dir)
    assert state is MappingDiskState.COMPLETE


def test_move_done_link_missing_state(tmp_path: Path):
    profile_dir = tmp_path / "profiles" / "2026-05-12-current"
    target = profile_dir / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text("{}")
    live = tmp_path / ".claude"  # does not exist
    intent = _make_intent(live_path=live)
    state = classify_mapping(intent, profile_dir)
    assert state is MappingDiskState.MOVE_DONE_LINK_MISSING


def test_untouched_state_original_kind_missing(tmp_path: Path):
    profile_dir = tmp_path / "profiles" / "2026-05-12-current"
    # target not created
    live = tmp_path / ".claude"  # does not exist
    intent = _make_intent(live_path=live, original_kind="missing")
    state = classify_mapping(intent, profile_dir)
    assert state is MappingDiskState.UNTOUCHED


def test_untouched_state_original_kind_real_dir(tmp_path: Path):
    profile_dir = tmp_path / "profiles" / "2026-05-12-current"
    live = tmp_path / ".claude"
    live.mkdir()
    intent = _make_intent(live_path=live, original_kind="real-dir")
    state = classify_mapping(intent, profile_dir)
    assert state is MappingDiskState.UNTOUCHED


def test_ambiguous_state_link_resolves_elsewhere(tmp_path: Path):
    profile_dir = tmp_path / "profiles" / "2026-05-12-current"
    target = profile_dir / "claude"
    target.mkdir(parents=True)
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    live = tmp_path / ".claude"
    _make_link(live, elsewhere)  # symlink to a non-target
    intent = _make_intent(live_path=live)
    state = classify_mapping(intent, profile_dir)
    assert state is MappingDiskState.AMBIGUOUS


def test_ambiguous_state_both_real_dir(tmp_path: Path):
    profile_dir = tmp_path / "profiles" / "2026-05-12-current"
    target = profile_dir / "claude"
    target.mkdir(parents=True)
    live = tmp_path / ".claude"
    live.mkdir()  # both target and live exist as real dirs
    intent = _make_intent(live_path=live)
    state = classify_mapping(intent, profile_dir)
    assert state is MappingDiskState.AMBIGUOUS


def test_ambiguous_state_untouched_with_wrong_original_kind(tmp_path: Path):
    profile_dir = tmp_path / "profiles" / "2026-05-12-current"
    # target absent
    live = tmp_path / ".claude"
    live.write_text("not a directory")  # regular file
    intent = _make_intent(live_path=live, original_kind="real-dir")
    state = classify_mapping(intent, profile_dir)
    assert state is MappingDiskState.AMBIGUOUS


def test_classifier_pure_read_no_mutations(tmp_path: Path):
    """Classifier must not mutate the filesystem."""
    profile_dir = tmp_path / "profiles" / "2026-05-12-current"
    target = profile_dir / "claude"
    target.mkdir(parents=True)
    (target / "settings.json").write_text("{}")
    live = tmp_path / ".claude"
    _make_link(live, target)
    intent = _make_intent(live_path=live)
    snapshot_before = sorted(p.name for p in tmp_path.rglob("*"))
    classify_mapping(intent, profile_dir)
    snapshot_after = sorted(p.name for p in tmp_path.rglob("*"))
    assert snapshot_before == snapshot_after
