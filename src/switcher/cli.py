"""Typer-based CLI for switcher."""

from __future__ import annotations

import functools
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
from switcher.service import ProfileService
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
def status() -> None:
    """Show active profile per tool."""
    deps = get_deps()
    active = deps.store.get_active()
    if not active:
        console.print("no active profiles")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("Tool")
    table.add_column("Active Profile")
    for tool in deps.registry:
        table.add_row(tool.id, active.get(tool.id, "-"))
    console.print(table)


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
    only_list = [t.strip() for t in only.split(",")] if only else None
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
    target = Path(out) if out else deps.store.state_dir() / "registry.d" / f"{tool_id}.toml"
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
