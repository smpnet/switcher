"""End-to-end CLI tests via subprocess.

These boot a fresh Python process with `python -m switcher`, isolating env
through the same tmp_home + tmp_state fixtures.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

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
        # Clear the POSIX-side roots too: Path.home() on Windows reads HOME
        # before USERPROFILE, and any code consulting XDG_CONFIG_HOME would
        # otherwise resolve into the runner's real user dir.
        env.pop("HOME", None)
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
    r = _run(["status"], tmp_home, tmp_state)
    assert r.returncode == 0, r.stderr
    assert "claude" in r.stdout


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
