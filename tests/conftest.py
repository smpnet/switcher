"""Shared fixtures.

`tmp_home`: tmp_path with the live config dirs pre-seeded; HOME (POSIX) or
USERPROFILE (Windows) pointed at it.

`tmp_state`: SWITCHER_STATE_DIR pointed at <tmp_path>/state; XDG_DATA_HOME and
LOCALAPPDATA cleared for belt-and-suspenders isolation.
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
    if IS_WINDOWS:
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
        (home / ".claude").mkdir()
        (home / "AppData" / "Local" / "github-copilot").mkdir(parents=True)
        (home / ".copilot").mkdir()
    else:
        monkeypatch.setenv("HOME", str(home))
        # On POSIX `Path.home()` reads HOME first; clear XDG_CONFIG_HOME so
        # ~/.config is the unambiguous default for github-copilot's first dir.
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        (home / ".claude").mkdir()
        (home / ".config" / "github-copilot").mkdir(parents=True)
        (home / ".copilot").mkdir()
    return home


@pytest.fixture
def tmp_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("SWITCHER_STATE_DIR", str(state))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    return state
