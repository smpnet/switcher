# pyright: reportPrivateUsage=none
"""Tests for ConfigFile handling in ProfileService.rescan() (spec §3.8).

Mirrors the local-registry pattern from test_service_config_file_save.py:
the built-in claude tool is augmented with a ConfigFile-equipped variant
so rescan can be exercised against a tool that carries the spec's
reference shape from §3.3 / §3.5.

These tests focus on the fresh-mode capture path, the --into-mode
snapshot collision refusal, and the rollback behavior when post-snapshot
work fails. Op-log compensation for config_file_mappings is exercised
separately in test_init_config_file_compensation.py /
test_rescan_config_file_compensation.py once Task 11c lands.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from switcher.errors import RescanCaptureError
from switcher.models import ConfigFile, Tool
from switcher.paths import PathResolver
from switcher.registry import build_registry
from switcher.service import ProfileService
from switcher.store import FileProfileStore


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
def registry() -> tuple[Tool, ...]:
    """Built-in registry with claude augmented to carry the spec's ConfigFile."""
    base = build_registry(Path("/nonexistent"))
    out: list[Tool] = []
    for t in base:
        if t.id == "claude":
            out.append(t.model_copy(update={"config_files": (CLAUDE_CONFIG_FILE,)}))
        else:
            out.append(t)
    return tuple(out)


@pytest.fixture
def service(
    tmp_home: Path, tmp_state: Path, registry: tuple[Tool, ...]
) -> ProfileService:
    store = FileProfileStore(tmp_state)
    resolver = PathResolver(home=tmp_home)
    return ProfileService(store, resolver, registry)


def _freeze_now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Pin service.now() so the rescan profile name is deterministic.

    Without this, two same-second test runs racing UTC midnight could pick
    different "{date}-rescan-1" names than the assertions expect.
    """
    from switcher import service as svc_mod

    frozen = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(svc_mod, "now", lambda: frozen)
    return frozen


def _suppress_claude(tmp_home: Path) -> None:
    """Remove ~/.claude so init() doesn't manage claude.

    Mirrors the integration-test _suppress_copilot helper. Lets tests
    treat claude as a rescan target (must be absent from active at
    rescan time) without rewriting the conftest fixture.
    """
    shutil.rmtree(tmp_home / ".claude", ignore_errors=True)


def test_rescan_fresh_captures_config_file_into_new_profile(
    service: ProfileService,
    tmp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh-mode rescan must capture the ConfigFile alongside the dir.

    Without this, the first ``use`` onto the rescan profile would hit
    snapshot-missing and silently apply ``{}`` over the user's MCPs.
    """
    _freeze_now(monkeypatch)
    _suppress_claude(tmp_home)
    service.init()  # captures copilot only

    # "Install" claude post-init: recreate ~/.claude AND drop ~/.claude.json.
    (tmp_home / ".claude").mkdir()
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"x": {"command": "y"}}}))

    service.rescan(only=["claude"])

    active = service._store.get_active()
    profile_name = active["claude"]
    snap = service._store.config_file_snapshot_path(
        profile_name, "claude", "claude.json"
    )
    assert snap.exists()
    assert json.loads(snap.read_text()) == {"mcpServers": {"x": {"command": "y"}}}


def test_rescan_into_refuses_when_snapshot_already_exists(
    service: ProfileService, tmp_home: Path
) -> None:
    """A pre-existing snapshot at the target path must NOT be silently
    overwritten — would destroy the previously-captured MCP/account state.
    Parallels the existing dir-collision check in service.py:3893-3901;
    the new check fires on the snapshot path, not the dir path.

    Setup mimics a profile saved by an earlier switcher whose claude
    config_dir subdir was manually rmdir'd by the user — leaves the
    snapshot orphaned in the profile without a colliding dir, so the
    NEW snapshot-collision check is what fires (rather than the
    pre-existing dir-collision check, which would mask it).
    """
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"original": {}}}))
    service.init(["claude"])
    # Capture a snapshot into "target_profile" — this is what makes the
    # collision check fire when rescan(--into=target_profile) runs later.
    service.save("target_profile")
    # Drop claude from the active map and restore its live dir, so rescan
    # treats claude as a fresh discovery again.
    service.unmanage("claude")
    # Isolate the new collision check: remove target_profile's claude
    # config_dir subdir so the existing dir-collision check does NOT fire
    # first. The snapshot under .switcher/config_files/... is unaffected.
    target_dir = service._store.profile_dir("target_profile")
    shutil.rmtree(target_dir / "claude")
    snap = service._store.config_file_snapshot_path(
        "target_profile", "claude", "claude.json"
    )
    assert snap.exists(), "test precondition: snapshot must still be present"

    with pytest.raises(RescanCaptureError, match="snapshot"):
        service.rescan(into="target_profile", only=["claude"])


def test_rescan_rollback_removes_partial_snapshot(
    service: ProfileService,
    tmp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If rescan fails after writing a config_file snapshot but before
    completing, the snapshot must be cleaned up (fresh-mode rmtree of the
    partial profile dir covers .switcher/config_files transitively).

    Mirrors test_rescan_state_write_failure_rolls_back_capture from
    tests/integration/test_rescan.py for the ConfigFile case.
    """
    _freeze_now(monkeypatch)
    _suppress_claude(tmp_home)
    service.init()  # captures copilot only

    (tmp_home / ".claude").mkdir()
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"x": {}}}))

    # Force set_active_state — the LAST step inside the rescan per-tool
    # try block — to fail. By the time this fires, the dir capture AND
    # the new ConfigFile snapshot are both written; the outer except
    # must reach back through _rollback_partial_rescan to clean both up.
    #
    # The spy records whether the snapshot was on disk at the moment
    # of failure, so the test asserts both halves of the rollback
    # contract: (a) the new ConfigFile capture step ran BEFORE the
    # failure (snapshot present), and (b) the rollback removed it
    # (snapshot absent after).
    from switcher import store as store_mod

    snapshot_present_at_failure: dict[str, bool] = {"yes": False}

    def boom(self: FileProfileStore, *args: object, **kwargs: object) -> None:
        profiles_dir = self.state_dir() / "profiles"
        for p in profiles_dir.iterdir():
            if not p.is_dir():
                continue
            if (p / ".switcher" / "config_files" / "claude" / "claude.json").exists():
                snapshot_present_at_failure["yes"] = True
                break
        raise OSError("simulated post-snapshot failure")

    monkeypatch.setattr(store_mod.FileProfileStore, "set_active_state", boom)

    # The outer rescan() wraps the OSError in RescanCaptureError(... from e).
    with pytest.raises(RescanCaptureError):
        service.rescan(only=["claude"])

    assert snapshot_present_at_failure["yes"], (
        "_capture_tool_for_rescan must write the ConfigFile snapshot BEFORE "
        "set_active_state runs; if this assertion fails, the rescan flow "
        "never reached the new ConfigFile capture step"
    )

    # Profile dir is rolled back, taking the snapshot with it.
    profiles_dir = service._store.state_dir() / "profiles"
    snapshot_anywhere = any(
        (p / ".switcher" / "config_files" / "claude" / "claude.json").exists()
        for p in profiles_dir.iterdir()
        if p.is_dir()
    )
    assert not snapshot_anywhere, (
        "rollback failed to remove the ConfigFile snapshot from the partial "
        "rescan profile"
    )
