# pyright: reportPrivateUsage=none
"""Tests for ConfigFile seeding during ProfileService.create().

The fixtures (`tmp_home`, `tmp_state`) come from tests/conftest.py. The
ConfigFile-equipped registry follows the same shape as the save/use/init
suites (spec §3.3).

create() copies the source profile's ConfigFile snapshot into the new
profile (state-store data copy, not a live capture). Without it, the
first `switcher use <new>` would hit snapshot-missing for every
ConfigFile-equipped tool and warn-and-skip — defeating the point of
creating the profile.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from switcher.errors import StorageError
from switcher.models import ConfigFile, Tool
from switcher.paths import PathResolver
from switcher.registry import build_registry
from switcher.service import ProfileService
from switcher.store import FileProfileStore

IS_WINDOWS = sys.platform == "win32"

CLAUDE_CONFIG_FILE = ConfigFile(
    posix_path="~/.claude.json",
    windows_path="%USERPROFILE%\\.claude.json",
    profile_subdir="claude",
    profile_filename="claude.json",
    merge_strategy="json_subtree_merge",
    owned_json_paths=(
        ".mcpServers",
        ".projects[].mcpServers",
        ".oauthAccount",
    ),
)


@pytest.fixture
def registry(tmp_path: Path) -> tuple[Tool, ...]:
    # Guaranteed-missing directory under ``tmp_path`` keeps the fixture
    # hermetic and platform-independent (CR pass-PR-3 minor).
    base = build_registry(tmp_path / "missing-registry-dir")
    out: list[Tool] = []
    for t in base:
        if t.id == "claude":
            out.append(t.model_copy(update={"config_files": (CLAUDE_CONFIG_FILE,)}))
        else:
            out.append(t)
    return tuple(out)


@pytest.fixture
def service(tmp_home: Path, tmp_state: Path, registry: tuple[Tool, ...]) -> ProfileService:
    store = FileProfileStore(tmp_state)
    resolver = PathResolver(home=tmp_home)
    return ProfileService(store, resolver, registry)


def test_create_seeds_config_file_snapshot_from_active_source(
    service: ProfileService, tmp_home: Path
) -> None:
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"shared": {"command": "x"}}}))
    service.init(["claude"])
    service.save("profA")  # profA's snapshot has the MCP

    service.create("profB")

    src_snap = service._store.config_file_snapshot_path("profA", "claude", "claude.json")
    dst_snap = service._store.config_file_snapshot_path("profB", "claude", "claude.json")
    assert dst_snap.exists()
    assert dst_snap.read_text() == src_snap.read_text()


def test_create_silently_skips_when_source_snapshot_missing(
    service: ProfileService, tmp_home: Path
) -> None:
    """Source profile pre-dates this feature: silently skip. rescan repairs.

    create() seeds from the active map's source, not from any save()-target
    name (save doesn't set active). Simulate a legacy profile by deleting
    the snapshot of whichever profile is currently active for claude.
    """
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {}}))
    service.init(["claude"])
    src_profile = service._store.get_active()["claude"]

    # Simulate pre-feature profile by deleting its snapshot.
    src_snap = service._store.config_file_snapshot_path(src_profile, "claude", "claude.json")
    src_snap.unlink()

    service.create("profB")  # must not raise
    dst_snap = service._store.config_file_snapshot_path("profB", "claude", "claude.json")
    assert not dst_snap.exists()


def test_create_rollback_removes_partial_profile_on_seed_failure(
    service: ProfileService,
    tmp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure during ConfigFile snapshot seeding must roll back the
    partial profile dir so retry isn't blocked by ProfileExistsError.

    The seed step uses ``atomic_write_file`` for ConfigFile snapshots
    (CR pass-PR-3: writes the already-validated bytes through the
    project's atomic helper instead of re-reading via shutil.copy2).
    Patch that helper to force the failure at the snapshot-write site.
    """
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"a": {}}}))
    service.init(["claude"])
    service.save("profA")

    from switcher import service as service_mod

    real_atomic = service_mod.atomic_write_file

    def failing_atomic(target: Path, content: bytes) -> None:
        # Fail only when writing the ConfigFile snapshot; credential
        # copies (which still use shutil.copy2) and any unrelated
        # atomic writes pass through.
        if str(target).endswith("claude.json"):
            raise OSError("simulated seed failure")
        real_atomic(target, content)

    monkeypatch.setattr(service_mod, "atomic_write_file", failing_atomic)
    with pytest.raises(OSError, match="simulated seed failure"):
        service.create("profB")
    assert not service._store.profile_dir("profB").exists()


def test_create_rejects_non_regular_file_at_source_snapshot(
    service: ProfileService, tmp_home: Path
) -> None:
    """A directory at the source snapshot path is corruption — snapshot
    writers always emit regular files. Refuse at the seed boundary
    rather than propagate the wrong shape into the child profile.
    """
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"a": {}}}))
    service.init(["claude"])
    service.save("profA")
    service.use("profA")

    src_snap = service._store.config_file_snapshot_path("profA", "claude", "claude.json")
    src_snap.unlink()
    src_snap.mkdir()

    with pytest.raises(StorageError, match="directory"):
        service.create("profB")
    assert not service._store.profile_dir("profB").exists()


def test_create_rejects_non_utf8_source_snapshot(service: ProfileService, tmp_home: Path) -> None:
    """Non-UTF-8 bytes at the source snapshot path are corruption —
    snapshot writers always emit UTF-8 encoded JSON.
    """
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"a": {}}}))
    service.init(["claude"])
    service.save("profA")
    service.use("profA")

    src_snap = service._store.config_file_snapshot_path("profA", "claude", "claude.json")
    # 0xff is an invalid UTF-8 start byte.
    src_snap.write_bytes(b"\xff\xfe\xfd")

    with pytest.raises(StorageError, match="non-UTF-8"):
        service.create("profB")
    assert not service._store.profile_dir("profB").exists()


def test_create_rejects_malformed_json_source_snapshot(
    service: ProfileService, tmp_home: Path
) -> None:
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"a": {}}}))
    service.init(["claude"])
    service.save("profA")
    service.use("profA")

    src_snap = service._store.config_file_snapshot_path("profA", "claude", "claude.json")
    src_snap.write_text("not valid json {")

    with pytest.raises(StorageError, match="malformed"):
        service.create("profB")
    assert not service._store.profile_dir("profB").exists()


def test_create_rejects_non_object_source_snapshot(service: ProfileService, tmp_home: Path) -> None:
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"a": {}}}))
    service.init(["claude"])
    service.save("profA")
    service.use("profA")

    src_snap = service._store.config_file_snapshot_path("profA", "claude", "claude.json")
    src_snap.write_text(json.dumps(["not", "an", "object"]))

    with pytest.raises(StorageError, match="not a JSON object"):
        service.create("profB")
    assert not service._store.profile_dir("profB").exists()


def test_create_rejects_shape_invalid_source_snapshot(
    service: ProfileService, tmp_home: Path
) -> None:
    """Hermes pass-PR-5: a source snapshot that parses as a JSON object
    but doesn't match the tool's ``owned_json_paths`` shape (e.g.,
    ``{"projects": []}`` against ``.projects[].mcpServers``) must be
    refused at the seed boundary. Pre-fix ``_seed_config_files`` only
    checked top-level dict shape and ``shutil.copy2``'d the corrupt
    snapshot into the child profile; the failure deferred to the first
    ``use`` of that child, which would either silently delete owned
    leaves on apply or raise mid-walk.
    """
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"a": {}}}))
    service.init(["claude"])
    service.save("profA")
    # Switch active to profA so create() seeds from profA.
    service.use("profA")

    # Replace profA's snapshot with a parses-as-JSON-object but
    # shape-incompatible payload. owned_json_paths includes
    # ``.projects[].mcpServers``; ``projects`` as a list breaks the
    # iter contract.
    src_snap = service._store.config_file_snapshot_path("profA", "claude", "claude.json")
    src_snap.write_text(json.dumps({"projects": []}))

    with pytest.raises(StorageError, match="shape-incompatible"):
        service.create("profB")
    # No partial profile left behind: create()'s rollback rmtrees on failure.
    assert not service._store.profile_dir("profB").exists()


@pytest.mark.skipif(IS_WINDOWS, reason="symlinks require elevation on Windows")
def test_create_rejects_symlink_at_source_snapshot(service: ProfileService, tmp_home: Path) -> None:
    """A symlink at the source snapshot path is corruption — snapshot
    writes use atomic rename, never link creation. _seed_config_files
    must refuse rather than copy through or treat a broken symlink as
    "missing", which would propagate corruption into the new profile
    and surface only as warn-and-skip on the next `use` (Hermes
    pass-PR-2).
    """
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"a": {}}}))
    service.init(["claude"])
    service.save("profA")
    # create() seeds from the active source — switch to profA so that
    # profA becomes the seed source and the corrupt snapshot is the one
    # the seed step will try to copy.
    service.use("profA")

    # Replace profA's snapshot with a (non-broken) symlink so _seed_config_files
    # would otherwise copy through it. Pre-fix, the path was followed silently.
    src_snap = service._store.config_file_snapshot_path("profA", "claude", "claude.json")
    src_content = src_snap.read_text()
    src_snap.unlink()
    real_target = tmp_home / "real-target.json"
    real_target.write_text(src_content)
    src_snap.symlink_to(real_target)

    with pytest.raises(StorageError, match="symlink"):
        service.create("profB")
    # No partial profile left behind: create()'s rollback rmtrees on failure.
    assert not service._store.profile_dir("profB").exists()


@pytest.mark.skipif(IS_WINDOWS, reason="symlinks require elevation on Windows")
def test_create_rejects_broken_symlink_at_source_snapshot(
    service: ProfileService, tmp_home: Path
) -> None:
    """Pre-fix: Path.exists() returned False for a broken symlink, so
    _seed_config_files silently skipped (treating it as "missing source").
    That turned a corrupt source profile into a child profile with no
    snapshot, and the next `use` warn-and-skipped instead of surfacing
    the corruption (Hermes pass-PR-2 specific repro)."""
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"a": {}}}))
    service.init(["claude"])
    service.save("profA")
    # create() seeds from the active source — switch to profA so the
    # corrupt snapshot is the seed source.
    service.use("profA")

    src_snap = service._store.config_file_snapshot_path("profA", "claude", "claude.json")
    src_snap.unlink()
    src_snap.symlink_to(tmp_home / ".does-not-exist.json")
    assert src_snap.is_symlink()
    assert not src_snap.exists()

    with pytest.raises(StorageError, match="symlink"):
        service.create("profB")
    assert not service._store.profile_dir("profB").exists()
