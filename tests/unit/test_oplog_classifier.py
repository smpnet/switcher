# pyright: reportPrivateUsage=none
"""Tests for the per-mapping disk-state classifier (spec §2.1.1)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from switcher.links import link_dir
from switcher.oplog import (
    MappingDiskState,
    _is_link,
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
    """Create a directory link at ``src`` pointing to ``dst``.

    Delegates to ``switcher.links.link_dir``, which production code
    also uses: symlink on POSIX, directory junction on Windows. Calling
    ``Path.symlink_to`` directly would fail on Windows runners without
    the symlink privilege, making the suite less portable than the
    production code under test. Using the same helper also means the
    Windows junction branch of ``_is_link`` (``os.path.isjunction``) is
    exercised by every test in this file on Windows CI.
    """
    link_dir(dst, src)


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


def test_ambiguous_state_target_is_regular_file_live_missing(tmp_path: Path):
    """A regular file at the target path is "present but wrong shape",
    not "absent". Collapsing it with target-missing would let abort
    treat a corrupted target artifact as UNTOUCHED and proceed as if
    nothing happened — exactly the kind of silent miscompensation the
    classifier exists to refuse.
    """
    profile_dir = tmp_path / "profiles" / "2026-05-12-current"
    profile_dir.mkdir(parents=True)
    target = profile_dir / "claude"
    target.write_text("not a directory")  # regular file at target path
    live = tmp_path / ".claude"  # absent
    intent = _make_intent(live_path=live, original_kind="missing")
    state = classify_mapping(intent, profile_dir)
    assert state is MappingDiskState.AMBIGUOUS


def test_ambiguous_state_target_is_regular_file_live_real_dir(tmp_path: Path):
    """Same as above, but with live in its recorded "real-dir" shape.
    The classifier must still refuse: target is present in the wrong
    shape, so the safe state is unknowable from this snapshot.
    """
    profile_dir = tmp_path / "profiles" / "2026-05-12-current"
    profile_dir.mkdir(parents=True)
    target = profile_dir / "claude"
    target.write_text("not a directory")
    live = tmp_path / ".claude"
    live.mkdir()  # live matches original_kind="real-dir"
    intent = _make_intent(live_path=live, original_kind="real-dir")
    state = classify_mapping(intent, profile_dir)
    assert state is MappingDiskState.AMBIGUOUS


def test_ambiguous_state_target_is_link_to_unrelated_dir(tmp_path: Path):
    """A link at the target path is reparse-point drift, not a real
    directory. ``target.is_dir()`` follows the link and returns True if
    the link resolves to a directory, so without separate
    target-missing logic this could be misclassified as a normal target
    dir. Cover the case explicitly.
    """
    profile_dir = tmp_path / "profiles" / "2026-05-12-current"
    profile_dir.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = profile_dir / "claude"
    _make_link(target, elsewhere)  # symlink/junction to an unrelated dir
    live = tmp_path / ".claude"
    live.mkdir()  # live also exists as a real dir
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


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_is_link_recognizes_directory_junction(tmp_path: Path):
    """Pin the ``os.path.isjunction`` branch of ``_is_link``.

    On Windows, ``link_dir`` creates a junction rather than a symlink
    (no Developer Mode required), and ``Path.is_symlink`` returns False
    for junctions. Without the ``isjunction`` branch, ``_is_link`` would
    misclassify a perfectly valid production-shaped link as not-a-link,
    cascading to AMBIGUOUS in the classifier. This test runs only on
    Windows CI — POSIX has no equivalent reparse-point to construct.
    """
    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "junction"
    link_dir(target, junction)
    assert _is_link(junction) is True
