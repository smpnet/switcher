"""Path resolution and per-OS expansion."""

import sys
from pathlib import Path

import pytest

from switcher.models import DirMapping, Tool
from switcher.paths import IS_WINDOWS, PathResolver


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def resolver(home: Path) -> PathResolver:
    return PathResolver(home=home)


def test_home_returns_configured_dir(resolver: PathResolver, home: Path) -> None:
    assert resolver.home() == home


def test_expand_handles_tilde(resolver: PathResolver, home: Path) -> None:
    assert resolver.expand("~/foo/bar") == home / "foo" / "bar"


def test_expand_handles_bare_tilde(resolver: PathResolver, home: Path) -> None:
    assert resolver.expand("~") == home


def test_expand_rejects_other_user_tilde(resolver: PathResolver) -> None:
    """POSIX ~username expands to another user's home; we do not support that
    and would otherwise treat 'otheruser/foo' as a path under self._home."""
    with pytest.raises(ValueError, match="username"):
        resolver.expand("~otheruser/foo")


def test_expand_passes_absolute_through(resolver: PathResolver) -> None:
    if IS_WINDOWS:
        assert resolver.expand("C:\\Windows") == Path("C:\\Windows")
    else:
        assert resolver.expand("/etc/passwd") == Path("/etc/passwd")


def test_expand_resolves_env_vars(
    resolver: PathResolver, monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    monkeypatch.setenv("FOO", str(home / "data"))
    if IS_WINDOWS:
        assert resolver.expand("%FOO%\\sub") == home / "data" / "sub"
    else:
        assert resolver.expand("$FOO/sub") == home / "data" / "sub"


def test_exists_true_for_existing(resolver: PathResolver, home: Path) -> None:
    p = home / "exists"
    p.mkdir()
    assert resolver.exists(p)


def test_exists_false_for_missing(resolver: PathResolver, home: Path) -> None:
    assert not resolver.exists(home / "missing")


def test_state_dir_uses_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SWITCHER_STATE_DIR", str(tmp_path / "state"))
    r = PathResolver(home=tmp_path)
    assert r.state_dir() == tmp_path / "state"


def test_state_dir_env_override_expands_against_injected_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SWITCHER_STATE_DIR must expand `~` against the injected home, not the
    process user's real home — otherwise the test/override contract leaks."""
    monkeypatch.setenv("SWITCHER_STATE_DIR", "~/state")
    r = PathResolver(home=tmp_path)
    assert r.state_dir() == tmp_path / "state"


def test_state_dir_falls_back_to_platformdirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SWITCHER_STATE_DIR", raising=False)
    r = PathResolver(home=tmp_path)
    sd = r.state_dir()
    # Cross-platform: just assert the dirname is "switcher"
    assert sd.name == "switcher"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink test")
def test_is_link_detects_posix_symlink(resolver: PathResolver, home: Path) -> None:
    target = home / "target"
    target.mkdir()
    link = home / "link"
    link.symlink_to(target)
    assert resolver.is_link(link)
    assert not resolver.is_link(target)


def _two_dir_tool() -> Tool:
    return Tool(
        id="copilot",
        name="GitHub Copilot CLI",
        config_dirs=(
            DirMapping(
                posix_path="~/.config/github-copilot",
                windows_path="%LOCALAPPDATA%\\github-copilot",
                profile_subdir="copilot-auth",
                env_override="GH_COPILOT_AUTH",
            ),
            DirMapping(
                posix_path="~/.copilot",
                windows_path="%USERPROFILE%\\.copilot",
                profile_subdir="copilot-config",
            ),
        ),
    )


def test_tool_dir_no_env_override_returns_expanded_dir(
    resolver: PathResolver, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GH_COPILOT_AUTH", raising=False)
    tool = _two_dir_tool()
    if IS_WINDOWS:
        # On Windows, the test expands %LOCALAPPDATA% — set it to home for determinism.
        monkeypatch.setenv("LOCALAPPDATA", str(home))
        assert resolver.tool_dir(tool, 0) == home / "github-copilot"
    else:
        assert resolver.tool_dir(tool, 0) == home / ".config" / "github-copilot"


def test_tool_dir_env_override_wins(
    resolver: PathResolver, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GH_COPILOT_AUTH", str(tmp_path / "custom"))
    tool = _two_dir_tool()
    assert resolver.tool_dir(tool, 0) == tmp_path / "custom"


def test_tool_dir_per_mapping_isolation(
    resolver: PathResolver, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, home: Path
) -> None:
    """The env override only affects the dir whose mapping declares it.

    Multi-dir tools must not collapse to a single env path — that was the
    latent bug that motivated the per-DirMapping move.
    """
    monkeypatch.setenv("GH_COPILOT_AUTH", str(tmp_path / "custom"))
    if IS_WINDOWS:
        monkeypatch.setenv("USERPROFILE", str(home))
    tool = _two_dir_tool()
    dir0 = resolver.tool_dir(tool, 0)
    dir1 = resolver.tool_dir(tool, 1)
    assert dir0 == tmp_path / "custom"
    assert dir0 != dir1
    # Both branches resolve to the same value: POSIX expands ~ against home;
    # Windows expands %USERPROFILE% (also pinned to home) for parity.
    assert dir1 == home / ".copilot"


def test_tool_dir_env_override_expands_env_vars(
    resolver: PathResolver, monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    """An env-var override value must obey the same expansion rules as
    posix_path/windows_path entries — host-platform env-var syntax against
    the configured home. Otherwise overrides like
    `GH_COPILOT_AUTH=$XDG_CONFIG_HOME/foo` (POSIX) or
    `%LOCALAPPDATA%\\foo` (Windows) are treated as literal paths."""
    if IS_WINDOWS:
        monkeypatch.setenv("LOCALAPPDATA", str(home / "appdata"))
        monkeypatch.setenv("GH_COPILOT_AUTH", "%LOCALAPPDATA%\\custom")
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
        monkeypatch.setenv("GH_COPILOT_AUTH", "$XDG_CONFIG_HOME/custom")
    tool = _two_dir_tool()
    expected = (home / "appdata" / "custom") if IS_WINDOWS else (home / "xdg" / "custom")
    assert resolver.tool_dir(tool, 0) == expected


def test_tool_dir_env_override_expands_tilde(
    resolver: PathResolver, monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    """`~` in an override must resolve against the injected home, not the
    process user's real home — so test/CLI overrides stay sandboxed."""
    monkeypatch.setenv("GH_COPILOT_AUTH", "~/custom")
    tool = _two_dir_tool()
    assert resolver.tool_dir(tool, 0) == home / "custom"


def test_tool_dir_env_override_rejects_other_user_tilde(
    resolver: PathResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`~username` is rejected by `expand()` — overrides must inherit that
    rejection so a misconfigured env var fails loudly, not silently."""
    monkeypatch.setenv("GH_COPILOT_AUTH", "~otheruser/foo")
    tool = _two_dir_tool()
    with pytest.raises(ValueError, match="username"):
        resolver.tool_dir(tool, 0)


def test_tool_dir_treats_empty_env_override_as_unset(
    resolver: PathResolver, monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    """An explicitly-empty env var falls back to the default mapping path —
    matches the docstring's "set to a non-empty value" contract and the
    common shell convention that empty == unset."""
    monkeypatch.setenv("GH_COPILOT_AUTH", "")
    if IS_WINDOWS:
        monkeypatch.setenv("LOCALAPPDATA", str(home))
        assert resolver.tool_dir(_two_dir_tool(), 0) == home / "github-copilot"
    else:
        assert resolver.tool_dir(_two_dir_tool(), 0) == home / ".config" / "github-copilot"
