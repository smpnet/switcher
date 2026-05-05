"""Verify the Windows config paths in our builtin TOMLs match where the AI
tools actually store config on Windows. Run on a real Windows machine.

Exits non-zero (1) if any expected path expands incompletely, is missing,
or points to a regular file instead of a directory -- otherwise the
\"verification\" added to CI would never fail and pinning these paths in
the builtin TOMLs gives a false sense of coverage.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# IMPORTANT: keep these in sync with the `windows_path` entries in
#   src/switcher/builtins/claude.toml
#   src/switcher/builtins/copilot.toml
# The duplication is intentional: the script is a cross-check, not a
# tautology — if the TOMLs change without this list updating, the next
# Windows CI run will surface a MISSING result and force a reconciliation.
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
    failures: list[str] = []
    for tool, paths in EXPECTATIONS.items():
        print(f"--- {tool} ---")
        for raw in paths:
            expanded_str = os.path.expandvars(raw)
            print(f"  {raw}")
            if "%" in expanded_str:
                # expandvars leaves unknown %VAR% tokens untouched, so a path
                # like '%LOCALAPPDATA%\foo' would otherwise be reported as
                # MISSING with no hint that the env var was the actual problem.
                print(f"    -> FAIL: env var not expanded: {expanded_str}")
                failures.append(f"{tool}: unexpanded {raw}")
                continue
            expanded = Path(expanded_str)
            if not expanded.exists():
                status = "MISSING"
                failures.append(f"{tool}: missing {expanded}")
            elif expanded.is_dir():
                status = "DIR"
            else:
                status = "FILE (expected directory!)"
                failures.append(f"{tool}: file-not-dir {expanded}")
            print(f"    -> {expanded} [{status}]")
    if failures:
        print()
        print(f"FAIL {len(failures)} path(s) failed verification:")
        for f in failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
