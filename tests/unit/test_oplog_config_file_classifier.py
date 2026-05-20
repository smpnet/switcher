# pyright: reportPrivateUsage=none
"""Tests for classify_config_file_mapping — the three-state ConfigFile
snapshot classifier (spec §3.7).

The dir-mapping classifier has FOUR states (COMPLETE /
MOVE_DONE_LINK_MISSING / UNTOUCHED / AMBIGUOUS) because move + swap_link
is a two-step commit. For ConfigFile, atomic rename collapses the move
to a single commit point, so MOVE_DONE_LINK_MISSING vanishes and the
state space is COMPLETE / UNTOUCHED / AMBIGUOUS.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from switcher.oplog import (
    ConfigFileDiskState,
    _ConfigFileMappingIntent,
    classify_config_file_mapping,
)

IS_WINDOWS = sys.platform == "win32"


def _entry(
    profile_subdir: str = "claude",
    profile_filename: str = "claude.json",
) -> _ConfigFileMappingIntent:
    # AbsolutePath needs a platform-appropriate canonical string. The
    # path doesn't have to point at an existing file — the classifier
    # only reads the snapshot side (profile_dir / .switcher / ...).
    live_path = "C:\\Users\\test\\.claude.json" if IS_WINDOWS else "/Users/test/.claude.json"
    return _ConfigFileMappingIntent(
        tool_id="claude",
        profile_subdir=profile_subdir,
        profile_filename=profile_filename,
        live_path=live_path,
        owned_json_paths=(".mcpServers",),
    )


def _snap(tmp_path: Path) -> Path:
    return tmp_path / ".switcher" / "config_files" / "claude" / "claude.json"


def test_classifier_complete_when_snapshot_is_valid_json(tmp_path: Path) -> None:
    snap = _snap(tmp_path)
    snap.parent.mkdir(parents=True)
    snap.write_text(json.dumps({"mcpServers": {}}))
    assert classify_config_file_mapping(_entry(), tmp_path) == ConfigFileDiskState.COMPLETE


def test_classifier_untouched_when_snapshot_absent(tmp_path: Path) -> None:
    assert classify_config_file_mapping(_entry(), tmp_path) == ConfigFileDiskState.UNTOUCHED


def test_classifier_ambiguous_when_snapshot_is_dir(tmp_path: Path) -> None:
    """A directory at the snapshot path is a corrupt-shape: snapshot
    writes are atomic file renames, never mkdir."""
    snap = _snap(tmp_path)
    snap.mkdir(parents=True)
    assert classify_config_file_mapping(_entry(), tmp_path) == ConfigFileDiskState.AMBIGUOUS


@pytest.mark.skipif(IS_WINDOWS, reason="symlinks require elevation on Windows")
def test_classifier_ambiguous_when_snapshot_is_symlink(tmp_path: Path) -> None:
    """A symlink at the snapshot path is corrupt: writes use atomic
    rename, never link creation."""
    snap = _snap(tmp_path)
    snap.parent.mkdir(parents=True)
    target = tmp_path / "target.json"
    target.write_text("{}")
    snap.symlink_to(target)
    assert classify_config_file_mapping(_entry(), tmp_path) == ConfigFileDiskState.AMBIGUOUS


@pytest.mark.skipif(IS_WINDOWS, reason="symlinks require elevation on Windows")
def test_classifier_ambiguous_when_snapshot_is_broken_symlink(tmp_path: Path) -> None:
    """A broken symlink at the snapshot path is corrupt — Path.exists()
    returns False, but is_symlink() returns True, so a naive "absent"
    check would misclassify it as UNTOUCHED and overwrite real data."""
    snap = _snap(tmp_path)
    snap.parent.mkdir(parents=True)
    snap.symlink_to(tmp_path / "does-not-exist.json")
    assert classify_config_file_mapping(_entry(), tmp_path) == ConfigFileDiskState.AMBIGUOUS


def test_classifier_ambiguous_when_snapshot_is_malformed_json(tmp_path: Path) -> None:
    """A regular file that parses with json.loads is COMPLETE; anything
    that fails to parse is AMBIGUOUS — refuse to compensate over a file
    we can't safely read."""
    snap = _snap(tmp_path)
    snap.parent.mkdir(parents=True)
    snap.write_text("not valid json {")
    assert classify_config_file_mapping(_entry(), tmp_path) == ConfigFileDiskState.AMBIGUOUS


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        '"x"',
        "null",
        "42",
        "true",
    ],
    ids=["array", "string", "null", "number", "bool"],
)
def test_classifier_ambiguous_when_snapshot_is_valid_json_but_not_object(
    tmp_path: Path, payload: str
) -> None:
    """The snapshot writer always emits a JSON object; a parseable-but-
    wrong shape (array / scalar / null) is detectable corruption that
    must surface AT the classifier boundary (where compensation can
    refuse and the user can recover), not later at apply time when the
    snapshot is consumed against a live read. abby r-batch4 blocker.
    """
    snap = _snap(tmp_path)
    snap.parent.mkdir(parents=True)
    snap.write_text(payload)
    assert classify_config_file_mapping(_entry(), tmp_path) == ConfigFileDiskState.AMBIGUOUS


def test_classifier_ambiguous_when_snapshot_shape_mismatches_owned_paths(
    tmp_path: Path,
) -> None:
    """Hermes pass-PR-5: a snapshot that parses as a JSON object but
    has a non-object value where ``owned_json_paths`` says to descend
    (e.g., ``{"projects": []}`` against ``.projects[].mcpServers``)
    must classify AMBIGUOUS. Pre-fix the classifier returned COMPLETE
    for any dict-shaped JSON, so compensation could short-circuit and
    mark the journal completed while shape corruption persisted in
    the on-disk snapshot — and the next ``use()`` would either
    silently delete owned leaves from live or raise mid-apply.
    """
    snap = _snap(tmp_path)
    snap.parent.mkdir(parents=True)
    # Top-level is a dict (passes the existing object check), but
    # ``projects`` is a list — the iter segment of
    # ``.projects[].mcpServers`` lands on a non-object.
    snap.write_text(json.dumps({"projects": []}))

    # The default _entry() owns_json_paths is (".mcpServers",) which
    # would NOT flag this snapshot. Pass an entry whose journaled
    # owned path actually walks into ``projects``.
    entry_with_iter = _ConfigFileMappingIntent(
        tool_id="claude",
        profile_subdir="claude",
        profile_filename="claude.json",
        live_path="C:\\Users\\test\\.claude.json" if IS_WINDOWS else "/Users/test/.claude.json",
        owned_json_paths=(".projects[].mcpServers",),
    )
    assert classify_config_file_mapping(entry_with_iter, tmp_path) == ConfigFileDiskState.AMBIGUOUS


def test_classifier_pure_read_no_mutations(tmp_path: Path) -> None:
    snap = _snap(tmp_path)
    snap.parent.mkdir(parents=True)
    snap.write_text(json.dumps({"mcpServers": {}}))
    before = snap.read_text()
    parent_listing_before = sorted(snap.parent.iterdir())

    classify_config_file_mapping(_entry(), tmp_path)
    classify_config_file_mapping(_entry(), tmp_path)

    assert snap.read_text() == before
    assert sorted(snap.parent.iterdir()) == parent_listing_before


@pytest.mark.skipif(IS_WINDOWS, reason="symlinks require elevation on Windows")
@pytest.mark.parametrize(
    "ancestor_rel",
    [".switcher", ".switcher/config_files", ".switcher/config_files/claude"],
    ids=["dot-switcher", "config-files", "subdir"],
)
def test_classifier_ambiguous_when_reserved_ancestor_is_symlink(
    tmp_path: Path, ancestor_rel: str
) -> None:
    """Hermes pass-PR-6: a symlink at ``.switcher``, ``config_files``,
    or the per-tool ``<subdir>`` redirects the snapshot leaf out of
    the state store. The leaf's ``is_symlink``/``is_file``/``read_text``
    follow the redirection — pre-fix the classifier returned COMPLETE
    for a snapshot whose real bytes lived in an external directory
    the user did not own. Surface AMBIGUOUS at the classifier so
    compensation refuses instead of treating attacker-controlled
    bytes as healthy state.
    """
    # Lay down a real JSON object at the redirection target so the
    # leaf reads (after symlink-following) WOULD succeed; only the
    # ancestor-link check prevents misclassification.
    external = tmp_path / "outside"
    external.mkdir()
    (external / "claude.json").write_text(json.dumps({"mcpServers": {}}))

    ancestor = tmp_path / ancestor_rel
    ancestor.parent.mkdir(parents=True, exist_ok=True)
    if ancestor_rel == ".switcher/config_files/claude":
        ancestor.symlink_to(external, target_is_directory=True)
    elif ancestor_rel == ".switcher/config_files":
        # Make the redirection still land at a "claude/claude.json"
        # leaf so a naive leaf-only check would pass.
        (external / "claude").mkdir()
        (external / "claude" / "claude.json").write_text(json.dumps({"mcpServers": {}}))
        ancestor.symlink_to(external, target_is_directory=True)
    else:  # ".switcher"
        (external / "config_files" / "claude").mkdir(parents=True)
        (external / "config_files" / "claude" / "claude.json").write_text(
            json.dumps({"mcpServers": {}})
        )
        ancestor.symlink_to(external, target_is_directory=True)

    assert classify_config_file_mapping(_entry(), tmp_path) == ConfigFileDiskState.AMBIGUOUS
