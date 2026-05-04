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

from switcher.models import Tool

IS_WINDOWS = sys.platform == "win32"


class PathResolver:
    def __init__(self, home: Path | None = None) -> None:
        self._home = Path(home) if home is not None else Path.home()

    def home(self) -> Path:
        return self._home

    def expand(self, path: str) -> Path:
        """Expand env vars and tildes against the configured home.

        Uses **the host platform's** env-var syntax: `$VAR` on POSIX, `%VAR%`
        on Windows (this is `os.path.expandvars`'s contract). Cross-syntax
        expansion is intentionally *not* supported — the registry already
        carries `posix_path`/`windows_path` per DirMapping and the caller
        picks the platform-correct one before passing it here, so `expand()`
        never sees foreign syntax in practice.

        Only `~` and `~/...` are accepted; the POSIX `~username` form
        (resolve another user's home) would otherwise be silently misread as
        a path segment under the configured home, so it is rejected.
        """
        expanded = os.path.expandvars(path)
        if expanded == "~":
            return self._home
        if expanded.startswith(("~/", "~\\")):
            return self._home / expanded[2:]
        if expanded.startswith("~"):
            raise ValueError(f"~username expansion is not supported: {expanded!r}")
        return Path(expanded)

    def state_dir(self) -> Path:
        """Where switcher stores its profiles + config.json.

        Two layers:
          * `SWITCHER_STATE_DIR` (test isolation + power-user override) — routed
            through `self.expand()` so `~` and `$VAR`/`%VAR%` resolve against
            the configured home, consistent with every other path the resolver
            emits.
          * Otherwise, `platformdirs.user_data_dir("switcher")` — XDG / Library /
            LOCALAPPDATA semantics. **Note:** this fallback intentionally
            consults the OS environment (XDG_DATA_HOME, %LOCALAPPDATA%, etc.),
            not the injected `home`. The injected-home contract governs `expand()`
            and the env-override branch above; tests that need a private state
            dir should set `SWITCHER_STATE_DIR`, not rely on `home=`.
        """
        env = os.environ.get("SWITCHER_STATE_DIR")
        if env:
            return self.expand(env)
        return Path(platformdirs.user_data_dir("switcher"))

    def tool_dir(self, tool: Tool, dir_index: int) -> Path:
        """Resolve where a tool's `dir_index`-th config dir lives.

        Per-DirMapping env override semantics:
          1. if config_dirs[dir_index].env_override is set AND the env var
             is present in os.environ AND its value is non-empty, route it
             through `self.expand()` — same contract as posix_path/
             windows_path entries (host-platform env vars + `~` against the
             configured home; `~username` rejected). Empty values fall back
             to the default mapping path; this matches the common shell
             convention of treating empty as unset, and avoids a footgun
             where `VAR=` would silently swallow the override.
          2. else expand the OS-appropriate field (windows_path or posix_path).

        Each DirMapping carries its own override (or none); multi-dir tools
        never collapse to a single env path. Routing overrides through
        `expand()` keeps test/CLI sandboxing consistent — same precedent as
        the `SWITCHER_STATE_DIR` handling in `state_dir()`.
        """
        mapping = tool.config_dirs[dir_index]
        if mapping.env_override:
            override = os.environ.get(mapping.env_override)
            if override:
                return self.expand(override)
        raw = mapping.windows_path if IS_WINDOWS else mapping.posix_path
        return self.expand(raw)

    def exists(self, p: Path) -> bool:
        return p.exists() or p.is_symlink()

    def is_link(self, p: Path) -> bool:
        if p.is_symlink():
            return True
        return bool(IS_WINDOWS and os.path.isjunction(p))
