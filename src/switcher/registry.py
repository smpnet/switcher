"""Two-layer registry: built-ins ship inside the wheel; users add TOMLs in
`<state_dir>/registry.d/`. Same code path loads both. User entries override
builtins (with a stderr warning)."""

from __future__ import annotations

import sys
import tomllib
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

from switcher.errors import StorageError
from switcher.models import Tool, canonicalize_path_for_uniqueness


def _load_toml_resource(resource: Traversable | Path) -> Tool:
    """Parse one TOML file into a Tool. Wraps validation errors with file path."""
    try:
        with resource.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"error parsing {resource}: {e}") from e
    try:
        return Tool.model_validate(data)
    except Exception as e:
        raise ValueError(f"error loading {resource}: {e}") from e


def load_builtin_tools() -> tuple[Tool, ...]:
    """Load every TOML inside the `switcher.builtins` package."""
    pkg = files("switcher.builtins")
    out: list[Tool] = []
    for resource in sorted(pkg.iterdir(), key=lambda r: r.name):
        if resource.name.endswith(".toml"):
            out.append(_load_toml_resource(resource))
    return tuple(out)


def load_user_tools(registry_dir: Path) -> tuple[Tool, ...]:
    """Load every *.toml in `registry_dir`, sorted by filename."""
    if not registry_dir.is_dir():
        return ()
    return tuple(_load_toml_resource(p) for p in sorted(registry_dir.glob("*.toml")))


def _validate_config_files_unique_across_tools(tools: tuple[Tool, ...]) -> None:
    """Reject cross-tool ConfigFile collisions.

    Per-tool ``ConfigFile`` uniqueness (snapshot slot + posix/windows live
    paths, all case-insensitive AND path-normalized) is enforced by the
    Tool model validator, but that only catches collisions *within* a
    single tool. Two distinct tools could still claim the same snapshot
    slot or the same live config file (abby r13). On ``save()`` /
    ``use()`` that would silently race the two tools in registry order
    — the later writer wins, the earlier tool's data is lost.

    Comparison routes each path through
    ``canonicalize_path_for_uniqueness`` (case-insensitive,
    path-normalized, AND home-spelling-folded). Any of the four
    runtime-equivalent spellings of a home-relative path —
    ``~/.claude.json``, ``$HOME/.claude.json``,
    ``${HOME}/.claude.json``, ``%USERPROFILE%\\.claude.json`` —
    produces the same key, mirroring how ``PathResolver.expand`` +
    ``os.path.normpath`` collapses them at runtime. Raw-string
    compare with normpath-only would still let
    ``~/.claude.json`` vs ``$HOME/.claude.json`` slip through and
    re-open the cross-tool clobber class these validators close
    (Hermes pass-PR-3 + pass-PR-4 blockers).

    Casefold matches the runtime behavior on default Windows (NTFS)
    and default macOS (APFS) filesystems —
    case-insensitive-but-preserving.

    With the v0.1.5 max-one-ConfigFile-per-tool cap, each tool
    contributes at most one entry to each map; we don't need a
    same-tool guard.
    """
    seen_slot: dict[tuple[str, str], str] = {}
    seen_posix: dict[str, str] = {}
    seen_windows: dict[str, str] = {}
    for t in tools:
        for cf in t.config_files:
            slot = (cf.profile_subdir.casefold(), cf.profile_filename.casefold())
            if slot in seen_slot:
                raise ValueError(
                    f"cross-tool config_file collision: tools "
                    f"{seen_slot[slot]!r} and {t.id!r} both claim snapshot "
                    f"slot (profile_subdir={cf.profile_subdir!r}, "
                    f"profile_filename={cf.profile_filename!r})"
                )
            seen_slot[slot] = t.id
            posix_key = canonicalize_path_for_uniqueness(cf.posix_path, windows=False)
            if posix_key in seen_posix:
                raise ValueError(
                    f"cross-tool config_file collision: tools "
                    f"{seen_posix[posix_key]!r} and {t.id!r} both claim "
                    f"posix_path {cf.posix_path!r}; comparison is "
                    f"case-insensitive, path-normalized, and "
                    f"home-spelling-folded"
                )
            seen_posix[posix_key] = t.id
            windows_key = canonicalize_path_for_uniqueness(cf.windows_path, windows=True)
            if windows_key in seen_windows:
                raise ValueError(
                    f"cross-tool config_file collision: tools "
                    f"{seen_windows[windows_key]!r} and {t.id!r} both claim "
                    f"windows_path {cf.windows_path!r}; comparison is "
                    f"case-insensitive, path-normalized, and "
                    f"home-spelling-folded"
                )
            seen_windows[windows_key] = t.id


def build_registry(registry_dir: Path) -> tuple[Tool, ...]:
    """Merge builtins and user tools. User entries override builtins, with a
    stderr warning so the override is visible. A second user TOML colliding
    with an earlier user TOML is reported as a duplicate, not a builtin
    override — distinguishing the two helps users find the actual conflict.

    Order of checks matters: once a user file has claimed a slot, the next
    collision is user-vs-user even if the slot started as a builtin. Check
    `user_seen` first so the second user file gets the right attribution.

    Cross-tool ConfigFile uniqueness is validated last, after the override
    merge has converged, so the check sees the final tool set the rest of
    the system operates on.
    """
    builtins = load_builtin_tools()
    builtin_ids = frozenset(t.id for t in builtins)
    by_id: dict[str, Tool] = {t.id: t for t in builtins}
    user_seen: set[str] = set()
    for t in load_user_tools(registry_dir):
        if t.id in user_seen:
            print(
                f"warning: duplicate user tool {t.id!r} (later file wins)",
                file=sys.stderr,
            )
        elif t.id in builtin_ids:
            print(
                f"warning: user tool {t.id!r} overrides builtin",
                file=sys.stderr,
            )
        by_id[t.id] = t
        user_seen.add(t.id)
    final = tuple(by_id.values())
    _validate_config_files_unique_across_tools(final)
    return final


def find_tool(registry: tuple[Tool, ...], tool_id: str) -> Tool | None:
    for t in registry:
        if t.id == tool_id:
            return t
    return None


_SCAFFOLD_TEMPLATE = """\
# Tool definition for {id}. Fill in `name` and adjust paths to match the tool's
# actual on-disk layout, then drop this file into <state_dir>/registry.d/.
id = "{id}"
name = "<Display Name>"
credential_files = []   # shorthand list, paths relative to first config_dir

[[config_dirs]]
posix_path = "~/.{id}"
windows_path = "%USERPROFILE%\\\\.{id}"
profile_subdir = "{id}"
# env_override = "MYTOOL_HOME"   # optional; remove if no env override exists
"""


def scaffold_tool(tool_id: str, out_path: Path) -> None:
    """Write a stub TOML for a new user tool to `out_path`.

    Refuses to overwrite — raises StorageError if the file already exists.
    """
    if out_path.exists():
        raise StorageError(f"refusing to overwrite {out_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_SCAFFOLD_TEMPLATE.format(id=tool_id), encoding="utf-8")
