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

# Copilot's two config dirs differ between platforms — see
# src/switcher/builtins/copilot.toml. Tests that suppress + recreate copilot
# need the platform-appropriate first dir so service.detect_installed (which
# checks the FIRST config dir only) sees the tool.
COPILOT_FIRST_DIR = (
    Path("AppData/Local/github-copilot") if IS_WINDOWS else Path(".config/github-copilot")
)
COPILOT_SECOND_DIR = Path(".copilot")


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
    if not p.exists() and not _is_link(p):
        return
    from switcher.links import remove_link

    if _is_link(p):
        remove_link(p)
    elif p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()


def _suppress_copilot(tmp_home: Path) -> None:
    """Remove copilot's live dirs across both platforms so init won't capture
    it. The POSIX paths are no-ops on Windows and vice versa."""
    for sub in [".copilot", ".config/github-copilot", "AppData/Local/github-copilot"]:
        _remove_path(tmp_home / sub)


def test_rescan_default_creates_fresh_profile_per_new_tool(tmp_state: Path, tmp_home: Path) -> None:
    """Init claude only, then drop a copilot dir, then rescan → fresh profile for copilot."""
    s = _service(tmp_state, tmp_home)
    # Pretend copilot wasn't installed at init time.
    _suppress_copilot(tmp_home)
    s.init()  # captures only claude

    # Now "install" copilot post-init.
    (tmp_home / COPILOT_SECOND_DIR).mkdir()
    (tmp_home / COPILOT_SECOND_DIR / "settings.json").write_text("{}")
    (tmp_home / COPILOT_FIRST_DIR).mkdir()
    (tmp_home / COPILOT_FIRST_DIR / "apps.json").write_text("{}")

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
    _suppress_copilot(tmp_home)
    s.init()
    (tmp_home / COPILOT_SECOND_DIR).mkdir()
    (tmp_home / COPILOT_FIRST_DIR).mkdir()

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
    # Suppress copilot at init: remove its live dirs (cross-platform).
    _suppress_copilot(tmp_home)
    s.init()  # claude only

    # Recreate copilot live dirs (eligible for rescan).
    (tmp_home / COPILOT_SECOND_DIR).mkdir()
    (tmp_home / COPILOT_FIRST_DIR).mkdir(parents=True)

    # Manually pre-create the colliding subdir inside the init profile.
    init_profile = next(iter(s._store.get_active().values()))
    (tmp_state / "profiles" / init_profile / "copilot-config").mkdir()

    with pytest.raises(RescanCaptureError, match="already has"):
        s.rescan(into=init_profile, only=["copilot"])


def test_rescan_seeds_missing_secondary_config_dir(tmp_state: Path, tmp_home: Path) -> None:
    """Mirror init's move_or_seed_dir behavior for missing secondary dirs."""
    s = _service(tmp_state, tmp_home)
    _suppress_copilot(tmp_home)
    s.init()  # claude only

    # First config dir present (platform-appropriate path), second absent.
    (tmp_home / COPILOT_FIRST_DIR).mkdir(parents=True)

    s.rescan(only=["copilot"])

    # Secondary dir was seeded empty; symlink created.
    assert _is_link(tmp_home / COPILOT_SECOND_DIR)


def test_rescan_real_file_at_live_path_raises_path_not_a_directory(
    tmp_state: Path, tmp_home: Path
) -> None:
    """Spec §4.3: regular file at live path → PathNotADirectoryError, not AlreadyLinkedError.

    Setup: suppress copilot at init (both config dirs removed), then recreate
    the first config dir as a real dir (so detection finds copilot) and place
    a regular file at the second (so pre-flight rejects it).
    """
    s = _service(tmp_state, tmp_home)
    _suppress_copilot(tmp_home)
    s.init()  # claude only
    (tmp_home / COPILOT_FIRST_DIR).mkdir(parents=True)
    (tmp_home / COPILOT_SECOND_DIR).write_text("not a dir")
    with pytest.raises(PathNotADirectoryError):
        s.rescan(only=["copilot"])


def test_rescan_rolls_back_on_partial_capture_failure(
    tmp_state: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §4.4 / §6.4: if mapping N+1 fails after 0..N captured, undo 0..N."""
    s = _service(tmp_state, tmp_home)
    # Suppress copilot at init so it ends up as the rescan target.
    _suppress_copilot(tmp_home)
    s.init()  # claude only

    # Recreate copilot dirs with content.
    (tmp_home / COPILOT_SECOND_DIR).mkdir()
    (tmp_home / COPILOT_SECOND_DIR / "settings.json").write_text("{copilot-config-marker}")
    (tmp_home / COPILOT_FIRST_DIR).mkdir(parents=True)
    (tmp_home / COPILOT_FIRST_DIR / "apps.json").write_text("{copilot-auth-marker}")

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
    assert (tmp_home / COPILOT_FIRST_DIR).is_dir()
    assert not _is_link(tmp_home / COPILOT_FIRST_DIR)
    assert (tmp_home / COPILOT_SECOND_DIR).is_dir()
    assert not _is_link(tmp_home / COPILOT_SECOND_DIR)
    # Original content preserved.
    assert (
        tmp_home / COPILOT_SECOND_DIR / "settings.json"
    ).read_text() == "{copilot-config-marker}"
    assert (tmp_home / COPILOT_FIRST_DIR / "apps.json").read_text() == "{copilot-auth-marker}"
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
    _suppress_copilot(tmp_home)
    s.init()  # claude only
    init_profile = next(iter(s._store.get_active().values()))
    tools_before = dict(s._store.get(init_profile).tools)

    (tmp_home / COPILOT_SECOND_DIR).mkdir()
    (tmp_home / COPILOT_FIRST_DIR).mkdir(parents=True)

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


def test_rescan_into_rollback_restores_live_dir_contents(
    tmp_state: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §4.4: --into rollback must restore live dir contents, not just
    metadata. Without the link-aware restore in `_rollback_partial_rescan`,
    a swap_link failure mid-capture would silently delete the user's data."""
    s = _service(tmp_state, tmp_home)
    _suppress_copilot(tmp_home)
    s.init()  # claude only
    init_profile = next(iter(s._store.get_active().values()))

    (tmp_home / COPILOT_SECOND_DIR).mkdir()
    (tmp_home / COPILOT_SECOND_DIR / "sentinel.txt").write_text("user-data")
    (tmp_home / COPILOT_FIRST_DIR).mkdir(parents=True)
    (tmp_home / COPILOT_FIRST_DIR / "config").write_text("settings")

    from switcher import service as svc_mod

    def failing_swap(target: Path, link: Path) -> None:
        raise RuntimeError("simulated FS error")

    monkeypatch.setattr(svc_mod, "swap_link", failing_swap)

    with pytest.raises(RescanCaptureError):
        s.rescan(into=init_profile, only=["copilot"])

    # Live dirs and contents must be restored — NOT silently deleted.
    assert (tmp_home / COPILOT_SECOND_DIR).is_dir()
    assert (tmp_home / COPILOT_SECOND_DIR / "sentinel.txt").read_text() == "user-data"
    assert (tmp_home / COPILOT_FIRST_DIR).is_dir()
    assert (tmp_home / COPILOT_FIRST_DIR / "config").read_text() == "settings"


def test_rescan_rollback_does_not_create_empty_live_dir_for_seeded_mapping(
    tmp_state: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (abby-review): when a secondary live path is missing, the
    capture loop "seeds" an empty subdir at the profile target. If a later
    swap_link fails and rollback runs, the seeded mapping must NOT mkdir
    the live path back into existence — the original state was "missing",
    not "empty dir present"."""
    s = _service(tmp_state, tmp_home)
    _suppress_copilot(tmp_home)
    s.init()  # claude only

    # Set up: first config dir present, second missing → second is seeded.
    (tmp_home / COPILOT_FIRST_DIR).mkdir(parents=True)
    (tmp_home / COPILOT_FIRST_DIR / "config").write_text("settings")
    # `.copilot` deliberately NOT created — this is the seeded mapping.

    from switcher import service as svc_mod

    # Fail on the second swap_link (after the seed mapping has been seeded
    # and after the move mapping has been moved+linked).
    real_swap = svc_mod.swap_link
    call_count = {"n": 0}

    def maybe_failing_swap(target: Path, link: Path) -> None:
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated FS error on second swap_link")
        real_swap(target, link)

    monkeypatch.setattr(svc_mod, "swap_link", maybe_failing_swap)

    with pytest.raises(RescanCaptureError):
        s.rescan(only=["copilot"])

    # Critical assertion: `.copilot` was missing originally and must STAY
    # missing after rollback. Creating an empty dir here would corrupt
    # detection on the next rescan run.
    assert not (tmp_home / COPILOT_SECOND_DIR).exists()
    assert not (tmp_home / COPILOT_SECOND_DIR).is_symlink()


def test_rescan_state_write_failure_rolls_back_capture(
    tmp_state: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §4.4: per-tool capture is finalized only when the active +
    active_live_paths write lands. If `set_active_state` fails after a
    successful capture, rollback must undo the link/move so retries don't
    hit AlreadyLinkedError on the (now-symlinked) live dirs.
    """
    s = _service(tmp_state, tmp_home)
    _suppress_copilot(tmp_home)
    s.init()  # claude only

    (tmp_home / COPILOT_SECOND_DIR).mkdir()
    (tmp_home / COPILOT_SECOND_DIR / "settings.json").write_text("user-config")
    (tmp_home / COPILOT_FIRST_DIR).mkdir(parents=True)
    (tmp_home / COPILOT_FIRST_DIR / "apps.json").write_text("user-auth")

    real_set_active_state = s._store.set_active_state

    def failing_set_active_state(active: object, live_paths: object) -> None:
        # Fail only when the rescan tries to add the new tool entry.
        if "copilot" in active:  # type: ignore[operator]
            raise RuntimeError("simulated state-write failure")
        return real_set_active_state(active, live_paths)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(s._store, "set_active_state", failing_set_active_state)

    with pytest.raises(RescanCaptureError, match="simulated state-write failure"):
        s.rescan(only=["copilot"])

    # Live dirs restored to real directories — not left as symlinks pointing
    # into a profile the active map doesn't know about.
    assert (tmp_home / COPILOT_SECOND_DIR).is_dir()
    assert not _is_link(tmp_home / COPILOT_SECOND_DIR)
    assert (tmp_home / COPILOT_FIRST_DIR).is_dir()
    assert not _is_link(tmp_home / COPILOT_FIRST_DIR)
    # Original content preserved at live paths.
    assert (tmp_home / COPILOT_SECOND_DIR / "settings.json").read_text() == "user-config"
    assert (tmp_home / COPILOT_FIRST_DIR / "apps.json").read_text() == "user-auth"
    # Active map and the partial profile dir are unchanged on disk.
    assert "copilot" not in s._store.get_active()
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert not (tmp_state / "profiles" / f"{today}-rescan-1").exists()


def test_rescan_default_rollback_fails_closed_when_restore_fails(
    tmp_state: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §4.4: when rollback's `move_or_seed_dir(sub, live)` fails, leave
    captured data on disk and surface a RescanCaptureError mentioning the
    leftover paths. Do NOT silently rmtree the partial profile dir.

    Repro shape (Hermes review): swap_link fails on the second mapping AND
    the rollback's restore call also fails. Without fail-closed semantics
    the user's only copy of the live dir gets rmtree'd via the partial
    profile dir.
    """
    s = _service(tmp_state, tmp_home)
    _suppress_copilot(tmp_home)
    s.init()  # claude only

    (tmp_home / COPILOT_SECOND_DIR).mkdir()
    (tmp_home / COPILOT_SECOND_DIR / "sentinel.txt").write_text("user-config")
    (tmp_home / COPILOT_FIRST_DIR).mkdir(parents=True)
    (tmp_home / COPILOT_FIRST_DIR / "apps.json").write_text("user-auth")

    from switcher import service as svc_mod

    real_swap = svc_mod.swap_link
    real_move = svc_mod.move_or_seed_dir
    phase = {"rollback": False}
    swap_count = {"n": 0}

    def flaky_swap(target: Path, link: Path) -> None:
        swap_count["n"] += 1
        if swap_count["n"] == 2:
            phase["rollback"] = True
            raise RuntimeError("simulated FS error on second swap_link")
        return real_swap(target, link)

    def flaky_move(src: Path, dst: Path) -> None:
        if phase["rollback"]:
            raise RuntimeError("simulated FS error during rollback restore")
        return real_move(src, dst)

    monkeypatch.setattr(svc_mod, "swap_link", flaky_swap)
    monkeypatch.setattr(svc_mod, "move_or_seed_dir", flaky_move)

    with pytest.raises(RescanCaptureError, match="user data left at"):
        s.rescan(only=["copilot"])

    # Captured user data still on disk in the partial profile dir — NOT
    # silently rmtree'd. The user can recover it manually.
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    profile_dir = tmp_state / "profiles" / f"{today}-rescan-1"
    assert profile_dir.exists()
    config_sentinel = profile_dir / "copilot-config" / "sentinel.txt"
    auth_sentinel = profile_dir / "copilot-auth" / "apps.json"
    assert config_sentinel.exists() and config_sentinel.read_text() == "user-config"
    assert auth_sentinel.exists() and auth_sentinel.read_text() == "user-auth"
    # Active map unchanged.
    assert "copilot" not in s._store.get_active()


def test_rescan_into_rollback_fails_closed_when_restore_fails(
    tmp_state: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §4.4: `--into` rollback must also fail closed if the restore
    call fails — leave the captured sub on disk rather than silent rmtree."""
    s = _service(tmp_state, tmp_home)
    _suppress_copilot(tmp_home)
    s.init()  # claude only
    init_profile = next(iter(s._store.get_active().values()))

    (tmp_home / COPILOT_SECOND_DIR).mkdir()
    (tmp_home / COPILOT_SECOND_DIR / "sentinel.txt").write_text("user-config")
    (tmp_home / COPILOT_FIRST_DIR).mkdir(parents=True)
    (tmp_home / COPILOT_FIRST_DIR / "apps.json").write_text("user-auth")

    from switcher import service as svc_mod

    real_swap = svc_mod.swap_link
    real_move = svc_mod.move_or_seed_dir
    phase = {"rollback": False}
    swap_count = {"n": 0}

    def flaky_swap(target: Path, link: Path) -> None:
        swap_count["n"] += 1
        if swap_count["n"] == 2:
            phase["rollback"] = True
            raise RuntimeError("simulated FS error on second swap_link")
        return real_swap(target, link)

    def flaky_move(src: Path, dst: Path) -> None:
        if phase["rollback"]:
            raise RuntimeError("simulated FS error during rollback restore")
        return real_move(src, dst)

    monkeypatch.setattr(svc_mod, "swap_link", flaky_swap)
    monkeypatch.setattr(svc_mod, "move_or_seed_dir", flaky_move)

    with pytest.raises(RescanCaptureError, match="user data left at"):
        s.rescan(into=init_profile, only=["copilot"])

    # User data still on disk under the existing profile's per-tool subdirs.
    profile_dir = tmp_state / "profiles" / init_profile
    config_sentinel = profile_dir / "copilot-config" / "sentinel.txt"
    auth_sentinel = profile_dir / "copilot-auth" / "apps.json"
    assert config_sentinel.exists() and config_sentinel.read_text() == "user-config"
    assert auth_sentinel.exists() and auth_sentinel.read_text() == "user-auth"
    # Metadata never recorded copilot under --into.
    assert "copilot" not in s._store.get(init_profile).tools


def test_rescan_already_linked_raises(tmp_state: Path, tmp_home: Path) -> None:
    """Spec §4.3: live path already a link (e.g. user-managed symlink) → AlreadyLinkedError.

    Setup: suppress copilot at init (both config dirs removed), then recreate
    the first config dir as a real dir (so detection finds copilot) and the
    second as a foreign symlink (so pre-flight rejects).
    """
    s = _service(tmp_state, tmp_home)
    _suppress_copilot(tmp_home)
    s.init()  # claude only
    (tmp_home / COPILOT_FIRST_DIR).mkdir(parents=True)
    foreign = tmp_home / "foreign-copilot"
    foreign.mkdir()
    (tmp_home / COPILOT_SECOND_DIR).symlink_to(foreign, target_is_directory=True)
    with pytest.raises(AlreadyLinkedError):
        s.rescan(only=["copilot"])
