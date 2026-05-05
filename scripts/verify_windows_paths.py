"""Verify the Windows config paths in our builtin TOMLs are well-formed.

Loads `windows_path` entries directly from src/switcher/builtins/*.toml so
the script can never drift from what's actually shipped: there's only one
source of truth for the expected Windows paths, the same one production
code reads.

The hermetic check (always run by CI): expand each path's env vars and
confirm none of them are left unexpanded. A leftover %VAR% token means a
shipped TOML references an env var the runner doesn't define -- something
the repo can fix.

The host-dependent check (informational only): each fully-expanded path
is also checked for existence and dir-vs-file status, but those statuses
do not affect the exit code. They depend on whether the AI tools are
actually installed on the runner under the expected user account, which
is host state -- not repo correctness -- and would otherwise make CI
fail on every fresh Windows runner.

Exits 1 only when an env var fails to expand. Run on a fully-provisioned
Windows machine (Claude Code + Copilot CLI installed) to also surface
the host-side mismatches, which are diagnostic.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

BUILTINS_DIR = Path(__file__).resolve().parent.parent / "src" / "switcher" / "builtins"


def load_expectations() -> dict[str, list[str]]:
    """Read every *.toml in the builtins dir, return {tool_id: [windows_path, ...]}.

    Raises KeyError with a file-qualified message if a TOML is missing
    `id` or a `config_dirs` entry is missing `windows_path`. We keep this
    loud (rather than skip-with-warning) since the builtin TOMLs are
    repo-shipped -- a missing key is a repo bug to surface, not host
    state to tolerate -- but the file-qualified message saves the reader
    from chasing a bare `KeyError: 'id'` traceback.
    """
    expectations: dict[str, list[str]] = {}
    for toml_path in sorted(BUILTINS_DIR.glob("*.toml")):
        with toml_path.open("rb") as f:
            data = tomllib.load(f)
        try:
            tool_id = str(data["id"])
            windows_paths = [str(d["windows_path"]) for d in data.get("config_dirs", [])]
        except KeyError as e:
            raise KeyError(f"{toml_path.name}: missing required key {e.args[0]!r}") from e
        expectations[tool_id] = windows_paths
    return expectations


def main() -> int:
    if sys.platform != "win32":
        print("SKIP: Windows only")
        return 0
    print(f"USERPROFILE = {os.environ.get('USERPROFILE')}")
    print(f"LOCALAPPDATA = {os.environ.get('LOCALAPPDATA')}")
    print()
    expectations = load_expectations()
    if not expectations:
        print(f"FAIL no builtin TOMLs found at {BUILTINS_DIR}")
        return 1
    unexpanded_failures: list[str] = []
    for tool, paths in expectations.items():
        print(f"--- {tool} ---")
        for raw in paths:
            expanded_str = os.path.expandvars(raw)
            print(f"  {raw}")
            if "%" in expanded_str:
                # expandvars leaves unknown %VAR% tokens untouched, so a path
                # like '%LOCALAPPDATA%\foo' would otherwise be reported as
                # MISSING with no hint that the env var was the actual problem.
                # This is the only failure mode the repo owns; everything below
                # depends on host install state.
                print(f"    -> FAIL: env var not expanded: {expanded_str}")
                unexpanded_failures.append(f"{tool}: unexpanded {raw}")
                continue
            expanded = Path(expanded_str)
            if not expanded.exists():
                status = "MISSING (host: tool may not be installed)"
            elif expanded.is_dir():
                status = "DIR"
            else:
                status = "FILE (expected directory; host state)"
            print(f"    -> {expanded} [{status}]")
    if unexpanded_failures:
        print()
        print(f"FAIL {len(unexpanded_failures)} env var(s) failed to expand:")
        for f in unexpanded_failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
