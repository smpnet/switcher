"""End-to-end CLI tests via subprocess.

These boot a fresh Python process with `python -m switcher`, isolating env
through the same tmp_home + tmp_state fixtures.

The subprocess pins ``cwd`` to the per-test temp home (see ``_run`` below),
which means ``switcher`` must be importable via ``sys.executable``'s site-
packages -- i.e. installed in the active environment, not just present in
the repo source tree. The pixi workspace handles this via
``[pypi-dependencies] switcher = { path = ".", editable = true }``; running
these tests under bare Python without an editable install will fail with
``ModuleNotFoundError: No module named 'switcher'`` and that's by design.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

# init() picks a `YYYY-MM-DD-current` dated profile name (spec §6); the e2e
# layer asserts the canonical shape rather than the literal date so tests
# don't drift across midnight or across machines with different locales.
_DATED_PROFILE = re.compile(r"\d{4}-\d{2}-\d{2}-current")

pytestmark = pytest.mark.e2e


def _run(args: list[str], home: Path, state: Path) -> subprocess.CompletedProcess[str]:
    """Boot `python -m switcher <args>` against an isolated env.

    The conftest fixtures already set HOME/USERPROFILE/LOCALAPPDATA via
    monkeypatch and `os.environ.copy()` would inherit those, but we re-set
    every config root the codepath consults here — making this helper
    self-contained instead of implicitly relying on which fixtures the
    caller pulled in.
    """
    env = os.environ.copy()
    if sys.platform == "win32":
        env["USERPROFILE"] = str(home)
        env["LOCALAPPDATA"] = str(home / "AppData" / "Local")
        env["APPDATA"] = str(home / "AppData" / "Roaming")
        # Clear every other root expanduser / Path.home() consults. Python's
        # os.path.expanduser('~') on Windows tries HOME, then USERPROFILE,
        # then HOMEDRIVE+HOMEPATH; any of those still pointing at the real
        # runner profile would let the subprocess escape the temp home.
        # Also clear XDG_CONFIG_HOME for code paths that consult it.
        env.pop("HOME", None)
        env.pop("HOMEDRIVE", None)
        env.pop("HOMEPATH", None)
        env.pop("XDG_CONFIG_HOME", None)
    else:
        env["HOME"] = str(home)
        env.pop("XDG_CONFIG_HOME", None)
    env["SWITCHER_STATE_DIR"] = str(state)
    return subprocess.run(
        [sys.executable, "-m", "switcher", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=30,
        # Pin CWD to the per-test home so the subprocess boot path is
        # independent of whatever directory pytest happens to be invoked
        # from. switcher itself doesn't read CWD-relative paths today, but
        # asserting hermeticity at the helper saves us from later debugging
        # an "only fails in CI" mystery.
        cwd=str(home),
    )


def test_version(tmp_home: Path, tmp_state: Path) -> None:
    r = _run(["version"], tmp_home, tmp_state)
    assert r.returncode == 0, r.stderr
    assert "switcher" in r.stdout


def test_tools(tmp_home: Path, tmp_state: Path) -> None:
    r = _run(["tools"], tmp_home, tmp_state)
    assert r.returncode == 0, r.stderr
    assert "claude" in r.stdout
    assert "copilot" in r.stdout


def test_init_then_status(tmp_home: Path, tmp_state: Path) -> None:
    r = _run(["init"], tmp_home, tmp_state)
    assert r.returncode == 0, r.stderr
    # init announces the dated profile it created
    assert _DATED_PROFILE.search(r.stdout), f"init didn't announce a dated profile: {r.stdout!r}"

    r = _run(["status"], tmp_home, tmp_state)
    assert r.returncode == 0, r.stderr
    # Each registered tool must appear on a line that ALSO carries a dated
    # active-profile name. A bare-substring approach would have passed even
    # on a status output where one tool's row had no active profile, since
    # the same dated string from the other row would satisfy a global
    # search. Per-line enforcement is what the comment actually claims.
    status_lines = r.stdout.splitlines()
    for tool in ("claude", "copilot"):
        assert any(tool in line and _DATED_PROFILE.search(line) for line in status_lines), (
            f"no dated active profile on the {tool!r} line of status: {r.stdout!r}"
        )


def test_init_twice_errors(tmp_home: Path, tmp_state: Path) -> None:
    first = _run(["init"], tmp_home, tmp_state)
    assert first.returncode == 0, first.stderr
    r = _run(["init"], tmp_home, tmp_state)
    assert r.returncode == 1
    assert "already initialized" in r.stderr


def test_which_before_init_errors(tmp_home: Path, tmp_state: Path) -> None:
    r = _run(["which", "claude"], tmp_home, tmp_state)
    assert r.returncode == 1
    assert "not been initialized" in r.stderr


def test_use_unknown_profile_errors(tmp_home: Path, tmp_state: Path) -> None:
    first = _run(["init"], tmp_home, tmp_state)
    assert first.returncode == 0, first.stderr
    r = _run(["use", "nonexistent"], tmp_home, tmp_state)
    assert r.returncode == 1
    assert "not found" in r.stderr


def test_help_lists_commands(tmp_home: Path, tmp_state: Path) -> None:
    r = _run(["--help"], tmp_home, tmp_state)
    assert r.returncode == 0, r.stderr
    for cmd in [
        "init",
        "use",
        "list",
        "status",
        "create",
        "save",
        "rename",
        "delete",
        "tools",
        "which",
        "version",
    ]:
        assert cmd in r.stdout, f"missing {cmd!r} in --help output"
