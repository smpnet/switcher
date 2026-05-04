"""Registry: loads builtins from the wheel, user TOMLs from registry.d/."""

import tomllib
from pathlib import Path

import pytest

from switcher.errors import StorageError
from switcher.registry import (
    build_registry,
    find_tool,
    load_builtin_tools,
    load_user_tools,
    scaffold_tool,
)

USER_TOOL = """\
id = "gemini"
name = "Gemini CLI"
credential_files = ["oauth_creds.json"]

[[config_dirs]]
posix_path = "~/.gemini"
windows_path = "%USERPROFILE%\\\\.gemini"
profile_subdir = "gemini"
"""


def test_load_builtins_returns_two_tools() -> None:
    tools = load_builtin_tools()
    ids = sorted(t.id for t in tools)
    assert ids == ["claude", "copilot"]


def test_load_user_tools_empty_when_no_dir(tmp_path: Path) -> None:
    assert load_user_tools(tmp_path / "missing") == ()


def test_load_user_tools_reads_files(tmp_path: Path) -> None:
    rd = tmp_path / "registry.d"
    rd.mkdir()
    (rd / "gemini.toml").write_text(USER_TOOL, encoding="utf-8")
    tools = load_user_tools(rd)
    assert len(tools) == 1
    assert tools[0].id == "gemini"


def test_load_user_tools_sorted_deterministically(tmp_path: Path) -> None:
    rd = tmp_path / "registry.d"
    rd.mkdir()
    (rd / "b.toml").write_text(USER_TOOL.replace("gemini", "btool"), encoding="utf-8")
    (rd / "a.toml").write_text(USER_TOOL.replace("gemini", "atool"), encoding="utf-8")
    ids = [t.id for t in load_user_tools(rd)]
    assert ids == ["atool", "btool"]


def test_load_user_tools_raises_on_malformed(tmp_path: Path) -> None:
    rd = tmp_path / "registry.d"
    rd.mkdir()
    (rd / "bad.toml").write_text("this is = not [valid TOML", encoding="utf-8")
    with pytest.raises(ValueError, match=r"bad\.toml"):
        load_user_tools(rd)


def test_build_registry_merges_builtins_and_user(tmp_path: Path) -> None:
    rd = tmp_path / "registry.d"
    rd.mkdir()
    (rd / "gemini.toml").write_text(USER_TOOL, encoding="utf-8")
    tools = build_registry(rd)
    ids = sorted(t.id for t in tools)
    assert ids == ["claude", "copilot", "gemini"]


def test_build_registry_user_overrides_builtin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rd = tmp_path / "registry.d"
    rd.mkdir()
    override = USER_TOOL.replace('id = "gemini"', 'id = "claude"').replace(
        '"Gemini CLI"', '"Claude (overridden)"'
    )
    (rd / "claude.toml").write_text(override, encoding="utf-8")
    tools = build_registry(rd)
    claude = find_tool(tools, "claude")
    assert claude is not None
    assert claude.name == "Claude (overridden)"
    captured = capsys.readouterr()
    assert "overrides builtin" in captured.err


def test_build_registry_user_overrides_user_emits_distinct_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When two user TOMLs collide on `id`, the warning must NOT claim the
    later one overrides a builtin — that misattributes the conflict and
    confuses debugging when no builtin is involved."""
    rd = tmp_path / "registry.d"
    rd.mkdir()
    a = USER_TOOL.replace('id = "gemini"', 'id = "shared"')
    b = USER_TOOL.replace('id = "gemini"', 'id = "shared"').replace('"Gemini CLI"', '"Second"')
    (rd / "a.toml").write_text(a, encoding="utf-8")
    (rd / "b.toml").write_text(b, encoding="utf-8")
    build_registry(rd)
    err = capsys.readouterr().err
    assert "shared" in err
    assert "overrides builtin" not in err


def test_find_tool_returns_none_when_missing() -> None:
    tools = load_builtin_tools()
    assert find_tool(tools, "nonexistent") is None


def test_scaffold_writes_valid_toml(tmp_path: Path) -> None:
    out = tmp_path / "registry.d" / "myool.toml"
    scaffold_tool("myool", out)
    content = out.read_text(encoding="utf-8")
    assert 'id = "myool"' in content
    # Verify the scaffold itself parses (after the user fills in `name`)
    parsed = tomllib.loads(content)
    assert parsed["id"] == "myool"


def test_scaffold_refuses_to_overwrite(tmp_path: Path) -> None:
    out = tmp_path / "exists.toml"
    out.write_text("# existing", encoding="utf-8")
    with pytest.raises(StorageError, match="overwrite"):
        scaffold_tool("myool", out)
