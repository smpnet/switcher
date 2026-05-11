"""Typer-based CLI for switcher."""

from __future__ import annotations

import difflib
import functools
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import typer
from rich.console import Console
from rich.table import Table

from switcher.errors import (
    NothingToInitializeError,
    StateNotInitializedError,
    SwitcherError,
    UnknownToolError,
)
from switcher.models import Tool
from switcher.paths import IS_WINDOWS, PathResolver
from switcher.registry import build_registry, scaffold_tool
from switcher.service import InitReport, ProfileService, UninstallMappingState
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
        # Distinguish two empty-active-map states (Hermes review):
        #   - Uninitialized: no profiles on disk yet → suggest `init`,
        #     because `rescan` would fail with NotInitializedError.
        #   - Initialized but everything unmanaged (post-`unmanage` of
        #     the last tool, or `uninstall` without --purge): suggest
        #     `rescan` per spec §3.5 — the profile list is intact, the
        #     active map just happens to be empty.
        if not deps.store.list():
            console.print("No tools currently managed. Run 'switcher init' to set up switcher.")
        else:
            console.print(
                "No tools currently managed. Run 'switcher rescan' to discover installed tools."
            )
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


def _resolve_init_targets(
    registry: Sequence[Tool],
    user_ids: list[str],
    mode: Literal["only", "skip"],
) -> list[str]:
    """Validate user_ids against the registry; resolve to a target_ids list.

    Hard-errors on unknown ids with a did-you-mean suggestion.
    `only`: returns user_ids unchanged (the service layer intersects with
        detect_installed). Unknown ids raise here.
    `skip`: returns [t.id for t in registry if t.id not in user_ids].
    """
    registered_ids = [t.id for t in registry]
    for uid in user_ids:
        if uid not in registered_ids:
            matches = difflib.get_close_matches(uid, registered_ids, n=1, cutoff=0.6)
            suggestion = f" Did you mean {matches[0]!r}?" if matches else ""
            raise UnknownToolError(f"tool {uid!r} is not registered.{suggestion}")
    if mode == "only":
        return user_ids
    return [tid for tid in registered_ids if tid not in user_ids]


def _stdin_is_tty() -> bool:
    """Indirection for --interactive's TTY check.

    Direct `sys.stdin.isatty()` calls are uncooperative under
    click.testing.CliRunner, which swaps in its own StringIO during
    `invoke()`. Tests monkeypatch this module-level helper instead.
    """
    return sys.stdin.isatty()


def _resolve_init_targets_interactive(deps: Deps) -> tuple[list[str], list[str]]:
    """Prompt yes/no per detected tool. Returns (accepted_ids, declined_ids).

    Empty input accepts the default (Y). Any answer starting with 'n' or
    'N' declines; anything else (including 'y', 'Y', or empty) accepts.

    Per spec §2.1: --interactive is a *filtered* init mode. An empty
    detected list means the filter resolves to zero captures — the caller
    raises NothingToInitializeError (NOT the bare-init warn-and-empty path).
    """
    detected = deps.service.detect_installed()
    if not detected:
        return [], []
    accepted: list[str] = []
    declined: list[str] = []
    console.print("Detected installed tools:")
    for tool in detected:
        answer = input(f"  Manage {tool.id}? [Y/n] ").strip().lower()
        if answer.startswith("n"):
            declined.append(tool.id)
            continue
        accepted.append(tool.id)
    return accepted, declined


def _print_init_report(report: InitReport) -> None:
    """Render an InitReport to the console with captured / skipped /
    requested-but-not-installed sections and re-add hints."""
    console.print(f"Initialized profile {report.profile_name!r}")
    if report.captured:
        console.print(f"  Captured: {', '.join(report.captured)}")
    if report.requested_but_not_installed:
        console.print(
            f"  Requested but not detected: {', '.join(report.requested_but_not_installed)}"
        )
        for tid in report.requested_but_not_installed:
            console.print(f"    To add it later: switcher rescan --only {tid}")
    if report.skipped_via_skip_flag:
        console.print(f"  Skipped: {', '.join(report.skipped_via_skip_flag)}")
        for tid in report.skipped_via_skip_flag:
            console.print(f"    To add it later: switcher rescan --only {tid}")
    if report.skipped_via_interactive:
        console.print(f"  Skipped (via interactive): {', '.join(report.skipped_via_interactive)}")
        for tid in report.skipped_via_interactive:
            console.print(f"    To add it later: switcher rescan --only {tid}")


@app.command()
@handle_errors
def init(
    only: str | None = typer.Option(
        None,
        "--only",
        help="Comma-separated tool IDs to manage. Mutually exclusive with --skip/--interactive.",
    ),
    skip: str | None = typer.Option(
        None,
        "--skip",
        help="Comma-separated tool IDs to exclude. Mutually exclusive with --only/--interactive.",
    ),
    interactive: bool = typer.Option(
        False,
        "--interactive",
        help="Prompt per-detected-tool. Mutually exclusive with --only/--skip.",
    ),
) -> None:
    """Initialize switcher; optionally restrict to a subset of detected tools."""
    if interactive and (only is not None or skip is not None):
        raise typer.BadParameter("--interactive is mutually exclusive with --only/--skip")
    if only is not None and skip is not None:
        raise typer.BadParameter("--only and --skip are mutually exclusive")

    deps = get_deps()
    requested_but_not_installed: list[str] = []
    skipped_via_skip_flag: list[str] = []
    skipped_via_interactive: list[str] = []
    target_ids: list[str] | None

    if interactive:
        if not _stdin_is_tty():
            raise typer.BadParameter(
                "--interactive requires a tty; use --only or --skip in non-interactive contexts"
            )
        accepted, declined = _resolve_init_targets_interactive(deps)
        if not accepted:
            raise NothingToInitializeError("every detected tool was skipped; nothing to initialize")
        target_ids = accepted
        skipped_via_interactive = declined
    elif only is not None:
        ids = [t.strip() for t in only.split(",") if t.strip()]
        if not ids:
            raise typer.BadParameter("--only must contain at least one tool id")
        target_ids = _resolve_init_targets(deps.registry, ids, mode="only")
        detected_ids = {t.id for t in deps.service.detect_installed()}
        requested_but_not_installed = [uid for uid in ids if uid not in detected_ids]
    elif skip is not None:
        ids = [t.strip() for t in skip.split(",") if t.strip()]
        if not ids:
            raise typer.BadParameter("--skip must contain at least one tool id")
        target_ids = _resolve_init_targets(deps.registry, ids, mode="skip")
        if not target_ids:
            # --skip excluded every registered tool. The generic
            # "no requested tools are installed" message at the service
            # layer is wrong for this path — surface the actual cause
            # before reaching service.init(). (abby review)
            raise NothingToInitializeError(
                "--skip excluded every registered tool; nothing left to initialize"
            )
        skipped_via_skip_flag = ids
    else:
        target_ids = None

    report = deps.service.init(
        target_ids,
        requested_but_not_installed=requested_but_not_installed,
        skipped_via_skip_flag=skipped_via_skip_flag,
        skipped_via_interactive=skipped_via_interactive,
    )
    _print_init_report(report)


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
def unmanage(
    tool: str = typer.Argument(..., help="Tool ID to unmanage."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview only; no changes."),
    force: bool = typer.Option(
        False,
        "--force",
        help="Skip orphan tools with no registry entry and no cache. "
        "Does NOT bypass corrupt mappings.",
    ),
) -> None:
    """Restore a single tool's live path and remove it from the active map."""
    deps = get_deps()
    report = deps.service.unmanage(tool, dry_run=dry_run, force=force)
    if dry_run:
        console.print(f"Would unmanage {report.tool_id!r}:")
        for m in report.mappings:
            if m.state == UninstallMappingState.SYMLINK:
                verb = "would restore"
            elif m.state == UninstallMappingState.MISSING_LIVE_TEMP_PRESENT:
                # A real run renames the sibling temp dir back into the
                # live position — that's a recovery, NOT a no-op. (abby review)
                verb = "would recover from interrupted uninstall"
            else:  # ALREADY_RESTORED (CORRUPT refused by pre-flight)
                verb = "no-op (already restored)"
            # `soft_wrap=True` keeps the verb token together. Without it,
            # Rich wraps long Windows paths and splits "would recover"
            # across a hard newline, breaking substring assertions and
            # making the preview harder to grep.
            console.print(f"  {m.live_path}  ({m.state.name} -> {verb})", soft_wrap=True)
        console.print("(dry-run; no changes made)")
    elif report.skipped_orphan:
        console.print(
            f"Skipped orphan tool {report.tool_id!r}: removed from active map; "
            f"symlinks left in place"
        )
    else:
        console.print(f"Unmanaged {report.tool_id!r}")


@app.command()
@handle_errors
def rescan(
    only: str | None = typer.Option(None, "--only", help="Comma-separated tool ids."),
    into: str | None = typer.Option(None, "--into", help="Capture into an existing profile."),
    all_: bool = typer.Option(
        False,
        "--all",
        help="Capture all detected unmanaged tools without prompting. "
        "Mutually exclusive with --only.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print plan; make no changes."),
) -> None:
    """Pick up tools installed after init.

    With no flags, on a TTY: prompt per detected unmanaged tool (default-Y).
    Off-TTY: print a stderr warning and capture every detected unmanaged
    tool. Use --all to suppress the warning, or --only to be selective.
    --dry-run never prompts regardless of TTY.
    """
    if all_ and only is not None:
        raise typer.BadParameter("--all and --only are mutually exclusive")

    deps = get_deps()

    # Preflight: surface StateNotInitializedError BEFORE the new prompt /
    # warning logic touches the user (Hermes review). Otherwise bare
    # rescan on an uninitialized machine prints capture-all warnings or
    # prompts the user, then fails inside service.rescan() with "switcher
    # has not been initialized" — misleading UX.
    if not deps.store.list():
        raise StateNotInitializedError("switcher has not been initialized; run 'switcher init'")

    # Mirror `use --only`: an explicitly empty `--only ""` is invalid input,
    # not "no filter". Without this the CLI silently bypasses the
    # service-layer guard at service.rescan (`--only requires at least one
    # tool id`), making `--only ""` behave like bare `rescan`.
    if only is not None:
        only_list = [s.strip() for s in only.split(",") if s.strip()]
        if not only_list:
            raise typer.BadParameter("--only must contain at least one tool id")
        report = deps.service.rescan(only=only_list, into=into, dry_run=dry_run)
    elif all_:
        report = deps.service.rescan(only=None, into=into, dry_run=dry_run)
    elif dry_run:
        # --dry-run alone: preview-all, never prompt regardless of TTY.
        report = deps.service.rescan(only=None, into=into, dry_run=True)
    else:
        # Bare rescan: TTY prompt per tool; non-TTY warn-and-capture-all.
        # Detection mirrors service.rescan's candidate set so the prompt
        # only lists what would actually be captured.
        unmanaged_detected = [
            t for t in deps.service.detect_installed() if t.id not in deps.store.get_active()
        ]
        if not unmanaged_detected:
            err_console.print("no new tools detected")
            return
        if _stdin_is_tty():
            console.print("Detected unmanaged tools:")
            accepted: list[str] = []
            for tool in unmanaged_detected:
                answer = input(f"  Capture {tool.id}? [Y/n] ").strip().lower()
                if answer.startswith("n"):
                    continue
                accepted.append(tool.id)
            if not accepted:
                console.print("Nothing accepted; nothing captured.")
                return
            report = deps.service.rescan(only=accepted, into=into, dry_run=False)
        else:
            ids = [t.id for t in unmanaged_detected]
            err_console.print(
                f"warning: capturing all detected unmanaged tools without prompt: "
                f"{', '.join(ids)}. Use --only to be selective, or --all to "
                f"suppress this warning."
            )
            report = deps.service.rescan(only=None, into=into, dry_run=False)

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


@dataclass(frozen=True)
class _ToolsTableRow:
    tool_id: str
    name: str
    installed: bool
    managed: bool
    paths: list[str]
    pathological: bool  # True when managed AND NOT installed


def _build_tools_table_rows(deps: Deps) -> list[_ToolsTableRow]:
    """Pure: registry → list of rows. Used by tools_main to populate the
    Rich table; also a clean assertion target for tests.

    `deps.store.get_active()` returns `{}` when config.json doesn't exist
    yet (pre-init), so no try/except is needed for the uninit case.
    A real StorageError (corrupt / unreadable config.json) intentionally
    propagates — the broad `except Exception` we used to have masked exactly
    the failures the rest of the CLI is careful to surface (Hermes review).
    """
    installed_ids = {t.id for t in deps.service.detect_installed()}
    managed_ids = set(deps.store.get_active().keys())
    rows: list[_ToolsTableRow] = []
    for tool in deps.registry:
        installed = tool.id in installed_ids
        managed = tool.id in managed_ids
        paths = [(dm.windows_path if IS_WINDOWS else dm.posix_path) for dm in tool.config_dirs]
        rows.append(
            _ToolsTableRow(
                tool_id=tool.id,
                name=tool.name,
                installed=installed,
                managed=managed,
                paths=paths,
                pathological=(managed and not installed),
            )
        )
    return rows


@tools_app.callback(invoke_without_command=True)
@handle_errors
def tools_main(ctx: typer.Context) -> None:
    """List supported tools and per-OS config paths."""
    if ctx.invoked_subcommand is not None:
        return
    deps = get_deps()
    rows = _build_tools_table_rows(deps)
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Installed", justify="center")
    table.add_column("Managed", justify="center")
    table.add_column("Paths" + (" (Windows)" if IS_WINDOWS else " (POSIX)"))
    for row in rows:
        installed_cell = "⚠" if row.pathological else ("✓" if row.installed else "—")
        managed_cell = "✓" if row.managed else "—"
        table.add_row(
            row.tool_id,
            row.name,
            installed_cell,
            managed_cell,
            "\n".join(row.paths),
        )
    console.print(table)
    for row in rows:
        if row.pathological:
            console.print(
                f"[yellow]⚠ {row.tool_id!r} is in active map but its live path is missing.[/yellow]\n"
                f"  Run 'switcher unmanage {row.tool_id}' or restore the live path."
            )
