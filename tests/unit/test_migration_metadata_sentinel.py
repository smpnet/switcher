# pyright: reportPrivateUsage=none
"""Sentinel: builtin TOMLs and historical-metadata tables stay in sync.

Why: the v0.1.4 FS-truth migration (spec §4, post-PR Hermes blocker fix)
reads three per-tool tables in `switcher.service`:

- `_HISTORICAL_PROFILE_SUBDIRS` — subdir names a tool's profile dir may
  contain across switcher versions (current registry union history).
- `_HISTORICAL_LIVE_PATHS_POSIX` / `_HISTORICAL_LIVE_PATHS_WINDOWS` —
  EXACT historical full-path templates per tool, per platform.

A future commit that rewrites a builtin TOML to use a new path or subdir
name without also updating the relevant table would silently break
migration for upgrading users — the new path/subdir wouldn't be
recognized as candidate profile content, so legacy data would be
orphaned.

This test enforces two contributor invariants:
1. Every current builtin profile_subdir is represented in the
   _HISTORICAL_PROFILE_SUBDIRS table.
2. Every entry in the historical full-path tables is structurally valid
   AND distinct from the tool's current registry paths (so we don't
   accidentally duplicate-discover the same live path).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from switcher.models import Tool
from switcher.paths import IS_WINDOWS, PathResolver
from switcher.registry import build_registry
from switcher.service import (
    _HISTORICAL_LIVE_PATHS_POSIX,
    _HISTORICAL_LIVE_PATHS_WINDOWS,
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


def test_historical_live_paths_are_well_formed_and_distinct_from_current(
    builtins_only_registry: Sequence[Tool], tmp_path: Path
) -> None:
    """For each entry in the per-platform historical-paths tables, verify:
       (a) The path expands cleanly via the resolver (no missing %VAR% or
           leftover ~).
       (b) The expanded path is NOT equal to any current registry path
           for the same tool — historical entries should be GENUINELY
           historical, not duplicate the current builtin.

    Catches a contributor accidentally either (a) using a syntactically
    invalid template, or (b) leaving stale entries in the historical
    table after rotating a builtin's path through it.
    """
    home = tmp_path / "home"
    home.mkdir()
    resolver = PathResolver(home=home)
    historical_table = (
        _HISTORICAL_LIVE_PATHS_WINDOWS if IS_WINDOWS else _HISTORICAL_LIVE_PATHS_POSIX
    )

    by_id = {tool.id: tool for tool in builtins_only_registry}
    for tool_id, raw_paths in historical_table.items():
        tool = by_id.get(tool_id)
        current_paths: set[Path] = set()
        if tool is not None:
            for i in range(len(tool.config_dirs)):
                current_paths.add(resolver.tool_dir(tool, i))
        for raw in raw_paths:
            expanded = resolver.expand(raw)
            # Resolver shouldn't leave %VAR% or ~ in place. (Path.expand on
            # an unresolved env-var template returns the literal string.)
            assert "%" not in str(expanded), (
                f"historical entry {raw!r} for tool {tool_id!r} contains an "
                f"unexpanded %VAR%. Fix the template or add the missing env."
            )
            assert "~" not in str(expanded), (
                f"historical entry {raw!r} for tool {tool_id!r} contains an unresolved '~'."
            )
            assert expanded not in current_paths, (
                f"historical entry {raw!r} for tool {tool_id!r} resolves to "
                f"{expanded}, which is also a current registry path. "
                f"Drop the duplicate from the historical table — current "
                f"paths come from the registry automatically."
            )
