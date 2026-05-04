"""Cross-platform path resolution.

PathResolver is the single source of truth for:
  * home directory (overridable in tests)
  * tilde / env-var expansion (POSIX `$VAR`, Windows `%VAR%`)
  * per-OS DirMapping resolution with optional env override per dir
  * state directory (XDG/Library/LOCALAPPDATA, with SWITCHER_STATE_DIR escape)
  * link detection (symlink + Windows junction)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import platformdirs

IS_WINDOWS = sys.platform == "win32"


class PathResolver:
    def __init__(self, home: Path | None = None) -> None:
        self._home = Path(home) if home is not None else Path.home()

    def home(self) -> Path:
        return self._home

    def expand(self, path: str) -> Path:
        """Expand env vars and tildes against the configured home.

        Order: env-var expansion first, then tilde, so values like `$HOME/foo`
        and `%USERPROFILE%\\foo` work without depending on Path.home().
        """
        expanded = os.path.expandvars(path)
        if expanded.startswith("~"):
            tail = expanded[1:].lstrip("/\\")
            return (self._home / tail) if tail else self._home
        return Path(expanded)

    def state_dir(self) -> Path:
        """Where switcher stores its profiles + config.json.

        SWITCHER_STATE_DIR overrides the platformdirs default. Used for test
        isolation and as a power-user override.
        """
        env = os.environ.get("SWITCHER_STATE_DIR")
        if env:
            return Path(env).expanduser()
        return Path(platformdirs.user_data_dir("switcher"))

    def exists(self, p: Path) -> bool:
        return p.exists() or p.is_symlink()

    def is_link(self, p: Path) -> bool:
        if p.is_symlink():
            return True
        return bool(IS_WINDOWS and os.path.isjunction(p))
