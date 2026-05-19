"""End-to-end test of ~/.claude.json profile isolation (spec §3.1, §3.2).

Exercises the full lifecycle: init → save → install MCP under profA →
switch to profB → verify the leak is closed → switch back → verify
state restored. Uses the production builtin claude registry — Task 12
shipped the [[config_files]] block — so this test reads against the
real shape users will see.

The integration suite already covers each lifecycle step in isolation;
this file is the cross-step proof that closing the MCP-leak class
actually works, including the per-project subtree and the machine-
global preservation (lastSessionId, hasCompletedOnboarding) that the
spec §3.2 separation depends on.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from switcher.paths import PathResolver
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


def _suppress_copilot(tmp_home: Path) -> None:
    """Remove copilot's live dirs so init manages claude only.

    Cross-platform: the POSIX paths are no-ops on Windows and vice
    versa. Same shape as test_rescan.py's helper.
    """
    for sub in [".copilot", ".config/github-copilot", "AppData/Local/github-copilot"]:
        target = tmp_home / sub
        if target.is_symlink() or target.exists():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()


def test_mcp_isolation_across_profile_switch(
    tmp_state: Path, tmp_home: Path
) -> None:
    """Spec §1 motivating case: an MCP installed under profA must
    NOT appear in live state when profB is active. The pre-isolation
    leak path was Claude reading from a single ``~/.claude.json``;
    Task 12 closes it by routing the owned subtrees through the
    profile store on every switch.

    ``save(name)`` creates a NEW snapshot under ``name`` — there is no
    overwrite verb, so the test captures the two profiles' states as
    distinct save calls separated by a live edit. ``profB`` is saved
    while live has no MCP; the user then "installs" the MCP and saves
    it as ``profA``. Switching between the two profiles must flip the
    MCP in and out of the owned subtree.
    """
    _suppress_copilot(tmp_home)
    live = tmp_home / ".claude.json"
    # Pre-install machine-global state to verify it rides across the
    # switch (spec §3.2: walker owns only mcpServers / oauthAccount /
    # projects[].mcpServers — everything else is live-machine-global).
    live.write_text(
        json.dumps({"mcpServers": {}, "hasCompletedOnboarding": True})
    )

    s = _service(tmp_state, tmp_home)
    s.init(["claude"])
    # profB: no MCP, just the baseline state.
    s.save("profB")

    # "Install" the MCP and capture it into profA.
    live.write_text(
        json.dumps(
            {
                "mcpServers": {"installed-under-A": {"command": "x"}},
                "oauthAccount": {"email": "a@example.com"},
                "hasCompletedOnboarding": True,
            }
        )
    )
    s.save("profA")

    # Switch to profB — the MCP must NOT bleed across. profB's
    # snapshot has an EXPLICIT ``mcpServers: {}`` (the user wrote
    # that shape into live before saving profB), so the walker
    # writes that exact value back onto live. ``mcpServers`` is
    # therefore present-but-empty after the switch, not absent —
    # different from the vanilla test where the snapshot has no
    # mcpServers key at all and delete-on-absence removes it.
    # The invariant under test is "the MCP I installed in profA
    # is not visible", not "mcpServers is absent."
    s.use("profB")
    live_after_switch = json.loads(live.read_text())
    assert "installed-under-A" not in live_after_switch.get("mcpServers", {})
    # Machine-global keys: preserved across the switch. The walker
    # only owns the three listed paths; everything else rides on the
    # live file unchanged.
    assert live_after_switch.get("hasCompletedOnboarding") is True

    # Switch to profA — MCP + oauth come back from the snapshot.
    s.use("profA")
    live_back = json.loads(live.read_text())
    assert live_back["mcpServers"] == {"installed-under-A": {"command": "x"}}
    assert live_back["oauthAccount"] == {"email": "a@example.com"}


def test_oauth_isolation_across_profile_switch(
    tmp_state: Path, tmp_home: Path
) -> None:
    """``.oauthAccount`` is the second owned path; switching between
    profiles with different accounts must flip the live value."""
    _suppress_copilot(tmp_home)
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({}))

    s = _service(tmp_state, tmp_home)
    s.init(["claude"])

    # Profile A: account A.
    live.write_text(json.dumps({"oauthAccount": {"email": "a@x"}}))
    s.save("profA")

    # Profile B: account B.
    live.write_text(json.dumps({"oauthAccount": {"email": "b@x"}}))
    s.save("profB")

    s.use("profA")
    assert json.loads(live.read_text())["oauthAccount"]["email"] == "a@x"
    s.use("profB")
    assert json.loads(live.read_text())["oauthAccount"]["email"] == "b@x"


def test_per_project_mcp_isolation(tmp_state: Path, tmp_home: Path) -> None:
    """``.projects[].mcpServers`` is the walker's iter form. A per-
    project MCP under profA must not bleed to profB, while per-project
    machine-global keys (lastSessionId) ride the live file."""
    _suppress_copilot(tmp_home)
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({}))

    s = _service(tmp_state, tmp_home)
    s.init(["claude"])

    # Profile A: project /repo has an MCP plus a machine-global session id.
    live.write_text(
        json.dumps(
            {
                "projects": {
                    "/repo": {
                        "mcpServers": {"proj-A": {}},
                        "lastSessionId": "A-session",
                    }
                }
            }
        )
    )
    s.save("profA")

    # Profile B: no per-project MCP, but its own session id in live.
    live.write_text(
        json.dumps({"projects": {"/repo": {"lastSessionId": "B-session"}}})
    )
    s.save("profB")

    # Switch to A → MCP restored; lastSessionId from CURRENT live, not
    # from the snapshot (spec §3.2: only the three owned paths roundtrip).
    s.use("profA")
    out = json.loads(live.read_text())
    assert out["projects"]["/repo"]["mcpServers"] == {"proj-A": {}}
    assert out["projects"]["/repo"]["lastSessionId"] == "B-session"

    # Switch to B → no MCP under /repo, lastSessionId still from live.
    s.use("profB")
    out = json.loads(live.read_text())
    assert "mcpServers" not in out["projects"]["/repo"]
    assert out["projects"]["/repo"]["lastSessionId"] == "B-session"


def test_full_lifecycle_init_save_create_use_unmanage_rescan(
    tmp_state: Path, tmp_home: Path
) -> None:
    """Covers every ConfigFile-touching lifecycle step in one flow:

    - init captures the snapshot under the initial profile.
    - save captures another snapshot under a named profile.
    - create copies the snapshot from the active source.
    - use applies the snapshot back over the owned subtrees.
    - unmanage drops the tool from active without touching live or
      per-profile snapshots (spec §3.6 ConfigFile invariant).
    - rescan picks the tool back up after unmanage and captures into
      a fresh profile.

    Uninstall isn't exercised here — its per-profile snapshot
    preservation is verified in test_service_config_file_unmanage_uninstall.py
    against a controlled fixture (this test focuses on cross-step
    flow, not uninstall's snapshot-preservation contract).
    """
    _suppress_copilot(tmp_home)
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"initial": {}}}))

    s = _service(tmp_state, tmp_home)
    s.init(["claude"])
    init_profile = s._store.get_active()["claude"]  # pyright: ignore[reportPrivateUsage]
    init_snap = s._store.config_file_snapshot_path(  # pyright: ignore[reportPrivateUsage]
        init_profile, "claude", "claude.json"
    )
    assert init_snap.exists()
    assert json.loads(init_snap.read_text()) == {"mcpServers": {"initial": {}}}

    # save → captures current live into a named profile.
    live.write_text(json.dumps({"mcpServers": {"saved": {}}}))
    s.save("named")
    named_snap = s._store.config_file_snapshot_path(  # pyright: ignore[reportPrivateUsage]
        "named", "claude", "claude.json"
    )
    assert json.loads(named_snap.read_text()) == {"mcpServers": {"saved": {}}}

    # create → snapshot copied from the active source (init_profile).
    s.create("copied")
    copied_snap = s._store.config_file_snapshot_path(  # pyright: ignore[reportPrivateUsage]
        "copied", "claude", "claude.json"
    )
    assert json.loads(copied_snap.read_text()) == {"mcpServers": {"initial": {}}}

    # use("named") → live gets the saved subtree back.
    s.use("named")
    assert json.loads(live.read_text())["mcpServers"] == {"saved": {}}

    # unmanage → tool dropped from active map; live restored; snapshots
    # preserved across profiles.
    s.unmanage("claude")
    assert "claude" not in s._store.get_active()  # pyright: ignore[reportPrivateUsage]
    # Live file untouched per spec §3.6 (ConfigFile invariant on unmanage).
    assert json.loads(live.read_text())["mcpServers"] == {"saved": {}}

    # rescan picks claude up again and captures the current live into
    # a fresh rescan-named profile.
    s.rescan(only=["claude"])
    rescan_profile = s._store.get_active()["claude"]  # pyright: ignore[reportPrivateUsage]
    rescan_snap = s._store.config_file_snapshot_path(  # pyright: ignore[reportPrivateUsage]
        rescan_profile, "claude", "claude.json"
    )
    assert rescan_snap.exists()
    assert json.loads(rescan_snap.read_text()) == {"mcpServers": {"saved": {}}}


def test_use_vanilla_clears_owned_subtrees(
    tmp_state: Path, tmp_home: Path
) -> None:
    """``vanilla`` represents factory-fresh tool state — switching onto
    it must wipe the owned subtrees back to empty. Without an explicit
    ``{}`` snapshot at init time, ``use("vanilla")`` falls into the
    "snapshot missing → warn-and-skip" branch and leaves the user's
    MCPs in live, silently defeating the isolation contract for the
    vanilla profile (CR r-batch4 major).
    """
    _suppress_copilot(tmp_home)
    live = tmp_home / ".claude.json"
    live.write_text(
        json.dumps(
            {
                "mcpServers": {"installed": {"command": "x"}},
                "oauthAccount": {"email": "u@x"},
                "hasCompletedOnboarding": True,
            }
        )
    )

    s = _service(tmp_state, tmp_home)
    s.init(["claude"])

    # Switch to vanilla. Spec §3.2: machine-global keys
    # (hasCompletedOnboarding) ride the live file; the three owned
    # subtrees collapse to the vanilla snapshot ({}).
    s.use("vanilla")
    out = json.loads(live.read_text())
    # Strict "key absent" form: the walker's delete-on-absence
    # semantics (json_paths.apply_owned_paths) removes owned keys
    # whose snapshot value is missing. An empty {} would mean the
    # walker stopped at the parent and set a literal empty dict —
    # not the contract.
    assert "mcpServers" not in out
    assert "oauthAccount" not in out
    assert out.get("hasCompletedOnboarding") is True
