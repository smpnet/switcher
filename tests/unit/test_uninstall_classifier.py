# pyright: reportPrivateUsage=none
"""Per-DirMapping classifier for uninstall pre-flight (spec §3.2).

Tests reach into private classifier symbols on purpose — there is no public
wrapper for the per-DirMapping pre-flight, and the classifier needs direct
coverage independent of `service.uninstall`'s orchestrator (Task 10).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from switcher.paths import IS_WINDOWS, PathResolver
from switcher.registry import build_registry
from switcher.service import ProfileService
from switcher.service import _UninstallMappingState as State
from switcher.store import FileProfileStore


def _build_initialized(tmp_state: Path, tmp_home: Path) -> ProfileService:
    store = FileProfileStore(tmp_state)
    resolver = PathResolver(home=tmp_home)
    registry = build_registry(tmp_state / "registry.d")
    service = ProfileService(store, resolver, registry)
    service.init()
    return service


def _drop_link(p: Path) -> None:
    if IS_WINDOWS and os.path.isjunction(p):
        p.rmdir()
        return
    if p.is_symlink():
        p.unlink()


def test_classify_symlink_state(tmp_state: Path, tmp_home: Path) -> None:
    service = _build_initialized(tmp_state, tmp_home)
    mappings = service._classify_uninstall_mappings()
    assert mappings, "classifier returned no mappings"
    for m in mappings:
        assert m.state == State.SYMLINK


def test_classify_already_restored(tmp_state: Path, tmp_home: Path) -> None:
    """A live path that's already a real dir matching the profile contents = ALREADY_RESTORED."""
    service = _build_initialized(tmp_state, tmp_home)
    mappings = service._classify_uninstall_mappings()
    target_mapping = mappings[0]
    target_dir = target_mapping.profile_dir_subdir
    live = target_mapping.live_path
    _drop_link(live)
    shutil.copytree(target_dir, live)

    re_classified = {
        (m.tool_id, m.profile_subdir): m.state for m in service._classify_uninstall_mappings()
    }
    assert (
        re_classified[(target_mapping.tool_id, target_mapping.profile_subdir)]
        == State.ALREADY_RESTORED
    )


def test_classify_corrupt_when_real_dir_does_not_match_profile(
    tmp_state: Path, tmp_home: Path
) -> None:
    service = _build_initialized(tmp_state, tmp_home)
    mappings = service._classify_uninstall_mappings()
    m = mappings[0]
    live = m.live_path
    _drop_link(live)
    live.mkdir()
    (live / "bogus.txt").write_text("not in profile")

    re_classified = next(
        c
        for c in service._classify_uninstall_mappings()
        if (c.tool_id, c.profile_subdir) == (m.tool_id, m.profile_subdir)
    )
    assert re_classified.state == State.CORRUPT
    assert re_classified.corruption_reason


def test_classify_missing_live_temp_present(tmp_state: Path, tmp_home: Path) -> None:
    """Simulate a crash between unlink and rename — live path missing, sibling temp exists."""
    from switcher.service import _temp_dir_for_uninstall

    service = _build_initialized(tmp_state, tmp_home)
    mappings = service._classify_uninstall_mappings()
    m = mappings[0]
    live = m.live_path
    temp = _temp_dir_for_uninstall(live)

    _drop_link(live)
    shutil.copytree(m.profile_dir_subdir, temp)

    re_classified = next(
        c
        for c in service._classify_uninstall_mappings()
        if (c.tool_id, c.profile_subdir) == (m.tool_id, m.profile_subdir)
    )
    assert re_classified.state == State.MISSING_LIVE_TEMP_PRESENT


def test_classify_corrupt_when_symlink_target_does_not_match_profile(
    tmp_state: Path, tmp_home: Path
) -> None:
    """A symlink that points somewhere unexpected MUST NOT classify as SYMLINK.

    Without this check, uninstall would silently swap the link with a copy of
    profile contents, destroying whatever the link actually pointed at.
    """
    service = _build_initialized(tmp_state, tmp_home)
    mappings = service._classify_uninstall_mappings()
    m = mappings[0]
    bogus = tmp_home / "bogus-target"
    bogus.mkdir()
    if m.live_path.is_symlink() or (IS_WINDOWS and os.path.isjunction(m.live_path)):
        if IS_WINDOWS and os.path.isjunction(m.live_path):
            m.live_path.rmdir()
            from switcher.links import _create_junction

            _create_junction(bogus, m.live_path)
        else:
            m.live_path.unlink()
            m.live_path.symlink_to(bogus)

    re_classified = next(
        c
        for c in service._classify_uninstall_mappings()
        if (c.tool_id, c.profile_subdir) == (m.tool_id, m.profile_subdir)
    )
    assert re_classified.state == State.CORRUPT
    assert "points to" in re_classified.corruption_reason


def test_classify_corrupt_when_temp_dir_collides_with_live_link(
    tmp_state: Path, tmp_home: Path
) -> None:
    """Pre-flight catches an unrelated temp-dir collision instead of overwriting it."""
    from switcher.service import _temp_dir_for_uninstall

    service = _build_initialized(tmp_state, tmp_home)
    mappings = service._classify_uninstall_mappings()
    m = mappings[0]
    temp = _temp_dir_for_uninstall(m.live_path)
    temp.mkdir()
    (temp / "user-data.txt").write_text("not a switcher artifact")

    re_classified = next(
        c
        for c in service._classify_uninstall_mappings()
        if (c.tool_id, c.profile_subdir) == (m.tool_id, m.profile_subdir)
    )
    assert re_classified.state == State.CORRUPT
    assert "temp dir" in re_classified.corruption_reason
