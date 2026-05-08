# pyright: reportPrivateUsage=none
"""End-to-end rescan flow (spec §4).

A few tests reach into private store internals (`s._store.get_active`,
`s._store.get(...)`) to inspect/seed state that isn't surfaced by the public
API. The pragma keeps strict-typecheck quiet for those test-only paths.
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from switcher.errors import (
    AlreadyLinkedError,
    PathNotADirectoryError,
    RescanCaptureError,
)
from switcher.paths import IS_WINDOWS, PathResolver
from switcher.registry import build_registry
from switcher.service import ProfileService
from switcher.store import FileProfileStore

pytestmark = pytest.mark.integration


def _service(tmp_state: Path, tmp_home: Path) -> ProfileService:
    return ProfileService(
        FileProfileStore(tmp_state),
        PathResolver(home=tmp_home),
        build_registry(tmp_state / "registry.d"),
    )


def _is_link(p: Path) -> bool:
    return p.is_symlink() or (IS_WINDOWS and os.path.isjunction(p))


def _remove_path(p: Path) -> None:
    """Link-aware removal helper for tests.

    `shutil.rmtree` raises on symlinks (and on Windows junctions). Tests that
    teardown directories that MIGHT be links — common after `init` — use this
    helper instead. It dispatches to `links.remove_link` for links and
    `shutil.rmtree` for real dirs.
    """
    if not p.exists() and not p.is_symlink():
        return
    from switcher.links import remove_link

    if p.is_symlink() or (IS_WINDOWS and os.path.isjunction(p)):
        remove_link(p)
    elif p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()


def test_rescan_default_creates_fresh_profile_per_new_tool(tmp_state: Path, tmp_home: Path) -> None:
    """Init claude only, then drop a copilot dir, then rescan → fresh profile for copilot."""
    s = _service(tmp_state, tmp_home)
    # Pretend copilot wasn't installed at init time.
    _remove_path(tmp_home / ".copilot")
    _remove_path(tmp_home / ".config" / "github-copilot")
    s.init()  # captures only claude

    # Now "install" copilot post-init.
    (tmp_home / ".copilot").mkdir()
    (tmp_home / ".copilot" / "settings.json").write_text("{}")
    (tmp_home / ".config" / "github-copilot").mkdir()
    (tmp_home / ".config" / "github-copilot" / "apps.json").write_text("{}")

    s.rescan()

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert (tmp_state / "profiles" / f"{today}-rescan-1").is_dir()
    assert "copilot" in s._store.get_active()
    assert "copilot" in s._store.get_active_live_paths()


def test_rescan_no_new_tools(tmp_state: Path, tmp_home: Path) -> None:
    s = _service(tmp_state, tmp_home)
    s.init()
    report = s.rescan()
    assert report.captured == []


def test_rescan_into_existing_profile(tmp_state: Path, tmp_home: Path) -> None:
    s = _service(tmp_state, tmp_home)
    _remove_path(tmp_home / ".copilot")
    _remove_path(tmp_home / ".config" / "github-copilot")
    s.init()
    (tmp_home / ".copilot").mkdir()
    (tmp_home / ".config" / "github-copilot").mkdir()

    init_profile = next(iter(s._store.get_active().values()))
    s.rescan(into=init_profile)

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert not (tmp_state / "profiles" / f"{today}-rescan-1").exists()
    # copilot's content lives in the init profile now.
    assert (tmp_state / "profiles" / init_profile / "copilot-config").exists()


def test_rescan_into_collision_refused(tmp_state: Path, tmp_home: Path) -> None:
    """A target profile that already has the tool's subdir → RescanCaptureError.

    Setup discipline: suppress copilot at init time so it's not in the active
    map (otherwise the `--only` "already managed" check fires first, not the
    collision check we're trying to test).
    """
    s = _service(tmp_state, tmp_home)
    # Suppress copilot at init: remove its live dirs.
    for sub in [".copilot", ".config/github-copilot"]:
        path = tmp_home / sub
        if path.exists():
            _remove_path(path)  # link-aware; see test helper above
    s.init()  # claude only

    # Recreate copilot live dirs (eligible for rescan).
    (tmp_home / ".copilot").mkdir()
    (tmp_home / ".config" / "github-copilot").mkdir(parents=True)

    # Manually pre-create the colliding subdir inside the init profile.
    init_profile = next(iter(s._store.get_active().values()))
    (tmp_state / "profiles" / init_profile / "copilot-config").mkdir()

    with pytest.raises(RescanCaptureError, match="already has"):
        s.rescan(into=init_profile, only=["copilot"])


def test_rescan_seeds_missing_secondary_config_dir(tmp_state: Path, tmp_home: Path) -> None:
    """Mirror init's move_or_seed_dir behavior for missing secondary dirs."""
    s = _service(tmp_state, tmp_home)
    _remove_path(tmp_home / ".copilot")
    _remove_path(tmp_home / ".config" / "github-copilot")
    s.init()  # claude only

    # First config dir present (~/.config/github-copilot), second absent.
    (tmp_home / ".config" / "github-copilot").mkdir(parents=True)

    s.rescan(only=["copilot"])

    # Secondary dir was seeded empty; symlink created.
    assert _is_link(tmp_home / ".copilot")


def test_rescan_real_file_at_live_path_raises_path_not_a_directory(
    tmp_state: Path, tmp_home: Path
) -> None:
    """Spec §4.3: regular file at live path → PathNotADirectoryError, not AlreadyLinkedError.

    Setup: suppress copilot at init (both config dirs removed), then recreate
    the first config dir as a real dir (so detection finds copilot) and place
    a regular file at the second (so pre-flight rejects it).
    """
    s = _service(tmp_state, tmp_home)
    _remove_path(tmp_home / ".copilot")
    _remove_path(tmp_home / ".config" / "github-copilot")
    s.init()  # claude only
    (tmp_home / ".config" / "github-copilot").mkdir(parents=True)
    (tmp_home / ".copilot").write_text("not a dir")
    with pytest.raises(PathNotADirectoryError):
        s.rescan(only=["copilot"])


def test_rescan_rolls_back_on_partial_capture_failure(
    tmp_state: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §4.4 / §6.4: if mapping N+1 fails after 0..N captured, undo 0..N."""
    s = _service(tmp_state, tmp_home)
    # Suppress copilot at init so it ends up as the rescan target.
    _remove_path(tmp_home / ".copilot")
    _remove_path(tmp_home / ".config" / "github-copilot")
    s.init()  # claude only

    # Recreate copilot dirs with content.
    (tmp_home / ".copilot").mkdir()
    (tmp_home / ".copilot" / "settings.json").write_text("{copilot-config-marker}")
    (tmp_home / ".config" / "github-copilot").mkdir(parents=True)
    (tmp_home / ".config" / "github-copilot" / "apps.json").write_text("{copilot-auth-marker}")

    # Force the SECOND swap_link call to fail.
    from switcher import service as svc_mod

    real_swap = svc_mod.swap_link
    call_count = {"n": 0}

    def flaky_swap(target: Path, link: Path) -> None:
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated FS error on second mapping")
        return real_swap(target, link)

    monkeypatch.setattr(svc_mod, "swap_link", flaky_swap)

    with pytest.raises(RescanCaptureError):
        s.rescan(only=["copilot"])

    # Both live paths are real dirs again (rolled back).
    assert (tmp_home / ".config" / "github-copilot").is_dir()
    assert not _is_link(tmp_home / ".config" / "github-copilot")
    assert (tmp_home / ".copilot").is_dir()
    assert not _is_link(tmp_home / ".copilot")
    # Original content preserved.
    assert (tmp_home / ".copilot" / "settings.json").read_text() == "{copilot-config-marker}"
    assert (
        tmp_home / ".config" / "github-copilot" / "apps.json"
    ).read_text() == "{copilot-auth-marker}"
    # Active map unchanged.
    assert "copilot" not in s._store.get_active()
    # Default-mode profile dir cleaned up.
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert not (tmp_state / "profiles" / f"{today}-rescan-1").exists()


def test_rescan_into_rollback_restores_metadata(
    tmp_state: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §4.4: --into rollback restores the previous metadata.tools map."""
    s = _service(tmp_state, tmp_home)
    _remove_path(tmp_home / ".copilot")
    _remove_path(tmp_home / ".config" / "github-copilot")
    s.init()  # claude only
    init_profile = next(iter(s._store.get_active().values()))
    tools_before = dict(s._store.get(init_profile).tools)

    (tmp_home / ".copilot").mkdir()
    (tmp_home / ".config" / "github-copilot").mkdir(parents=True)

    from switcher import service as svc_mod

    def failing_swap(target: Path, link: Path) -> None:
        raise RuntimeError("simulated FS error")

    monkeypatch.setattr(svc_mod, "swap_link", failing_swap)

    with pytest.raises(RescanCaptureError):
        s.rescan(into=init_profile, only=["copilot"])

    # Metadata is reverted: copilot is NOT in the profile's tools map.
    tools_after = dict(s._store.get(init_profile).tools)
    assert tools_after == tools_before
    assert "copilot" not in tools_after


def test_rescan_already_linked_raises(tmp_state: Path, tmp_home: Path) -> None:
    """Spec §4.3: live path already a link (e.g. user-managed symlink) → AlreadyLinkedError.

    Setup: suppress copilot at init (both config dirs removed), then recreate
    the first config dir as a real dir (so detection finds copilot) and the
    second as a foreign symlink (so pre-flight rejects).
    """
    s = _service(tmp_state, tmp_home)
    _remove_path(tmp_home / ".copilot")
    _remove_path(tmp_home / ".config" / "github-copilot")
    s.init()  # claude only
    (tmp_home / ".config" / "github-copilot").mkdir(parents=True)
    foreign = tmp_home / "foreign-copilot"
    foreign.mkdir()
    (tmp_home / ".copilot").symlink_to(foreign, target_is_directory=True)
    with pytest.raises(AlreadyLinkedError):
        s.rescan(only=["copilot"])
