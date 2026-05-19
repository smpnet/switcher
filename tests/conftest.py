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

from switcher.registry import load_builtin_tools

IS_WINDOWS = sys.platform == "win32"


@pytest.fixture
def clear_builtin_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear every registered builtin's env_override env var.

    Derives the list from `load_builtin_tools()` so adding a new builtin
    with an env_override auto-extends fixture hermeticity. A developer
    shell with CODEX_HOME / CLAUDE_CONFIG_DIR / <future-builtin>_HOME set
    can't escape the temp home or detect a non-temp live config. Hosted
    CI runners don't typically have these set; this hardens against
    developer-local test runs.
    """
    for tool in load_builtin_tools():
        for dm in tool.config_dirs:
            if dm.env_override:
                monkeypatch.delenv(dm.env_override, raising=False)


@pytest.fixture
def tmp_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clear_builtin_env_overrides: None,
) -> Path:
    home = tmp_path / "home"
    home.mkdir()
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
