# pyright: reportPrivateUsage=none
"""Sentinel: every builtin TOML's profile_subdir + live-path basename is
represented in the historical-metadata tables in `switcher.service`.

Why: the v0.1.4 FS-truth migration (spec §4) reads
`_HISTORICAL_PROFILE_SUBDIRS` and `_HISTORICAL_LIVE_PATH_BASENAMES` as
the source of truth for "what subdirs / basenames a tool's data may
legitimately occupy on disk across switcher versions." A future commit
that rewrites a builtin TOML to use a new path or subdir name without
also updating the historical tables would silently break migration for
upgrading users — the new path/subdir wouldn't be recognized as
candidate profile content, so legacy data would be orphaned.

This test enforces the contributor invariant: changing a builtin TOML's
config_dirs requires also updating the historical tables. Adding a new
builtin tool requires adding entries (or an explicit empty frozenset for
tools with no historical drift, like claude).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from switcher.models import Tool
from switcher.registry import build_registry
from switcher.service import (
    _HISTORICAL_LIVE_PATH_BASENAMES,
    _HISTORICAL_PROFILE_SUBDIRS,
)


@pytest.fixture
def builtins_only_registry(tmp_path: Path) -> Sequence[Tool]:
    """Registry built from the bundled builtins (no user TOMLs)."""
    empty_registry_d = tmp_path / "registry.d"
    empty_registry_d.mkdir()
    return build_registry(empty_registry_d)


def test_every_builtin_profile_subdir_is_in_historical_table(
    builtins_only_registry: Sequence[Tool],
) -> None:
    """For each builtin tool, every profile_subdir must be in
    _HISTORICAL_PROFILE_SUBDIRS[tool.id] (which is the union of
    historical + current). Renaming/adding a profile_subdir without
    updating the table fails this test."""
    for tool in builtins_only_registry:
        historical = _HISTORICAL_PROFILE_SUBDIRS.get(tool.id)
        current_subdirs = {dm.profile_subdir for dm in tool.config_dirs}
        if historical is None:
            # Tools with no historical drift may legitimately omit the entry,
            # but only if their current subdir set is a single value matching
            # the tool id (the trivial single-dir case). Anything richer must
            # opt in by adding an entry to the table — even an empty one would
            # signal "no historical drift, intentional."
            assert current_subdirs == {tool.id}, (
                f"builtin {tool.id!r} has profile_subdirs {sorted(current_subdirs)} "
                f"but no _HISTORICAL_PROFILE_SUBDIRS entry. Add one (even an "
                f"empty frozenset) so future builtin rewrites surface here."
            )
            continue
        missing = current_subdirs - historical
        assert not missing, (
            f"builtin {tool.id!r} has profile_subdir(s) {sorted(missing)} not "
            f"in _HISTORICAL_PROFILE_SUBDIRS[{tool.id!r}]={sorted(historical)}. "
            f"Update the table in src/switcher/service.py to include the new "
            f"subdir(s) so legacy profiles still migrate cleanly."
        )


def test_every_builtin_live_path_basename_is_in_historical_table(
    builtins_only_registry: Sequence[Tool],
) -> None:
    """For each builtin tool, every live-path basename (POSIX and Windows)
    must be in _HISTORICAL_LIVE_PATH_BASENAMES[tool.id]. Renaming a live
    path's leaf without updating the table fails this test."""
    for tool in builtins_only_registry:
        historical = _HISTORICAL_LIVE_PATH_BASENAMES.get(tool.id)
        # Strip the leading dot from POSIX hidden dirs to compare canonically
        # — the historical table stores leaf names without the dot prefix
        # (e.g. "github-copilot" not ".config/github-copilot").
        current_basenames: set[str] = set()
        for dm in tool.config_dirs:
            for raw in (dm.posix_path, dm.windows_path):
                leaf = raw.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
                current_basenames.add(leaf.lstrip("."))
        if historical is None:
            # Trivial single-basename case: the basename equals the tool id
            # (post-dot-strip). Anything richer must opt in.
            assert current_basenames == {tool.id}, (
                f"builtin {tool.id!r} has live-path basenames "
                f"{sorted(current_basenames)} but no "
                f"_HISTORICAL_LIVE_PATH_BASENAMES entry. Add one so future "
                f"builtin rewrites surface here."
            )
            continue
        missing = current_basenames - historical - {tool.id}
        assert not missing, (
            f"builtin {tool.id!r} has live-path basename(s) {sorted(missing)} "
            f"not in _HISTORICAL_LIVE_PATH_BASENAMES[{tool.id!r}]="
            f"{sorted(historical)}. Update the table in src/switcher/service.py "
            f"so legacy live paths are still recognized as canonical."
        )
