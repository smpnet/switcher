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
            expanded = Path(os.path.expandvars(raw))
            exists = "EXISTS" if expanded.exists() else "MISSING"
            print(f"  {raw}")
            print(f"    -> {expanded} [{exists}]")


if __name__ == "__main__":
    main()
