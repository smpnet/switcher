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
def service(tmp_home: Path, tmp_state: Path, registry: tuple[Tool, ...]) -> ProfileService:
    store = FileProfileStore(tmp_state)
    resolver = PathResolver(home=tmp_home)
    return ProfileService(store, resolver, registry)


def test_use_overlays_snapshot_onto_live(service: ProfileService, tmp_home: Path) -> None:
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


def test_use_rejects_broken_symlink_at_snapshot_path(
    service: ProfileService,
    tmp_home: Path,
) -> None:
    """A broken (dangling) symlink at the snapshot path must NOT be
    silently treated as "missing" — Path.exists() returns False for
    a broken symlink, but the apply path needs to refuse rather than
    fall into warn-and-skip. Without the refusal, ``use()`` would
    swap the config_dirs symlinks AND update ``active`` to the new
    profile, but leave ``~/.claude.json`` carrying the PREVIOUS
    profile's owned subtrees — a silent half-switched state that
    re-opens the MCP-leak class for users with snapshot-path
    corruption. The compensation classifier already treats any link
    shape at the snapshot path as AMBIGUOUS; the apply side mirrors
    that contract (Hermes + CR pass-PR-1).
    """
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"profA-source": {}}}))
    service.init(["claude"])
    service.save("profA")
    # profB carries the destination's MCP — would be the "live after
    # switch" content if use() didn't refuse.
    live.write_text(json.dumps({"mcpServers": {"profB-source": {}}}))
    service.save("profB")
    service.use("profB")  # live now reflects profB's snapshot

    # Capture the live content the post-use state would have left
    # behind if the symlink check didn't fire — proves the assertion
    # below is actually testing the refusal, not coincidence.
    live_before_switch = live.read_text()

    # Replace profA's snapshot with a dangling symlink. This is the
    # exact scenario in the Hermes reproducer: ``snap_path.exists()``
    # returns False, but the path is corrupt and must NOT degrade to
    # warn-and-skip.
    snap = service._store.config_file_snapshot_path("profA", "claude", "claude.json")
    snap.unlink()
    snap.symlink_to(tmp_home / ".does-not-exist.json")
    assert snap.is_symlink()
    assert not snap.exists()

    with pytest.raises(StorageError, match="symlink"):
        service.use("profA")

    # Pre-flight refusal means live is UNTOUCHED (matches the
    # plan-then-commit contract). If the refusal fires post-swap
    # instead, this would silently regress to the half-switched
    # state the Hermes finding describes.
    assert live.read_text() == live_before_switch
    # Active map must not have flipped — use() refused before
    # commit so the on-disk active still reports profB.
    assert service._store.get_active()["claude"] == "profB"


def test_use_synthesizes_live_when_missing(service: ProfileService, tmp_home: Path) -> None:
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


def test_use_raises_on_malformed_live(service: ProfileService, tmp_home: Path) -> None:
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({}))
    service.init(["claude"])
    service.save("profA")

    live.write_text("not json {")
    with pytest.raises(StorageError, match="malformed"):
        service.use("profA")


def test_use_raises_on_malformed_snapshot(service: ProfileService, tmp_home: Path) -> None:
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


def test_use_raises_when_live_is_not_a_json_object(service: ProfileService, tmp_home: Path) -> None:
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


def test_use_skips_capture_when_symlink_diverged_from_active(
    service: ProfileService, tmp_home: Path
) -> None:
    """abby r11: post-partial-commit drift must not corrupt the source
    profile's snapshot on retry.

    Scenario: a prior multi-tool use(profB) flushed claude's swap + write
    but raised before set_active_state. On-disk active still says profA;
    claude's symlink and live now hold profB content. Without divergence
    detection, the retry's capture-before-apply would read live (profB
    content) and overwrite profA's snapshot with it — silent cross-
    profile data corruption.

    The test manually constructs that post-partial-commit state (without
    needing a second tool with config_files, which would otherwise be
    needed to force the failure path) and asserts profA's snapshot is
    not touched by the retry.
    """
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"src": {}}}))
    service.init(["claude"])
    service.save("profA")

    live.write_text(json.dumps({"mcpServers": {"dst": {}}}))
    service.save("profB")

    # Restore live, switch onto profA so active = {claude: profA}.
    live.write_text(json.dumps({"mcpServers": {"src": {}}}))
    service.use("profA")

    snap = service._store.config_file_snapshot_path("profA", "claude", "claude.json")
    prof_a_before = json.loads(snap.read_text())

    # Simulate post-partial-commit drift: claude's symlink and live moved
    # to profB content, on-disk active still says profA (set_active_state
    # never fired in the imaginary prior failed use(profB)).
    claude_dir = tmp_home / ".claude"
    claude_dir.unlink()
    claude_dir.symlink_to(
        service._store.profile_dir("profB") / "claude",
        target_is_directory=True,
    )
    live.write_text(json.dumps({"mcpServers": {"dst": {}}}))

    # Retry. Capture for claude must detect the symlink/active divergence
    # and refuse to overwrite profA's snapshot.
    service.use("profB")

    prof_a_after = json.loads(snap.read_text())
    assert prof_a_after == prof_a_before
    # The owned content must still be "src", NOT "dst" (live at retry time).
    assert prof_a_after["mcpServers"] == {"src": {}}


def test_use_raises_storage_error_when_snapshot_path_is_a_directory(
    service: ProfileService, tmp_home: Path
) -> None:
    """abby r12 (snapshot side): if the snapshot path is a directory
    (external tampering — switcher itself would never put one there),
    surface as StorageError pre-flight rather than a raw IsADirectoryError
    from read_text. Pre-flight must also fire before swap_link."""
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {}}))
    service.init(["claude"])
    service.save("profA")

    snap = service._store.config_file_snapshot_path("profA", "claude", "claude.json")
    snap.unlink()
    snap.mkdir()

    claude_dir = tmp_home / ".claude"
    before = claude_dir.resolve()
    with pytest.raises(StorageError, match="directory"):
        service.use("profA")
    assert claude_dir.resolve() == before


def test_use_raises_storage_error_when_live_path_is_a_directory(
    service: ProfileService, tmp_home: Path
) -> None:
    """abby r12 (live side, via use()'s capture pre-flight)."""
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {}}))
    service.init(["claude"])
    service.save("profA")

    # Replace live with a directory between save() and use().
    live.unlink()
    live.mkdir()

    claude_dir = tmp_home / ".claude"
    before = claude_dir.resolve()
    with pytest.raises(StorageError, match="directory"):
        service.use("profA")
    assert claude_dir.resolve() == before


def test_use_raises_storage_error_on_non_utf8_snapshot(
    service: ProfileService, tmp_home: Path
) -> None:
    """A snapshot corrupted to non-UTF-8 bytes must surface as StorageError
    during pre-flight, not a raw UnicodeDecodeError (abby r10). Snapshots
    are switcher-written so we should only see this on external tampering;
    the service contract should still hold.
    """
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {}}))
    service.init(["claude"])
    service.save("profA")

    snap = service._store.config_file_snapshot_path("profA", "claude", "claude.json")
    snap.write_bytes(b"\xff\xfe\xfd")

    claude_dir = tmp_home / ".claude"
    before = claude_dir.resolve()
    with pytest.raises(StorageError, match="non-UTF-8"):
        service.use("profA")
    # Pre-flight raises before swap_link — symlink unchanged.
    assert claude_dir.resolve() == before


def test_use_raises_storage_error_when_snapshot_has_non_object_at_iter(
    service: ProfileService, tmp_home: Path
) -> None:
    """abby r9 (apply side): a snapshot shape like ``{"projects": []}``
    under an owned path ``.projects[].mcpServers`` makes the walker raise
    UnsupportedWalkTargetError. Service must re-raise as StorageError so the
    CLI's friendly error handling kicks in instead of a raw ValueError
    traceback, and it must fire pre-flight (no swap_link).
    """
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {}, "projects": {}}))
    service.init(["claude"])
    service.save("profA")

    # Corrupt profA's snapshot to have a non-object at an iter target.
    snap = service._store.config_file_snapshot_path("profA", "claude", "claude.json")
    snap.write_text(json.dumps({"projects": [], "mcpServers": {}}))

    # Track the symlink before to verify swap_link doesn't fire.
    claude_dir = tmp_home / ".claude"
    before = claude_dir.resolve()

    with pytest.raises(StorageError, match="non-object"):
        service.use("profA")
    assert claude_dir.resolve() == before


def test_use_capture_does_not_clobber_source_snapshot_on_broken_symlink(
    service: ProfileService, tmp_home: Path
) -> None:
    """abby r6 (capture side): a broken symlink at live_path must not be
    treated as "missing" during capture-before-apply. If it were, the
    source profile's snapshot would be overwritten with {}, destroying the
    user's last-good state.
    """
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"A": {"command": "x"}}}))
    service.init(["claude"])
    service.save("profA")
    service.save("profB")
    service.use("profA")  # active is now profA; profA snapshot has the data

    snap_path = service._store.config_file_snapshot_path("profA", "claude", "claude.json")
    snapshot_before = json.loads(snap_path.read_text())
    assert snapshot_before == {"mcpServers": {"A": {"command": "x"}}}

    # Replace live with a broken symlink, then try to switch profiles.
    # Capture-before-apply would have read live; pre-r6, exists() == False
    # would have made capture silently write {} into profA's snapshot.
    live.unlink()
    live.symlink_to(tmp_home / ".does-not-exist.json")

    with pytest.raises(StorageError, match="symlink"):
        service.use("profB")

    # profA's snapshot must not have been touched — the original owned
    # data is still there for a subsequent recovery.
    assert json.loads(snap_path.read_text()) == snapshot_before


def test_use_rejects_symlink_at_live_path(service: ProfileService, tmp_home: Path) -> None:
    """A symlink at the ConfigFile live path is fatal during pre-flight.

    atomic_write_file refuses to atomic-rename through a symlink (it would
    silently replace the link with a regular file). If pre-flight didn't
    catch this, swap_link would fire first and then the commit would fail
    with the tool half-switched. abby r5 specifically called out the broken-
    symlink case: ``live_path.exists()`` returns False for a broken
    symlink, so the planner thinks live is missing and synthesizes a write
    that atomic_write_file then rejects post-swap.
    """
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"A": {}}}))
    service.init(["claude"])
    service.save("profA")

    # Capture the config_dirs symlink target before triggering the failure.
    claude_dir = tmp_home / ".claude"
    before = claude_dir.resolve()

    # Replace the live file with a broken symlink (target doesn't exist).
    # exists() returns False here, so the planner pre-r5 would synthesize a
    # write and only fail at commit time, post-swap.
    live.unlink()
    live.symlink_to(tmp_home / ".does-not-exist.json")
    assert live.is_symlink()
    assert not live.exists()

    with pytest.raises(StorageError, match="symlink"):
        service.use("profA")

    # The pre-flight failure must have fired before swap_link.
    assert claude_dir.resolve() == before


def test_use_rolls_back_swap_link_when_write_fails(
    service: ProfileService,
    tmp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If atomic_write_file raises after swap_link succeeds, the swap must
    be rolled back so the tool isn't left with config_dirs at the new
    profile and ConfigFile state at the old. Covers the residual runtime
    failure modes pre-flight can't predict (disk full, permissions, etc.).
    """
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"A": {}}}))
    service.init(["claude"])
    service.save("profA")
    service.save("profB")
    service.use("profA")  # active is now profA; profA's config_dir is live

    claude_dir = tmp_home / ".claude"
    prof_a_subdir = service._store.profile_dir("profA") / "claude"
    prof_b_subdir = service._store.profile_dir("profB") / "claude"
    assert claude_dir.resolve() == prof_a_subdir.resolve()

    # Patch atomic_write_file to fail at commit time. swap_link will have
    # already run by then — rollback should swap claude_dir back to profA.
    from switcher import service as service_mod

    call_count = {"n": 0}

    def real_atomic_write_file(target: Path, content: bytes) -> None:
        from switcher import links as links_mod

        links_mod.atomic_write_file(target, content)

    def flaky(target: Path, content: bytes) -> None:
        call_count["n"] += 1
        # Capture phase writes to source profile's snapshot dir, NOT to
        # ~/.claude.json. The post-swap_link apply write is the one we want
        # to break. Use the target path to discriminate.
        if target == live:
            raise OSError("simulated commit-time write failure")
        real_atomic_write_file(target, content)

    monkeypatch.setattr(service_mod, "atomic_write_file", flaky)

    with pytest.raises(OSError, match="simulated commit-time write failure"):
        service.use("profB")

    # config_dirs symlink must be rolled back to profA (the source).
    assert claude_dir.resolve() == prof_a_subdir.resolve()
    # And NOT pointing at profB (the destination of the failed switch).
    assert claude_dir.resolve() != prof_b_subdir.resolve()


def test_use_aborts_before_swap_link_when_snapshot_malformed(
    service: ProfileService, tmp_home: Path
) -> None:
    """Plan-then-mutate ordering: a malformed snapshot raises during the
    pre-flight plan phase, before any swap_link or live write. The dir
    symlink must remain pointing at the source profile (addresses abby r4
    finding 1: parse failures used to half-apply the switch).
    """
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"A": {}}}))
    service.init(["claude"])
    service.save("profA")
    service.save("profB")

    # use profA first to establish a clean baseline pointing at profA.
    service.use("profA")
    claude_dir = tmp_home / ".claude"
    prof_a_subdir = service._store.profile_dir("profA") / "claude"
    assert claude_dir.resolve() == prof_a_subdir.resolve()

    # Corrupt profB's snapshot, then attempt to switch to it.
    snap_b = service._store.config_file_snapshot_path("profB", "claude", "claude.json")
    snap_b.write_text("not json {")

    with pytest.raises(StorageError, match="malformed"):
        service.use("profB")

    # swap_link must NOT have fired — symlink still points at profA's subdir.
    assert claude_dir.resolve() == prof_a_subdir.resolve()
