"""Typer-based CLI for switcher."""

from __future__ import annotations

import contextlib
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
    InitInProgressError,
    NothingToInitializeError,
    RescanInProgressError,
    StateAlreadyInitializedError,
    StateNotInitializedError,
    SwitcherError,
    UnknownToolError,
)
from switcher.models import Tool

# Op-log record classes (`_InitOp`, `_RenameOp`, `_RescanOp`) are namespace-
# private to oplog.py; the CLI detection hook (spec §2.2) is a legitimate
# cross-module consumer that dispatches on them. Per-line basedpyright
# suppressions keep the privacy intent explicit at the import site rather
# than leaking module-wide — mirrors the pattern in service.py.
from switcher.oplog import (
    OpLogIO,
    OpLogRecord,
    _InitOp,  # pyright: ignore[reportPrivateUsage]
    _RenameOp,  # pyright: ignore[reportPrivateUsage]
    _RescanOp,  # pyright: ignore[reportPrivateUsage]
)
from switcher.paths import IS_WINDOWS, PathResolver
from switcher.registry import build_registry, scaffold_tool
from switcher.service import InitReport, ProfileService, UninstallMappingState
from switcher.store import FileProfileStore, ProfileStore

# Reconfigure stdout/stderr to UTF-8 so the v0.1.4 tools table can emit its
# ✓ / — / ⚠ glyphs without UnicodeEncodeError on Windows consoles whose
# default codepage is cp1252 / cp437. Modern Windows Terminal handles UTF-8
# natively; legacy cmd.exe sessions degrade to "?" via errors="replace"
# rather than crashing. POSIX terminals are already UTF-8 — the reconfigure
# is a no-op there.
#
# Done at module import time (NOT lazily) so any code path that reaches
# `console.print(...)` is covered, including subprocess invocations from
# tests that read stdout via PIPE (the default subprocess.PIPE encoding on
# Windows is cp1252, which is what surfaced this).
for _stream in (sys.stdout, sys.stderr):
    if _stream is None:
        continue
    encoding = getattr(_stream, "encoding", None)
    if encoding and encoding.lower().replace("-", "") != "utf8":
        # Older Python or non-text stream falls through silently and lets
        # Rich's default encode-error handling kick in.
        with contextlib.suppress(AttributeError, OSError):
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

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
    oplog: OpLogIO


def get_deps() -> Deps:
    """Construct the runtime dependency tree. Tests override this via monkeypatch."""
    resolver = PathResolver()
    state = resolver.state_dir()
    registry = build_registry(state / "registry.d")
    store = FileProfileStore(state)
    service = ProfileService(store, resolver, registry)
    oplog = OpLogIO(state)
    return Deps(service=service, store=store, registry=registry, oplog=oplog)


def handle_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except SwitcherError as e:
            err_console.print(f"[red]error:[/] {e}")
            raise typer.Exit(code=1) from e
        except (KeyboardInterrupt, EOFError) as e:
            # Interactive prompts (init --interactive, bare rescan on TTY)
            # call input() directly. Without this catch a Ctrl-C / closed
            # stdin during the prompt bubbles out as a raw traceback.
            # Exit cleanly BEFORE any service-level mutation runs (Hermes
            # review): the prompt loops gather user choices and only call
            # service.init / service.rescan after they return, so an
            # interrupt during the prompt cannot have produced partial
            # mutation. 130 = 128 + SIGINT, the conventional Unix exit
            # code for "killed by SIGINT"; EOF gets the same exit since
            # both are user-side aborts.
            err_console.print("[yellow]aborted[/]")
            raise typer.Exit(code=130) from e

    return wrapper


# -- op-log detection hook (spec §2.2) --------------------------------------


def _format_in_progress_hint(record: OpLogRecord) -> str:
    """Render the user-facing recovery hint for an in-flight init/rescan.

    Same text for read-only and mutating callers so the wording stays
    consistent across multiple invocations of the same broken state.
    Rename has no hint — it is auto-compensated transparently.

    The hint references ``switcher init --continue/--abort`` and
    ``switcher rescan --continue/--abort``. Those flags ship in
    follow-on PRs (init compensation = Phase 6; rescan compensation =
    Phase 7). Until they land, ``service.init`` and ``service.rescan``
    do NOT write op-log intent records — so the journal never carries
    an in-flight ``_InitOp`` or ``_RescanOp`` in a PR3-only deployment,
    and this hint surface is unreachable. The flags become functional
    in lockstep with the records becoming writable.
    """
    if isinstance(record, _InitOp):
        targets = ", ".join(record.target_ids) if record.target_ids else "(none)"
        return (
            f"Interrupted `switcher init` detected "
            f"(started {record.started_at.isoformat()}).\n"
            f"  Profile: {record.profile_name}\n"
            f"  Targets: {targets}\n"
            f"Run `switcher init --continue` to finish the capture, or\n"
            f"`switcher init --abort` to restore the pre-init state."
        )
    if isinstance(record, _RescanOp):
        targets = ", ".join(record.target_ids) if record.target_ids else "(none)"
        # into_mode rescan routes every target to a single existing
        # profile; surface that target by name. Fresh-mode rescan creates
        # one profile per tool — label it explicitly so the user knows
        # which mode the interrupted op was in.
        if record.into_mode:
            # Spec invariant: into-mode rescan captures every target tool
            # into the SAME existing profile, so set(target_profiles.values())
            # should be a singleton. Reaching the multi-value branch implies
            # a hand-edited / corrupt journal — emit a marker rather than a
            # bogus "switcher rescan --into a, b" command the CLI won't
            # accept (abby review). _RescanOp validators don't enforce this
            # singleton today; the hint stays robust against that gap.
            into_targets = sorted(set(record.target_profiles.values()))
            if len(into_targets) == 1:
                mode_desc = f"--into {into_targets[0]}"
            else:
                mode_desc = f"--into (corrupt: multiple distinct values {into_targets!r})"
        else:
            mode_desc = "fresh-profile"
        profiles_desc = ", ".join(
            f"{tid}->{prof}" for tid, prof in sorted(record.target_profiles.items())
        )
        return (
            f"Interrupted `switcher rescan` detected "
            f"(started {record.started_at.isoformat()}).\n"
            f"  Mode: {mode_desc} (target profiles: {profiles_desc})\n"
            f"  Targets: {targets}\n"
            f"Run `switcher rescan --continue` to finish the capture, or\n"
            f"`switcher rescan --abort` to restore the pre-rescan state."
        )
    raise AssertionError(f"unexpected in-flight record type: {type(record).__name__}")


def _detect_or_compensate_oplog(deps: Deps, *, allow_mutation: bool) -> None:
    """Op-log detection hook.

    Called at the top of every state-touching command callback (every
    callback in this module's `@app.command` / `@tools_app.command` /
    `@tools_app.callback` set EXCEPT ``version``, which has no FS
    dependency and intentionally skips the hook per spec §5.2 dispatch
    table). Mutating callbacks must invoke this BEFORE any interactive
    prompt — otherwise an in-flight init/rescan would let the user
    answer a confirm dialog before being told about the broken state.

    Always vacuums completed records first so a stale "completed" snapshot
    cannot masquerade as in-flight. Then:

    - In-flight `_RenameOp`: auto-compensates via
      ``service._compensate_rename`` REGARDLESS of ``allow_mutation``.
      The service method is idempotent disk-truth roll-forward of state
      the user already committed (spec §2.3); a read-only command
      surfacing that state cleanly is better than refusing. The service
      method owns its own ``mark_completed`` call (service.py:1265) —
      the hook MUST NOT replicate it (a second mark_completed would
      raise on the already-completed record).
    - In-flight `_InitOp` / `_RescanOp`:
        - ``allow_mutation=False`` (status / list / which / tools_main):
          prints the recovery hint and raises ``typer.Exit(code=3)``.
          ``handle_errors`` does not catch ``typer.Exit``, so the exit
          code propagates correctly.
        - ``allow_mutation=True`` (init / rescan / use / save / create /
          rename / delete / uninstall / unmanage / prune / tools_scaffold):
          raises ``InitInProgressError`` / ``RescanInProgressError``.
          The init / rescan callbacks special-case these around their
          ``--continue`` / ``--abort`` dispatch in later PRs; every
          other mutating callback lets the error bubble to
          ``handle_errors`` (stderr + exit 1).

    Spec §2.2.
    """
    deps.oplog.vacuum_completed()
    in_flight = deps.oplog.read_in_flight()
    if in_flight is None:
        return
    if isinstance(in_flight, _RenameOp):
        deps.service._compensate_rename(in_flight)  # pyright: ignore[reportPrivateUsage]
        return
    if not allow_mutation:
        console.print(_format_in_progress_hint(in_flight))
        raise typer.Exit(code=3)
    if isinstance(in_flight, _InitOp):
        raise InitInProgressError(_format_in_progress_hint(in_flight))
    # OpLogRecord is the discriminated union of _InitOp | _RenameOp | _RescanOp;
    # only _RescanOp remains here. No fallback assertion — basedpyright would
    # flag the redundant isinstance and an unreachable branch is dead code.
    raise RescanInProgressError(_format_in_progress_hint(in_flight))


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
    _detect_or_compensate_oplog(deps, allow_mutation=False)
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
    _detect_or_compensate_oplog(deps, allow_mutation=False)
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
    # Hermes blocker: surface StateAlreadyInitializedError BEFORE any
    # flag-specific detection / prompting runs. Without this preflight,
    # `init --skip claude` on an already-initialized repo reaches the
    # service layer's StateAlreadyInitialized check ONLY if the
    # CLI-level "every detected tool" / "nothing to initialize" guards
    # don't fire first — and `init --interactive` would even prompt
    # the user before failing. The state-invariant takes priority over
    # filter validation; emitting the same error for every init variant
    # keeps the CLI surface consistent.
    _detect_or_compensate_oplog(deps, allow_mutation=True)
    if deps.store.list():
        raise StateAlreadyInitializedError("switcher is already initialized")

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
            # Distinguish "no installed tools to prompt for" from "user
            # declined every prompt" (Hermes nit). Without this branch
            # the zero-detected case shows the misleading "every detected
            # tool was skipped" message.
            if not declined:
                raise NothingToInitializeError("no installed tools detected; nothing to initialize")
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
        # Also catch the more common case (Hermes review): --skip excluded
        # every DETECTED tool, even though target_ids still contains
        # registered-but-not-installed tools. Without this branch the call
        # falls through to service.init() and raises the generic
        # "no requested tools are installed" — accurate for --only, but
        # misleading for --skip. Surface the real cause here instead.
        detected_ids = {t.id for t in deps.service.detect_installed()}
        if not (set(target_ids) & detected_ids):
            raise NothingToInitializeError(
                f"--skip excluded every detected tool. Detected: "
                f"{sorted(detected_ids) or '(none)'}; after skipping "
                f"{sorted(ids)} nothing remains to initialize."
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
        help=(
            "Comma-separated tool IDs; default: all CURRENTLY-MANAGED tools the "
            "profile includes (= profile.tools intersect active.keys()). Tools "
            "previously removed via `unmanage` stay unmanaged across profile "
            "switches."
        ),
    ),
) -> None:
    """Switch a profile for the currently-managed tools.

    Defaults to switching every tool that is BOTH in the profile AND in the
    active map. After `unmanage X`, subsequent `use` calls leave X alone —
    the durability fix from v0.1.4. Pass `--only X` to restrict further.
    """
    if only is None:
        only_list = None
    else:
        only_list = [t.strip() for t in only.split(",") if t.strip()]
        if not only_list:
            raise typer.BadParameter("--only must contain at least one tool id")
    deps = get_deps()
    _detect_or_compensate_oplog(deps, allow_mutation=True)
    deps.service.use(name, only_list)
    if only_list is None:
        console.print(f"Using profile {name!r} for all currently-managed tools")
    else:
        console.print(f"Using profile {name!r} for tools: {', '.join(only_list)}")


@app.command()
@handle_errors
def create(name: str) -> None:
    """Create a new profile, seeding credentials from the current active profile."""
    deps = get_deps()
    _detect_or_compensate_oplog(deps, allow_mutation=True)
    deps.service.create(name)
    console.print(f"Created profile {name!r}")


@app.command()
@handle_errors
def save(name: str) -> None:
    """Snapshot current live config into a new profile."""
    deps = get_deps()
    _detect_or_compensate_oplog(deps, allow_mutation=True)
    deps.service.save(name)
    console.print(f"Saved live config as {name!r}")


@app.command()
@handle_errors
def rename(old: str, new: str) -> None:
    """Rename a profile. Active tools auto-relink to the new name."""
    deps = get_deps()
    _detect_or_compensate_oplog(deps, allow_mutation=True)
    deps.service.rename(old, new)
    console.print(f"Renamed {old!r} -> {new!r}")


@app.command()
@handle_errors
def delete(
    name: str,
    force: bool = typer.Option(False, "--force", help="Skip the interactive confirmation."),
) -> None:
    """Delete a profile. Refuses if the profile is active for any tool."""
    # Hook BEFORE typer.confirm: an in-flight init/rescan must surface the
    # recovery hint before the user is prompted to delete anything (Hermes
    # review). Otherwise a user who answers "y" to "Delete profile X?" only
    # then sees they have a broken state to recover first.
    deps = get_deps()
    _detect_or_compensate_oplog(deps, allow_mutation=True)
    if not force and not typer.confirm(f"Delete profile {name!r}?"):
        raise typer.Exit(code=0)
    deps.service.delete(name)
    console.print(f"Deleted profile {name!r}")


@app.command()
@handle_errors
def which(tool: str) -> None:
    """Show which profile a specific tool is currently using."""
    deps = get_deps()
    _detect_or_compensate_oplog(deps, allow_mutation=False)
    name = deps.service.which(tool)
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
    _detect_or_compensate_oplog(deps, allow_mutation=True)
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
        help="Drop an orphan tool (no registry entry, no cache) from the "
        "active map. Refuses if any owned profile subdir for the tool "
        "still has data on disk — that would be silently lost on a later "
        "`uninstall --purge`. Does NOT bypass corrupt mappings.",
    ),
) -> None:
    """Restore a single tool's live path and remove it from the active map."""
    deps = get_deps()
    _detect_or_compensate_oplog(deps, allow_mutation=True)
    report = deps.service.unmanage(tool, dry_run=dry_run, force=force)
    if dry_run:
        console.print(f"Would unmanage {report.tool_id!r}:")
        for m in report.mappings:
            if report.skipped_orphan:
                # `--force --dry-run` on an orphan-no-cache tool: the real
                # run drops the tool from the active map and leaves any
                # symlinks in place. Don't mislabel that as a no-op
                # restore (CodeRabbit blocker).
                verb = "would skip orphan (drop from active map; leave links in place)"
            elif m.state == UninstallMappingState.SYMLINK:
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
        if report.skipped_orphan and not report.mappings:
            # Orphan-no-cache produces zero mappings — surface the would-skip
            # explicitly so the dry-run output isn't an empty body.
            console.print(f"  (orphan {report.tool_id!r}: no mappings; would drop from active map)")
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
    _detect_or_compensate_oplog(deps, allow_mutation=True)

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
    _detect_or_compensate_oplog(deps, allow_mutation=True)

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
    _detect_or_compensate_oplog(deps, allow_mutation=True)
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
    pathological: bool  # True when managed AND NOT installed (any owned live path missing)
    is_orphan: bool = False  # True when in active map but has no registry entry


def _build_tools_table_rows(deps: Deps) -> list[_ToolsTableRow]:
    """Pure: registry → list of rows. Used by tools_main to populate the
    Rich table; also a clean assertion target for tests.

    `deps.store.get_active()` returns `{}` when config.json doesn't exist
    yet (pre-init), so no try/except is needed for the uninit case.
    A real StorageError (corrupt / unreadable config.json) intentionally
    propagates — the broad `except Exception` we used to have masked exactly
    the failures the rest of the CLI is careful to surface (Hermes review).

    Pathological-state checks are aware of:
      - Multi-dir tools (Hermes review): a managed tool with `config_dirs[0]`
        present but a later managed live path missing is still pathological.
        `detect_installed()` only checks the first config_dir, so the per-row
        check iterates every `tool_dir(tool, i)` for managed tools.
      - Orphan active entries (Hermes review): a tool id in the active map
        but absent from the registry (e.g., after `uninstall --force` left
        a skipped tool in active). Rendered as a dedicated orphan row so
        `tools` doesn't silently hide the broken state `unmanage` is meant
        to repair.
    """
    installed_ids = {t.id for t in deps.service.detect_installed()}
    managed_ids = set(deps.store.get_active().keys())
    registry_ids = {t.id for t in deps.registry}
    rows: list[_ToolsTableRow] = []
    for tool in deps.registry:
        first_dir_installed = tool.id in installed_ids
        managed = tool.id in managed_ids
        paths = [(dm.windows_path if IS_WINDOWS else dm.posix_path) for dm in tool.config_dirs]
        if managed:
            # Multi-dir-aware: every owned live path must exist for the tool
            # to count as fully installed. Catches the case where the first
            # dir is fine but a later managed dir was deleted.
            all_live_present = deps.service.all_live_paths_present(tool)
        else:
            all_live_present = first_dir_installed
        rows.append(
            _ToolsTableRow(
                tool_id=tool.id,
                name=tool.name,
                installed=all_live_present,
                managed=managed,
                paths=paths,
                pathological=(managed and not all_live_present),
            )
        )
    # Orphan rows: in active but no registry entry. Render at the end so
    # registered tools' rows don't shift when an orphan appears, and so the
    # orphan footer below has something to anchor on.
    for tid in sorted(managed_ids - registry_ids):
        rows.append(
            _ToolsTableRow(
                tool_id=tid,
                name="(no registry entry)",
                installed=False,
                managed=True,
                paths=[],
                pathological=True,
                is_orphan=True,
            )
        )
    return rows


@tools_app.callback(invoke_without_command=True)
@handle_errors
def tools_main(ctx: typer.Context) -> None:
    """List supported tools and per-OS config paths."""
    if ctx.invoked_subcommand is not None:
        # Subcommand path: that callback (e.g. tools_scaffold) owns its own
        # op-log hook, so skip here to avoid two read_in_flight calls per
        # invocation. The early return preserves the existing fall-through
        # to the subcommand.
        return
    deps = get_deps()
    _detect_or_compensate_oplog(deps, allow_mutation=False)
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
        if row.is_orphan:
            console.print(
                f"[yellow]⚠ {row.tool_id!r} is in active map but has no registry "
                f"entry (orphan).[/yellow]\n  Restore the registry TOML, or run "
                f"'switcher unmanage {row.tool_id} --force' once any leftover "
                f"profile data is cleared."
            )
        elif row.pathological:
            console.print(
                f"[yellow]⚠ {row.tool_id!r} is in active map but its live path is missing.[/yellow]\n"
                f"  Run 'switcher unmanage {row.tool_id}' or restore the live path."
            )
