"""Verify the Windows config paths in our builtin TOMLs are well-formed.

The hermetic check (always run by CI): expand each path's env vars and
confirm none of them are left unexpanded. A leftover %VAR% token means
the builtin TOML references an env var that the runner doesn't define,
which is something the repo can fix.

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
from pathlib import Path

# IMPORTANT: keep these in sync with the `windows_path` entries in
#   src/switcher/builtins/claude.toml
#   src/switcher/builtins/copilot.toml
# The duplication is intentional: the script is a cross-check, not a
# tautology -- if the TOMLs change without this list updating, the next
# Windows run on a provisioned host will surface a MISSING result.
EXPECTATIONS = {
    "claude": [r"%USERPROFILE%\.claude"],
    "copilot": [r"%LOCALAPPDATA%\github-copilot", r"%USERPROFILE%\.copilot"],
}


def main() -> int:
    if sys.platform != "win32":
        print("SKIP: Windows only")
        return 0
    print(f"USERPROFILE = {os.environ.get('USERPROFILE')}")
    print(f"LOCALAPPDATA = {os.environ.get('LOCALAPPDATA')}")
    print()
    unexpanded_failures: list[str] = []
    for tool, paths in EXPECTATIONS.items():
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
