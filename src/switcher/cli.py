"""Typer-based CLI for switcher."""

from __future__ import annotations

import functools
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from switcher.errors import SwitcherError
from switcher.models import Tool
from switcher.paths import IS_WINDOWS, PathResolver
from switcher.registry import build_registry, scaffold_tool
from switcher.service import ProfileService, UninstallMappingState
from switcher.store import FileProfileStore, ProfileStore

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
tools_app = typer.Typer(
    no_args_is_help=False,
    invoke_without_command=True,
    help="List supported tools or scaffold a new one.",
)
app.add_typer(tools_app, name="tools")

console = Console()
err_console = Console(stderr=True)


@dataclass(frozen=True)
class Deps:
    service: ProfileService
    store: ProfileStore
    registry: Sequence[Tool]


def get_deps() -> Deps:
    """Construct the runtime dependency tree. Tests override this via monkeypatch."""
    resolver = PathResolver()
    state = resolver.state_dir()
    registry = build_registry(state / "registry.d")
    store = FileProfileStore(state)
    service = ProfileService(store, resolver, registry)
    return Deps(service=service, store=store, registry=registry)


def handle_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except SwitcherError as e:
            err_console.print(f"[red]error:[/] {e}")
            raise typer.Exit(code=1) from e

    return wrapper


# -- simple read-only commands ----------------------------------------------


@app.command()
def version() -> None:
    """Print the installed switcher version."""
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as pkg_version

    try:
        v = pkg_version("switcher")
    except PackageNotFoundError:
        v = "0.0.0+local"
    console.print(f"switcher v{v}")


@app.command(name="list")
@handle_errors
def list_cmd() -> None:
    """List all profiles. `*` marks any tool's active profile."""
    deps = get_deps()
    profiles = deps.store.list()
    active = set(deps.store.get_active().values())
    if not profiles:
        console.print("No profiles found.")
        return
    for p in profiles:
        marker = "*" if p.name in active else " "
        console.print(f"{marker} {p.name}")


@app.command()
@handle_errors
def status(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show cached live paths."),
) -> None:
    """Show currently-active profiles per tool, plus live-path cache state."""
    deps = get_deps()
    active = deps.store.get_active()
    if not active:
        console.print("no active profiles")
        return
    # `markup=False` is REQUIRED because `[ok]` / `[--]` would otherwise be
    # interpreted as Rich markup tags. Spec §6.5.
    # Read the RAW persisted cache (not the derived view) so `[ok]` strictly
    # means "active_live_paths[tool_id] is populated on disk" per spec §6.5.
    # The derived view via service.get_active_live_paths() synthesizes from
    # live links when cache is empty, which would mask the legacy / drifted
    # state operators are trying to diagnose.
    cache = deps.store.get_active_live_paths()
    for tool_id in sorted(active):
        profile = active[tool_id]
        cache_marker = "[ok]" if cache.get(tool_id) else "[--]"
        # `soft_wrap=True` so paths/profile names are never broken across lines
        # by Rich's terminal-width wrap. Status output has to remain stable
        # (and substring-greppable) under narrow CI terminals.
        console.print(f"{cache_marker} {tool_id:20} {profile}", markup=False, soft_wrap=True)
        if verbose:
            paths = cache.get(tool_id, [])
            if paths:
                for p in paths:
                    console.print(f"       {p}", markup=False, soft_wrap=True)
            else:
                # Don't promise "fall back to registry": for orphan tools
                # (no registry entry AND no cache) uninstall refuses, so the
                # earlier wording contradicted the next command's behavior.
                console.print("       live_paths not cached", markup=False, soft_wrap=True)


# -- mutating commands ------------------------------------------------------


@app.command()
@handle_errors
def init() -> None:
    """One-time setup: detect tools, snapshot current config, create vanilla."""
    name = get_deps().service.init()
    console.print(f"Initialized profile {name!r}")


@app.command()
@handle_errors
def use(
    name: str,
    only: str | None = typer.Option(
        None,
        "--only",
        help="Comma-separated tool IDs; default: all tools the profile includes.",
    ),
) -> None:
    """Switch a profile (atomically re-points the live config dirs)."""
    if only is None:
        only_list = None
    else:
        only_list = [t.strip() for t in only.split(",") if t.strip()]
        if not only_list:
            raise typer.BadParameter("--only must contain at least one tool id")
    get_deps().service.use(name, only_list)
    if only_list is None:
        console.print(f"Using profile {name!r} for all tools")
    else:
        console.print(f"Using profile {name!r} for tools: {', '.join(only_list)}")


@app.command()
@handle_errors
def create(name: str) -> None:
    """Create a new profile, seeding credentials from the current active profile."""
    get_deps().service.create(name)
    console.print(f"Created profile {name!r}")


@app.command()
@handle_errors
def save(name: str) -> None:
    """Snapshot current live config into a new profile."""
    get_deps().service.save(name)
    console.print(f"Saved live config as {name!r}")


@app.command()
@handle_errors
def rename(old: str, new: str) -> None:
    """Rename a profile. Active tools auto-relink to the new name."""
    get_deps().service.rename(old, new)
    console.print(f"Renamed {old!r} -> {new!r}")


@app.command()
@handle_errors
def delete(
    name: str,
    force: bool = typer.Option(False, "--force", help="Skip the interactive confirmation."),
) -> None:
    """Delete a profile. Refuses if the profile is active for any tool."""
    if not force and not typer.confirm(f"Delete profile {name!r}?"):
        raise typer.Exit(code=0)
    get_deps().service.delete(name)
    console.print(f"Deleted profile {name!r}")


@app.command()
@handle_errors
def which(tool: str) -> None:
    """Show which profile a specific tool is currently using."""
    name = get_deps().service.which(tool)
    console.print(name)


@app.command()
@handle_errors
def uninstall(
    purge: bool = typer.Option(
        False, "--purge", help="Also remove the state directory after restoring real dirs."
    ),
    yes: bool = typer.Option(False, "--yes", help="Skip the --purge confirmation prompt."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print plan; make no changes."),
    force: bool = typer.Option(
        False, "--force", help="Skip orphan tools (registry-and-cache-missing)."
    ),
) -> None:
    """Inverse of init: replace every active symlink with a real directory."""
    deps = get_deps()
    report = deps.service.uninstall(
        purge=purge,
        yes=yes,
        dry_run=dry_run,
        force=force,
    )
    prefix = "would " if dry_run else ""
    for m in report.mappings:
        if m.state == UninstallMappingState.ALREADY_RESTORED:
            err_console.print(f"already restored {m.live_path}")
        elif m.state == UninstallMappingState.MISSING_LIVE_TEMP_PRESENT:
            verb = "would recover" if dry_run else "recovered"
            err_console.print(f"{verb} {m.live_path} from interrupted uninstall")
        else:
            err_console.print(
                f"{prefix}unlink {m.live_path} (was symlink to "
                f"{m.profile_dir_subdir.parent.name}/{m.profile_subdir})"
            )
    for tool_id, reason in report.skipped:
        err_console.print(f"skipped {tool_id}: {reason}")
    # Footer: consult report.purged (NOT the purge flag) — service may have
    # returned without purging if the user declined the prompt.
    if dry_run:
        if purge:
            err_console.print(f"would purge {deps.store.state_dir()}")
        else:
            err_console.print(
                f"would clear active map; would keep state at {deps.store.state_dir()}"
            )
    elif report.purged:
        err_console.print(f"purged {deps.store.state_dir()}")
    else:
        err_console.print(f"cleared active map; kept state at {deps.store.state_dir()}")


@app.command()
@handle_errors
def rescan(
    only: str | None = typer.Option(None, "--only", help="Comma-separated tool ids."),
    into: str | None = typer.Option(None, "--into", help="Capture into an existing profile."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print plan; make no changes."),
) -> None:
    """Pick up tools installed after init."""
    # Mirror `use --only`: an explicitly empty `--only ""` is invalid input,
    # not "no filter". Without this the CLI silently bypasses the
    # service-layer guard at service.rescan (`--only requires at least one
    # tool id`), making `--only ""` behave like bare `rescan`.
    if only is None:
        only_list = None
    else:
        only_list = [s.strip() for s in only.split(",") if s.strip()]
        if not only_list:
            raise typer.BadParameter("--only must contain at least one tool id")
    deps = get_deps()
    report = deps.service.rescan(only=only_list, into=into, dry_run=dry_run)
    if not report.captured:
        err_console.print("no new tools detected")
        return
    verb = "would capture" if dry_run else "captured"
    for tool_id, target in report.captured:
        err_console.print(f"{verb} {tool_id} into {target}")


@app.command()
@handle_errors
def prune(
    force: bool = typer.Option(False, "--force", help="Skip the confirmation prompt."),
    dry_run: bool = typer.Option(False, "--dry-run", help="List orphans; make no changes."),
) -> None:
    """Delete orphan profiles."""
    deps = get_deps()

    # First call is always a dry-run to enumerate orphans + sizes.
    preview = deps.service.prune(dry_run=True)
    if not preview.sizes_bytes:
        err_console.print("no orphan profiles")
        return

    _print_orphan_list(preview.sizes_bytes)
    if dry_run:
        err_console.print("Run without --dry-run to delete.")
        return

    if not force:
        if not sys.stdin.isatty():
            err_console.print("refusing to delete without --force in non-interactive mode")
            raise typer.Exit(code=1)
        if not typer.confirm("Delete all?"):
            err_console.print("aborted; nothing deleted")
            return

    final = deps.service.prune(force=True)
    err_console.print(f"deleted {len(final.deleted)} orphan profile(s)")


def _print_orphan_list(sizes: dict[str, int]) -> None:
    err_console.print(f"{len(sizes)} orphan profile(s):")
    for name, sz in sizes.items():
        err_console.print(f"  {name}  {_fmt_size(sz)}")


def _fmt_size(n: int) -> str:
    if n < 1024:
        return "<1 KB"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


@tools_app.command(name="scaffold")
@handle_errors
def tools_scaffold(
    tool_id: str,
    out: str | None = typer.Option(
        None,
        "--out",
        help="Output path; defaults to <state_dir>/registry.d/<id>.toml",
    ),
) -> None:
    """Write a stub TOML for a new user tool."""
    deps = get_deps()
    target = (
        Path(out).expanduser() if out else deps.store.state_dir() / "registry.d" / f"{tool_id}.toml"
    )
    scaffold_tool(tool_id, target)
    console.print(f"Wrote scaffold to {target}")


@tools_app.callback(invoke_without_command=True)
@handle_errors
def tools_main(ctx: typer.Context) -> None:
    """List supported tools and per-OS config paths."""
    if ctx.invoked_subcommand is not None:
        return
    deps = get_deps()
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Paths" + (" (Windows)" if IS_WINDOWS else " (POSIX)"))
    for tool in deps.registry:
        paths = "\n".join(
            (dm.windows_path if IS_WINDOWS else dm.posix_path) for dm in tool.config_dirs
        )
        table.add_row(tool.id, tool.name, paths)
    console.print(table)
