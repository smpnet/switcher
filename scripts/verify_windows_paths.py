"""Verify the Windows config paths in our builtin TOMLs match where the AI
tools actually store config on Windows. Run on a real Windows machine.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

EXPECTATIONS = {
    "claude": [r"%USERPROFILE%\.claude"],
    "copilot": [r"%LOCALAPPDATA%\github-copilot", r"%USERPROFILE%\.copilot"],
}


def main() -> None:
    if sys.platform != "win32":
        print("SKIP: Windows only")
        return
    print(f"USERPROFILE = {os.environ.get('USERPROFILE')}")
    print(f"LOCALAPPDATA = {os.environ.get('LOCALAPPDATA')}")
    print()
    for tool, paths in EXPECTATIONS.items():
        print(f"--- {tool} ---")
        for raw in paths:
            expanded_str = os.path.expandvars(raw)
            print(f"  {raw}")
            if "%" in expanded_str:
                # expandvars leaves unknown %VAR% tokens untouched, so a path
                # like '%LOCALAPPDATA%\foo' would otherwise be reported as
                # MISSING with no hint that the env var was the actual problem.
                print(f"    -> WARN: env var not expanded: {expanded_str}")
                continue
            expanded = Path(expanded_str)
            exists = "EXISTS" if expanded.exists() else "MISSING"
            print(f"    -> {expanded} [{exists}]")


if __name__ == "__main__":
    main()
