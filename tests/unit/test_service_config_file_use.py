# pyright: reportPrivateUsage=none
"""Tests for ConfigFile handling in ProfileService.use().

The fixtures (`tmp_home`, `tmp_state`) come from tests/conftest.py. The local
`registry`/`service` fixtures add a ConfigFile-equipped claude tool. The save
side is already exercised by test_service_config_file_save.py — these tests
focus on the apply-on-switch direction.

Warning capture uses ``capsys`` rather than ``caplog`` because the codebase
emits warnings via ``print(..., file=sys.stderr)`` (see ``_warn_migration``),
not the stdlib logging module.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from switcher.errors import StorageError
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


def test_use_overlays_snapshot_onto_live(
    service: ProfileService, tmp_home: Path
) -> None:
    live = tmp_home / ".claude.json"
    # Profile A's live state
    live.write_text(
        json.dumps(
            {
                "mcpServers": {"A-server": {}},
                "projects": {"/repo": {"mcpServers": {"A-proj": {}}}},
                "oauthAccount": {"email": "a@x"},
                "hasCompletedOnboarding": True,
            }
        )
    )
    service.init(["claude"])
    service.save("profA")

    # Simulate switching: change live to profile B's state, then save B
    live.write_text(
        json.dumps(
            {
                "mcpServers": {"B-server": {}},
                "oauthAccount": {"email": "b@x"},
                "hasCompletedOnboarding": True,
            }
        )
    )
    service.save("profB")

    # Switch back to profA — live owned paths should restore profA's snapshot;
    # machine-global keys (hasCompletedOnboarding) preserved.
    service.use("profA")
    live_after = json.loads(live.read_text())
    assert live_after["mcpServers"] == {"A-server": {}}
    assert live_after["oauthAccount"] == {"email": "a@x"}
    assert live_after["projects"] == {"/repo": {"mcpServers": {"A-proj": {}}}}
    assert live_after["hasCompletedOnboarding"] is True


def test_use_deletes_live_keys_absent_from_snapshot(
    service: ProfileService, tmp_home: Path
) -> None:
    """The MCP-leak fix: an MCP added under profile A must not survive
    switching to profile B (whose snapshot has no entry for it)."""
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({}))
    service.init(["claude"])
    service.save("profA")
    service.save("profB")  # both profiles have empty owned paths

    # Simulate the user adding an MCP while profA is active
    live.write_text(json.dumps({"mcpServers": {"leaked": {"command": "x"}}}))

    # Switch to B → leaked MCP must be removed
    service.use("profB")
    live_after = json.loads(live.read_text())
    assert "mcpServers" not in live_after


def test_use_warns_and_skips_when_snapshot_missing(
    service: ProfileService,
    tmp_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An old profile pre-dating ConfigFile support has no snapshot. Apply
    must warn + skip, not silently wipe live to {} (the destructive default
    the spec explicitly rejects)."""
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"unchanged": {}}}))
    service.init(["claude"])
    service.save("profA")

    snap = service._store.config_file_snapshot_path("profA", "claude", "claude.json")
    snap.unlink()
    # Drain stderr from preceding init/save calls so the assertion below
    # only sees the warning emitted by use().
    capsys.readouterr()

    service.use("profA")
    live_after = json.loads(live.read_text())
    assert live_after == {"mcpServers": {"unchanged": {}}}
    err = capsys.readouterr().err
    assert "snapshot" in err.lower()
    assert "claude" in err
    # Warning must not prescribe an unimplemented remediation. Init/rescan
    # integration ships in a later PR; until then, telling users to run
    # `switcher rescan` here would be misleading.
    assert "rescan" not in err.lower()


def test_use_synthesizes_live_when_missing(
    service: ProfileService, tmp_home: Path
) -> None:
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {}}))
    service.init(["claude"])
    service.save("profA")

    # Delete live before switching → use() must recreate it from the snapshot.
    live.unlink()
    service.use("profA")
    assert live.exists()
    out = json.loads(live.read_text())
    assert "mcpServers" in out


def test_use_raises_on_malformed_live(
    service: ProfileService, tmp_home: Path
) -> None:
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({}))
    service.init(["claude"])
    service.save("profA")

    live.write_text("not json {")
    with pytest.raises(StorageError, match="malformed"):
        service.use("profA")


def test_use_raises_on_malformed_snapshot(
    service: ProfileService, tmp_home: Path
) -> None:
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({}))
    service.init(["claude"])
    service.save("profA")

    snap = service._store.config_file_snapshot_path("profA", "claude", "claude.json")
    snap.write_text("not json {")
    with pytest.raises(StorageError, match="malformed"):
        service.use("profA")


def test_use_raises_when_snapshot_is_not_a_json_object(
    service: ProfileService, tmp_home: Path
) -> None:
    """A snapshot that survived storage tampering and is now a JSON array or
    scalar isn't a valid Claude config; refuse to apply."""
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({}))
    service.init(["claude"])
    service.save("profA")

    snap = service._store.config_file_snapshot_path("profA", "claude", "claude.json")
    snap.write_text(json.dumps(["array", "not", "object"]))
    with pytest.raises(StorageError, match="object"):
        service.use("profA")


def test_use_raises_when_live_is_not_a_json_object(
    service: ProfileService, tmp_home: Path
) -> None:
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({}))
    service.init(["claude"])
    service.save("profA")

    live.write_text(json.dumps(["array", "not", "object"]))
    with pytest.raises(StorageError, match="object"):
        service.use("profA")


def test_use_captures_active_profile_live_before_apply(
    service: ProfileService, tmp_home: Path
) -> None:
    """A user edit to live while profA is active must survive a round-trip
    through use(profB) → use(profA).

    Without capture-before-apply, ``use(profB)`` overwrites live with profB's
    snapshot and the edit is lost. With it, the edit is first captured into
    profA's snapshot, then the switch proceeds.
    """
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"A": {}}}))
    service.init(["claude"])
    service.save("profA")

    # Manually rewrite live, then save profB.
    live.write_text(json.dumps({"mcpServers": {"B": {}}}))
    service.save("profB")

    # Switch onto profA so the active map points at profA. After this,
    # capture-before-apply will treat profA as the source on the next switch.
    service.use("profA")
    assert json.loads(live.read_text())["mcpServers"] == {"A": {}}

    # User edit while profA is active.
    live_data = json.loads(live.read_text())
    live_data["mcpServers"]["user-added"] = {"command": "z"}
    live.write_text(json.dumps(live_data))

    # Switch away; the edit must be captured into profA's snapshot, not lost.
    service.use("profB")
    assert json.loads(live.read_text())["mcpServers"] == {"B": {}}

    # Switch back; the user-added MCP must reappear.
    service.use("profA")
    out = json.loads(live.read_text())
    assert out["mcpServers"] == {"A": {}, "user-added": {"command": "z"}}


def test_use_captures_live_when_switching_to_already_active_profile(
    service: ProfileService, tmp_home: Path
) -> None:
    """`switcher use <active>` is a legitimate reload-from-snapshot affordance,
    but must not silently discard in-flight live edits.

    Without capture-on-self-use the apply would overwrite live with the
    last-saved snapshot — data loss. With it, the call captures-then-no-ops
    on live (the freshly-captured snapshot is what gets applied back).
    """
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"A": {}}}))
    service.init(["claude"])
    service.save("profA")
    service.use("profA")  # active is now profA

    # User edit while profA is active, without an explicit save.
    live_data = json.loads(live.read_text())
    live_data["mcpServers"]["user-added"] = {"command": "z"}
    live.write_text(json.dumps(live_data))

    # Re-using the same profile must preserve the in-flight edit.
    service.use("profA")
    out = json.loads(live.read_text())
    assert out["mcpServers"] == {"A": {}, "user-added": {"command": "z"}}

    # The capture must also have updated profA's snapshot so a subsequent
    # switch-away-and-back round-trip works.
    snap = service._store.config_file_snapshot_path("profA", "claude", "claude.json")
    snap_data = json.loads(snap.read_text())
    assert snap_data["mcpServers"] == {"A": {}, "user-added": {"command": "z"}}


def test_use_parse_phase_failure_preserves_all_live_files(
    tmp_home: Path, tmp_state: Path
) -> None:
    """A parse-time failure on cf2 must not leave cf1's live half-overwritten.

    This test verifies the **plan/parse phase** atomicity contract only —
    every ConfigFile parses to a planned write before any write executes,
    so a malformed cf2 snapshot raises before any commit. **Commit-phase**
    failures (write #2 fails after write #1 has been swapped into place) are
    intentionally out of scope; see ``_apply_config_files`` docstring for
    the op-log compensation path (plan Task 11).
    """
    cf1 = ConfigFile(
        posix_path="~/.claude.json",
        windows_path="%USERPROFILE%\\.claude.json",
        profile_subdir="claude",
        profile_filename="claude.json",
        merge_strategy="json_subtree_merge",
        owned_json_paths=(".mcpServers",),
    )
    cf2 = ConfigFile(
        posix_path="~/.claude-extra.json",
        windows_path="%USERPROFILE%\\.claude-extra.json",
        profile_subdir="claude",
        profile_filename="claude-extra.json",
        merge_strategy="json_subtree_merge",
        owned_json_paths=(".mcpServers",),
    )
    base = build_registry(Path("/nonexistent"))
    enhanced: list[Tool] = []
    for t in base:
        if t.id == "claude":
            enhanced.append(t.model_copy(update={"config_files": (cf1, cf2)}))
        else:
            enhanced.append(t)
    store = FileProfileStore(tmp_state)
    resolver = PathResolver(home=tmp_home)
    service = ProfileService(store, resolver, tuple(enhanced))

    live1 = tmp_home / ".claude.json"
    live2 = tmp_home / ".claude-extra.json"
    live1.write_text(json.dumps({"mcpServers": {"profA-1": {}}}))
    live2.write_text(json.dumps({"mcpServers": {"profA-2": {}}}))
    service.init(["claude"])
    service.save("profA")

    # Replace live1 with a sentinel that the test will look for. If cf2's
    # plan failure ever lets cf1's write through, this sentinel is gone.
    sentinel = {"mcpServers": {"unchanged": {"command": "preserved"}}}
    live1.write_text(json.dumps(sentinel))

    # Corrupt profA's cf2 snapshot.
    snap2 = service._store.config_file_snapshot_path(
        "profA", "claude", "claude-extra.json"
    )
    snap2.write_text("not json {")

    with pytest.raises(StorageError, match="malformed"):
        service.use("profA")

    # cf1's live must be untouched — plan phase aborts before any commit.
    assert json.loads(live1.read_text()) == sentinel
