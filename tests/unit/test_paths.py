"""Path resolution and per-OS expansion."""

import sys
from pathlib import Path

import pytest

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
