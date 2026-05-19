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


def test_load_builtins_includes_claude_and_copilot() -> None:
    """Subset assertion so adding new builtins later doesn't trip the test
    on legitimate growth — we only care that the day-one set is loaded."""
    ids = {t.id for t in load_builtin_tools()}
    assert {"claude", "copilot"} <= ids


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


def test_build_registry_two_user_files_overriding_builtin_attribute_correctly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When two user TOMLs both target the same builtin id, the FIRST is a
    builtin-override and the SECOND is a duplicate-user — the immediate
    conflict for the second file is with the earlier user file, not the
    builtin. Misattributing both as builtin-overrides hides the real
    conflict and makes user-config debugging painful."""
    rd = tmp_path / "registry.d"
    rd.mkdir()
    a = USER_TOOL.replace('id = "gemini"', 'id = "claude"').replace('"Gemini CLI"', '"First"')
    b = USER_TOOL.replace('id = "gemini"', 'id = "claude"').replace('"Gemini CLI"', '"Second"')
    (rd / "a-first.toml").write_text(a, encoding="utf-8")
    (rd / "b-second.toml").write_text(b, encoding="utf-8")
    build_registry(rd)
    lines = capsys.readouterr().err.strip().splitlines()
    assert len(lines) == 2
    assert "overrides builtin" in lines[0]
    assert "duplicate user tool" in lines[1]


_TOOL_WITH_CF_TEMPLATE = """\
id = "{id}"
name = "{name}"

[[config_dirs]]
posix_path = "~/.{dir}"
windows_path = "%USERPROFILE%\\\\.{dir}"
profile_subdir = "{dir}"

[[config_files]]
posix_path = "{posix}"
windows_path = "{windows}"
profile_subdir = "{dir}"
profile_filename = "{filename}"
merge_strategy = "json_subtree_merge"
owned_json_paths = [".mcpServers"]
"""


def _write_cf_tool(
    rd: Path,
    filename_in_rd: str,
    *,
    tool_id: str,
    subdir: str,
    posix: str,
    windows: str,
    filename: str,
) -> None:
    (rd / filename_in_rd).write_text(
        _TOOL_WITH_CF_TEMPLATE.format(
            id=tool_id,
            name=tool_id,
            dir=subdir,
            posix=posix,
            windows=windows,
            filename=filename,
        ),
        encoding="utf-8",
    )


def test_build_registry_rejects_cross_tool_snapshot_slot_collision(
    tmp_path: Path,
) -> None:
    """abby r13: per-tool ConfigFile uniqueness doesn't catch two distinct
    tools claiming the same snapshot slot (profile_subdir + profile_filename)
    under ``.switcher/config_files/...``. The registry-level check must.
    """
    rd = tmp_path / "registry.d"
    rd.mkdir()
    _write_cf_tool(
        rd,
        "a.toml",
        tool_id="atool",
        subdir="atool",
        posix="~/.atool.json",
        windows="%USERPROFILE%\\\\.atool.json",
        filename="conf.json",
    )
    _write_cf_tool(
        rd,
        "b.toml",
        tool_id="btool",
        subdir="atool",  # same subdir → snapshot slot collision
        posix="~/.btool.json",
        windows="%USERPROFILE%\\\\.btool.json",
        filename="conf.json",
    )
    with pytest.raises(ValueError, match="snapshot slot"):
        build_registry(rd)


def test_build_registry_rejects_cross_tool_posix_path_collision(
    tmp_path: Path,
) -> None:
    """Two distinct tools claiming the same posix_path → live-file race on
    save/use. Registry-level check must catch."""
    rd = tmp_path / "registry.d"
    rd.mkdir()
    _write_cf_tool(
        rd,
        "a.toml",
        tool_id="atool",
        subdir="atool",
        posix="~/.shared.json",
        windows="%USERPROFILE%\\\\.a.json",
        filename="a.json",
    )
    _write_cf_tool(
        rd,
        "b.toml",
        tool_id="btool",
        subdir="btool",
        posix="~/.shared.json",  # same posix → live-file collision
        windows="%USERPROFILE%\\\\.b.json",
        filename="b.json",
    )
    with pytest.raises(ValueError, match="posix_path"):
        build_registry(rd)


def test_build_registry_rejects_cross_tool_windows_path_collision(
    tmp_path: Path,
) -> None:
    rd = tmp_path / "registry.d"
    rd.mkdir()
    _write_cf_tool(
        rd,
        "a.toml",
        tool_id="atool",
        subdir="atool",
        posix="~/.a.json",
        windows="%USERPROFILE%\\\\.shared.json",
        filename="a.json",
    )
    _write_cf_tool(
        rd,
        "b.toml",
        tool_id="btool",
        subdir="btool",
        posix="~/.b.json",
        windows="%USERPROFILE%\\\\.shared.json",  # same windows → collision
        filename="b.json",
    )
    with pytest.raises(ValueError, match="windows_path"):
        build_registry(rd)


def test_build_registry_cross_tool_collision_is_case_insensitive(
    tmp_path: Path,
) -> None:
    """Default NTFS / APFS are case-insensitive; ``~/.foo.json`` and
    ``~/.Foo.json`` resolve to the same file. Raw string compare would
    let them through and re-introduce the silent-clobber class on
    case-insensitive filesystems.
    """
    rd = tmp_path / "registry.d"
    rd.mkdir()
    _write_cf_tool(
        rd,
        "a.toml",
        tool_id="atool",
        subdir="atool",
        posix="~/.foo.json",
        windows="%USERPROFILE%\\\\.a.json",
        filename="a.json",
    )
    _write_cf_tool(
        rd,
        "b.toml",
        tool_id="btool",
        subdir="btool",
        posix="~/.Foo.json",  # case-variant of ~/.foo.json
        windows="%USERPROFILE%\\\\.b.json",
        filename="b.json",
    )
    with pytest.raises(ValueError, match="case-insensitive"):
        build_registry(rd)


def test_build_registry_accepts_disjoint_config_files(tmp_path: Path) -> None:
    """Sanity-check the cross-tool uniqueness validator doesn't false-
    positive on tools whose ConfigFiles touch disjoint paths."""
    rd = tmp_path / "registry.d"
    rd.mkdir()
    _write_cf_tool(
        rd,
        "a.toml",
        tool_id="atool",
        subdir="atool",
        posix="~/.atool.json",
        windows="%USERPROFILE%\\\\.atool.json",
        filename="a.json",
    )
    _write_cf_tool(
        rd,
        "b.toml",
        tool_id="btool",
        subdir="btool",
        posix="~/.btool.json",
        windows="%USERPROFILE%\\\\.btool.json",
        filename="b.json",
    )
    tools = build_registry(rd)
    assert {t.id for t in tools} >= {"atool", "btool"}


def test_find_tool_returns_none_when_missing() -> None:
    tools = load_builtin_tools()
    assert find_tool(tools, "nonexistent") is None


def test_scaffold_writes_valid_toml(tmp_path: Path) -> None:
    out = tmp_path / "registry.d" / "mytool.toml"
    scaffold_tool("mytool", out)
    content = out.read_text(encoding="utf-8")
    assert 'id = "mytool"' in content
    # Verify the scaffold itself parses (after the user fills in `name`)
    parsed = tomllib.loads(content)
    assert parsed["id"] == "mytool"


def test_scaffold_refuses_to_overwrite(tmp_path: Path) -> None:
    out = tmp_path / "exists.toml"
    out.write_text("# existing", encoding="utf-8")
    with pytest.raises(StorageError, match="overwrite"):
        scaffold_tool("mytool", out)
