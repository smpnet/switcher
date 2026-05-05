"""Verify the Windows config paths in our builtin TOMLs are well-formed.

Loads `windows_path` entries directly from src/switcher/builtins/*.toml so
the script can never drift from what's actually shipped: there's only one
source of truth for the expected Windows paths, the same one production
code reads.

Two modes, selected by the --strict flag:

Default (hermetic only): expand each path's env vars and confirm none are
left unexpanded. A leftover %VAR% token means a shipped TOML references an
env var the runner doesn't define -- something the repo can fix. MISSING /
FILE statuses on the expanded paths are reported but informational; the
exit code only reflects unexpanded vars. This is what hosted CI runs --
no provisioned-tool assumption.

Strict (--strict): same hermetic check PLUS the host-state check is
gating. Missing-directory and file-not-dir statuses also fail the run.
This is the mode the self-hosted Windows runner uses to assert it's been
provisioned with Claude Code / Copilot CLI under the expected accounts;
without --strict the self-hosted job would just rerun what hosted CI
already covers.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tomllib
from pathlib import Path

# Match an unresolved Windows env-var placeholder (e.g. `%LOCALAPPDATA%`).
# Bare `"%" in expanded_str` would also flag legitimate filenames containing
# a literal percent sign; this regex matches the placeholder shape only.
_UNEXPANDED_VAR = re.compile(r"%[^%]+%")

BUILTINS_DIR = Path(__file__).resolve().parent.parent / "src" / "switcher" / "builtins"


def load_expectations() -> dict[str, list[str]]:
    """Read every *.toml in the builtins dir, return {tool_id: [windows_path, ...]}.

    `config_dirs[*].windows_path` is treated as OPTIONAL: a builtin
    intentionally targeting POSIX-only tools can omit it. Such tools land
    in the result with an empty list and main() reports them as
    `(POSIX-only)` rather than failing CI for a non-bug.

    Raises KeyError with a file-qualified message if a TOML is missing
    `id` or `config_dirs`, or if `config_dirs` is empty (every builtin
    needs at least one config dir to be useful). We keep this loud
    (rather than skip-with-warning) since the builtin TOMLs are
    repo-shipped -- a missing key is a repo bug to surface, not host
    state to tolerate -- but the file-qualified message saves the reader
    from chasing a bare `KeyError: 'id'` traceback.
    """
    expectations: dict[str, list[str]] = {}
    for toml_path in sorted(BUILTINS_DIR.glob("*.toml")):
        with toml_path.open("rb") as f:
            data = tomllib.load(f)
        try:
            tool_id_raw = data["id"]
            config_dirs = data["config_dirs"]
        except KeyError as e:
            raise KeyError(f"{toml_path.name}: missing required key {e.args[0]!r}") from e
        # Reject non-string `id` instead of silently coercing via str(). A
        # malformed `id = []` or `id = 42` would otherwise produce nonsense
        # tool keys like "[]" or "42" that pass downstream checks.
        if not isinstance(tool_id_raw, str):
            raise TypeError(
                f"{toml_path.name}: 'id' must be a string, "
                f"got {type(tool_id_raw).__name__}"
            )
        tool_id = tool_id_raw
        if not isinstance(config_dirs, list):
            # E.g. someone wrote `config_dirs = "claude"` instead of
            # `[[config_dirs]]`. Without this check the for-loop below would
            # raise an unrelated TypeError that doesn't name the file.
            raise TypeError(
                f"{toml_path.name}: 'config_dirs' must be a list of tables, "
                f"got {type(config_dirs).__name__}"
            )
        if not config_dirs:
            # An empty `config_dirs` list (or one that defaulted to []) would
            # otherwise turn this script into a tautology for that builtin --
            # zero windows_paths means zero checks, which always reports green.
            # Every shipped builtin requires at least one config_dirs entry.
            raise KeyError(
                f"{toml_path.name}: 'config_dirs' is empty "
                f"(every builtin requires at least one config dir)"
            )
        windows_paths: list[str] = []
        for i, entry in enumerate(config_dirs):
            # Each config_dirs entry must be a [[config_dirs]] table. A
            # malformed value like `config_dirs = ["foo"]` (list of strings)
            # would otherwise fall into the comprehension below and raise a
            # generic TypeError that doesn't name the file or the index.
            if not isinstance(entry, dict):
                raise TypeError(
                    f"{toml_path.name}: config_dirs[{i}] must be a table, "
                    f"got {type(entry).__name__}"
                )
            # windows_path is optional (POSIX-only tools leave it out), but
            # if present it must be a string -- reject non-string values
            # rather than silently coerce, same reason as `id` above.
            if "windows_path" in entry:
                wp = entry["windows_path"]
                if not isinstance(wp, str):
                    raise TypeError(
                        f"{toml_path.name}: config_dirs[{i}].windows_path "
                        f"must be a string, got {type(wp).__name__}"
                    )
                windows_paths.append(wp)
        if tool_id in expectations:
            # Two builtin TOMLs claiming the same id would otherwise silently
            # overwrite, masking one of them and letting CI report a false pass.
            raise KeyError(
                f"{toml_path.name}: duplicate tool id {tool_id!r} "
                f"(already provided by an earlier file)"
            )
        expectations[tool_id] = windows_paths
    return expectations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Also fail on MISSING / FILE host-state results (use on a "
            "self-hosted runner provisioned with the expected tool installs)."
        ),
    )
    args = parser.parse_args()

    if sys.platform != "win32":
        print("SKIP: Windows only")
        return 0
    # Echo every Windows config root we know about, including APPDATA --
    # the e2e helper normalizes that one too, so a future builtin that
    # references %APPDATA% would otherwise hit a FAIL with no nearby
    # context for why the var resolved (or didn't) the way it did.
    print(f"USERPROFILE = {os.environ.get('USERPROFILE')}")
    print(f"LOCALAPPDATA = {os.environ.get('LOCALAPPDATA')}")
    print(f"APPDATA = {os.environ.get('APPDATA')}")
    print(f"strict mode = {args.strict}")
    print()
    expectations = load_expectations()
    if not expectations:
        print(f"FAIL no builtin TOMLs found at {BUILTINS_DIR}")
        return 1
    unexpanded_failures: list[str] = []
    host_state_failures: list[str] = []
    for tool, paths in expectations.items():
        print(f"--- {tool} ---")
        if not paths:
            # Builtin declares no windows_path entries -- POSIX-only tool.
            # Print a marker so the report shows we considered it; nothing
            # to verify on the Windows side.
            print("  (POSIX-only -- no windows_path declared)")
            continue
        for raw in paths:
            expanded_str = os.path.expandvars(raw)
            print(f"  {raw}")
            if _UNEXPANDED_VAR.search(expanded_str):
                # expandvars leaves unknown %VAR% tokens untouched, so a path
                # like '%LOCALAPPDATA%\foo' would otherwise be reported as
                # MISSING with no hint that the env var was the actual problem.
                # Always a failure regardless of --strict -- this is repo-owned.
                print(f"    -> FAIL: env var not expanded: {expanded_str}")
                unexpanded_failures.append(f"{tool}: unexpanded {raw}")
                continue
            expanded = Path(expanded_str)
            if not expanded.exists():
                status = "MISSING (host: tool may not be installed)"
                host_state_failures.append(f"{tool}: missing {expanded}")
            elif expanded.is_dir():
                status = "DIR"
            else:
                status = "FILE (expected directory; host state)"
                host_state_failures.append(f"{tool}: file-not-dir {expanded}")
            print(f"    -> {expanded} [{status}]")
    rc = 0
    if unexpanded_failures:
        print()
        print(f"FAIL {len(unexpanded_failures)} env var(s) failed to expand:")
        for f in unexpanded_failures:
            print(f"  - {f}")
        rc = 1
    if host_state_failures:
        print()
        if args.strict:
            print(
                f"FAIL {len(host_state_failures)} host-state issue(s) "
                f"(--strict): runner is not provisioned as expected:"
            )
            rc = 1
        else:
            print(
                f"NOTE {len(host_state_failures)} host-state issue(s) "
                f"(informational; pass --strict to fail on these):"
            )
        for f in host_state_failures:
            print(f"  - {f}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
