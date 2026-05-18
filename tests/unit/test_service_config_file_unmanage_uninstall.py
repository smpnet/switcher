# pyright: reportPrivateUsage=none
"""Tests for ConfigFile behavior across unmanage and uninstall.

unmanage and uninstall non-purge must leave live ~/.claude.json AND the
per-profile snapshot files alone. The dir-symlink equivalent
(``restore_real_dir``) writes data back because the symlink stops
resolving on teardown; for ConfigFile there is no symlink, and writing
the snapshot back would destroy any drift Claude produced since the
last switch.

Per spec §3.6, uninstall non-purge also preserves per-profile snapshots
(symmetric with dir-snapshot preservation; the asymmetric alternative is
silent data loss). Purge mode's existing ``shutil.rmtree(state_dir)``
carries snapshots away with everything else.

These tests are largely a "verify the no-op" contract — no production
code paths need to add anything. If a test here fails, production is
unexpectedly touching live or snapshots — investigate rather than guard.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def test_unmanage_does_not_touch_live_config_file(
    service: ProfileService, tmp_home: Path
) -> None:
    live = tmp_home / ".claude.json"
    live.write_text(
        json.dumps({"mcpServers": {"a": {}}, "hasCompletedOnboarding": True})
    )
    service.init(["claude"])
    before = live.read_text()
    service.unmanage("claude")
    after = live.read_text()
    assert before == after


def test_unmanage_does_not_delete_snapshot_files(
    service: ProfileService, tmp_home: Path
) -> None:
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"a": {}}}))
    service.init(["claude"])
    active_source = service._store.get_active()["claude"]
    snap = service._store.config_file_snapshot_path(
        active_source, "claude", "claude.json"
    )
    assert snap.exists()
    service.unmanage("claude")
    assert snap.exists()  # snapshot preserved


def test_uninstall_non_purge_does_not_touch_live_config_file(
    service: ProfileService, tmp_home: Path
) -> None:
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"a": {}}}))
    service.init(["claude"])
    before = live.read_text()
    service.uninstall()
    after = live.read_text()
    assert before == after


def test_uninstall_non_purge_preserves_snapshot_files(
    service: ProfileService, tmp_home: Path
) -> None:
    live = tmp_home / ".claude.json"
    live.write_text(json.dumps({"mcpServers": {"a": {}}}))
    service.init(["claude"])
    active_source = service._store.get_active()["claude"]
    snap = service._store.config_file_snapshot_path(
        active_source, "claude", "claude.json"
    )
    snap_before = snap.read_text()
    service.uninstall()
    assert snap.exists()
    assert snap.read_text() == snap_before
