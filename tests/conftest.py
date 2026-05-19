"""Shared fixtures.

`tmp_home`: tmp_path with the live config dirs pre-seeded; HOME (POSIX) or
USERPROFILE (Windows) pointed at it.

`tmp_state`: SWITCHER_STATE_DIR pointed at <tmp_path>/state; XDG_DATA_HOME
cleared as belt-and-suspenders. LOCALAPPDATA is intentionally NOT cleared —
when `tmp_home` is also active it sets LOCALAPPDATA to a per-test path that
the github-copilot resolution depends on; clearing it in `tmp_state` would
race the two fixtures and break Windows tool detection.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

IS_WINDOWS = sys.platform == "win32"


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    # Clear every builtin's env_override so a developer shell with
    # e.g. CODEX_HOME set can't escape the temp home or detect a
    # non-temp live config. Hosted CI runners don't typically have
    # these set; this hardens against developer-local test runs.
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    if IS_WINDOWS:
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
        (home / ".claude").mkdir()
        (home / "AppData" / "Local" / "github-copilot").mkdir(parents=True)
        (home / ".copilot").mkdir()
        (home / ".codex").mkdir()
    else:
        monkeypatch.setenv("HOME", str(home))
        # On POSIX `Path.home()` reads HOME first; clear XDG_CONFIG_HOME so
        # ~/.config is the unambiguous default for github-copilot's first dir.
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        (home / ".claude").mkdir()
        (home / ".config" / "github-copilot").mkdir(parents=True)
        (home / ".copilot").mkdir()
        (home / ".codex").mkdir()
    return home


@pytest.fixture
def tmp_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("SWITCHER_STATE_DIR", str(state))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    return state
