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
import tomllib
from pathlib import Path

import pytest

# init() picks a `YYYY-MM-DD-current` dated profile name (spec §6); the e2e
# layer asserts the canonical shape rather than the literal date so tests
# don't drift across midnight or across machines with different locales.
_DATED_PROFILE = re.compile(r"\d{4}-\d{2}-\d{2}-current")

# Discover the shipped builtin tool IDs at module load. Hardcoding "claude"
# and "copilot" would have made these tests assert repo inventory instead
# of CLI semantics: any future builtin rename / add / remove would surface
# as a CLI regression even when CLI behavior was unchanged. Reading from
# the same TOMLs production code consults keeps the test honest.
_BUILTINS_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "switcher" / "builtins"


def _shipped_tool_ids() -> list[str]:
    """Discover shipped builtin tool IDs.

    Mirrors the validation rules in verify_windows_paths.load_expectations
    (file-qualified errors for missing `id`, type-check that `id` is a
    string, reject duplicate IDs). Keeping the two helpers in lockstep --
    even with light duplication -- means an ambiguous repo state (two
    builtin TOMLs claiming the same id) fails BOTH the verifier and the
    e2e suite, so neither layer can mask a divergence.
    """
    ids: list[str] = []
    seen: set[str] = set()
    for toml_path in sorted(_BUILTINS_DIR.glob("*.toml")):
        with toml_path.open("rb") as f:
            data = tomllib.load(f)
        try:
            tool_id = data["id"]
        except KeyError as e:
            raise KeyError(f"{toml_path.name}: missing required key {e.args[0]!r}") from e
        if not isinstance(tool_id, str):
            raise TypeError(
                f"{toml_path.name}: 'id' must be a string, got {type(tool_id).__name__}"
            )
        if tool_id in seen:
            raise KeyError(
                f"{toml_path.name}: duplicate tool id {tool_id!r} "
                f"(already provided by an earlier file)"
            )
        seen.add(tool_id)
        ids.append(tool_id)
    return ids


_TOOL_IDS = _shipped_tool_ids()
assert _TOOL_IDS, f"no builtin TOMLs discovered under {_BUILTINS_DIR}"

# Split on whitespace AND table border glyphs so a tool ID can be checked
# as a discrete token rather than a substring. Without this, an assertion
# like `"claude" in r.stdout` would match a hypothetical future
# "claude-code" tool ID's row, manufacturing a false-positive coverage.
# Includes both Rich's default Unicode borders and the ASCII pipe `|` for
# environments where Rich renders the simpler table style. `-` is
# intentionally omitted -- it's a legitimate character inside tool IDs
# (e.g. "claude-code") and ASCII row separators (---+---) span whole
# lines without participating in the token assertions.
_TABLE_TOKEN_SPLIT = re.compile(r"[\s│┃┏┓┗┛━┳┻╇╋|]+")


def _tokens(text: str) -> list[str]:
    return [t for t in _TABLE_TOKEN_SPLIT.split(text) if t]


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
    tokens = _tokens(r.stdout)
    for tid in _TOOL_IDS:
        assert tid in tokens, (
            f"missing builtin {tid!r} as a discrete token in tools output: {r.stdout!r}"
        )


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
    for tool in _TOOL_IDS:
        assert any(
            tool in _tokens(line) and _DATED_PROFILE.search(line) for line in status_lines
        ), f"no dated active profile on the {tool!r} line of status: {r.stdout!r}"


def test_init_twice_errors(tmp_home: Path, tmp_state: Path) -> None:
    first = _run(["init"], tmp_home, tmp_state)
    assert first.returncode == 0, first.stderr
    r = _run(["init"], tmp_home, tmp_state)
    assert r.returncode == 1
    assert "already initialized" in r.stderr


def test_which_before_init_errors(tmp_home: Path, tmp_state: Path) -> None:
    r = _run(["which", _TOOL_IDS[0]], tmp_home, tmp_state)
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
    # Tokenize the help output for the same reason test_tools does -- a
    # short command like "use" could otherwise match prose ("use the foo
    # command") and pass even if the command itself were missing.
    tokens = _tokens(r.stdout)
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
        assert cmd in tokens, f"missing {cmd!r} as a discrete token in --help output: {r.stdout!r}"
