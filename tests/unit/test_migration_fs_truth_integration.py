# pyright: reportPrivateUsage=none
"""Integration tests for the FS-truth migration path (spec §4.2)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from switcher.links import _create_junction
from switcher.paths import IS_WINDOWS, PathResolver
from switcher.registry import build_registry
from switcher.service import ProfileService
from switcher.store import FileProfileStore


def _make_service(state_dir: Path, home: Path) -> ProfileService:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "registry.d").mkdir(parents=True, exist_ok=True)
    store = FileProfileStore(state_dir)
    return ProfileService(store, PathResolver(home=home), build_registry(state_dir / "registry.d"))


def _replace_symlink(link: Path, target: Path) -> None:
    """Replace whatever is at `link` (real dir, file, or symlink/junction)
    with a directory link pointing at `target`. Idempotent — handles the
    conftest seed that pre-creates real dirs at the live-path locations.

    On Windows uses a directory junction so the suite runs in CI without
    Developer Mode / elevation (matches the repo pattern in test_rescan.py).
    Junction removal goes through rmdir(); unlink() on a junction raises
    because junctions are directory reparse points (mirrors links.remove_link).
    """
    if IS_WINDOWS and os.path.isjunction(link):
        link.rmdir()
    elif link.is_symlink():
        link.unlink()
    elif link.is_dir():
        shutil.rmtree(link)
    elif link.exists():
        link.unlink()
    if IS_WINDOWS:
        _create_junction(target, link)
    else:
        link.symlink_to(target)


def _write_legacy_config(state_dir: Path, active: dict[str, str]) -> None:
    """Overwrite config.json with a v0.1.0..v0.1.2-shape payload (active map
    only; NO active_live_paths key). Forces the legacy-migration path."""
    (state_dir / "config.json").write_text(json.dumps({"active": active}))


@pytest.mark.skipif(
    IS_WINDOWS, reason="POSIX-specific legacy parent (~/.config); Windows uses %LOCALAPPDATA%"
)
def test_legacy_migration_with_drifted_copilot_two_dir(tmp_state: Path, tmp_home: Path) -> None:
    """User initialized v0.1.0 with the two-dir Copilot layout. Registry
    has since rewritten to single-dir. Migration must discover BOTH live
    paths via FS-truth + legacy-parent rescue."""
    service = _make_service(tmp_state, tmp_home)
    store = service._store
    profile_name = "legacy-current"

    # 1. Use the public store API to create the profile + metadata.json.
    store.create(profile_name, {"copilot": True})
    profile_dir = store.profile_dir(profile_name)
    # The legacy two-dir Copilot layout had BOTH subdirs on disk.
    (profile_dir / "copilot-config").mkdir(parents=True, exist_ok=True)
    (profile_dir / "copilot-auth").mkdir(parents=True, exist_ok=True)

    # 2. Overwrite config.json with the legacy v0.1.0 shape (no
    # active_live_paths key) to force migration on read.
    _write_legacy_config(tmp_state, {"copilot": profile_name})

    # 3. Pre-stage both symlinks: ~/.copilot (current registry parent)
    # and ~/.config/github-copilot (legacy parent — the orphan target).
    new_link = tmp_home / ".copilot"
    _replace_symlink(new_link, profile_dir / "copilot-config")
    config_parent = tmp_home / ".config"
    config_parent.mkdir(exist_ok=True)
    old_link = config_parent / "github-copilot"
    _replace_symlink(old_link, profile_dir / "copilot-auth")

    # 4. Trigger migration via get_active_live_paths (derives on-read).
    derived = service.get_active_live_paths()
    assert "copilot" in derived
    paths = set(derived["copilot"])
    assert str(new_link) in paths
    assert str(old_link) in paths


@pytest.mark.skipif(
    IS_WINDOWS, reason="POSIX-specific legacy parent (~/.config); Windows uses %LOCALAPPDATA%"
)
def test_legacy_migration_warns_on_count_mismatch(
    tmp_state: Path, tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Discovery finds more paths than the current registry promises →
    stderr warning suggests unmanage + rescan."""
    service = _make_service(tmp_state, tmp_home)
    profile_name = "warn-current"
    service._store.create(profile_name, {"copilot": True})
    profile_dir = service._store.profile_dir(profile_name)
    (profile_dir / "copilot-config").mkdir(parents=True, exist_ok=True)
    (profile_dir / "copilot-auth").mkdir(parents=True, exist_ok=True)
    _write_legacy_config(tmp_state, {"copilot": profile_name})

    new_link = tmp_home / ".copilot"
    _replace_symlink(new_link, profile_dir / "copilot-config")
    config_parent = tmp_home / ".config"
    config_parent.mkdir(exist_ok=True)
    old_link = config_parent / "github-copilot"
    _replace_symlink(old_link, profile_dir / "copilot-auth")

    service.get_active_live_paths()
    err = capsys.readouterr().err
    assert "registry has" in err
    assert "live path" in err
