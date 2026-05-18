"""Tests for the ConfigFile model and Tool.config_files plumbing."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from switcher.models import ConfigFile, DirMapping, Tool


def _claude_dir() -> DirMapping:
    return DirMapping(
        posix_path="~/.claude",
        windows_path="%USERPROFILE%\\.claude",
        profile_subdir="claude",
    )


def test_config_file_accepts_valid_fields():
    cf = ConfigFile(
        posix_path="~/.claude.json",
        windows_path="%USERPROFILE%\\.claude.json",
        profile_subdir="claude",
        profile_filename="claude.json",
        merge_strategy="json_subtree_merge",
        owned_json_paths=(".mcpServers", ".projects[].mcpServers", ".oauthAccount"),
    )
    assert cf.profile_subdir == "claude"
    assert cf.profile_filename == "claude.json"
    assert cf.owned_json_paths == (
        ".mcpServers",
        ".projects[].mcpServers",
        ".oauthAccount",
    )


def test_config_file_rejects_empty_owned_paths():
    with pytest.raises(ValidationError, match="owned_json_paths must be non-empty"):
        ConfigFile(
            posix_path="~/.claude.json",
            windows_path="%USERPROFILE%\\.claude.json",
            profile_subdir="claude",
            profile_filename="claude.json",
            merge_strategy="json_subtree_merge",
            owned_json_paths=(),
        )


def test_config_file_rejects_unsafe_profile_filename():
    with pytest.raises(ValidationError):
        ConfigFile(
            posix_path="~/.claude.json",
            windows_path="%USERPROFILE%\\.claude.json",
            profile_subdir="claude",
            profile_filename="../escape.json",
            merge_strategy="json_subtree_merge",
            owned_json_paths=(".mcpServers",),
        )


def test_config_file_rejects_unsafe_profile_subdir():
    with pytest.raises(ValidationError):
        ConfigFile(
            posix_path="~/.claude.json",
            windows_path="%USERPROFILE%\\.claude.json",
            profile_subdir=".switcher",
            profile_filename="claude.json",
            merge_strategy="json_subtree_merge",
            owned_json_paths=(".mcpServers",),
        )


def test_config_file_rejects_windows_reserved_profile_filename():
    """validate_safe_name rejects Windows-reserved stems (e.g. CON.txt)."""
    with pytest.raises(ValidationError, match="reserved"):
        ConfigFile(
            posix_path="~/.claude.json",
            windows_path="%USERPROFILE%\\.claude.json",
            profile_subdir="claude",
            profile_filename="CON.txt",
            merge_strategy="json_subtree_merge",
            owned_json_paths=(".mcpServers",),
        )


def test_config_file_rejects_trailing_dot_profile_filename():
    """validate_safe_name rejects names ending with a dot (Windows-illegal)."""
    with pytest.raises(ValidationError, match="must not end with a dot"):
        ConfigFile(
            posix_path="~/.claude.json",
            windows_path="%USERPROFILE%\\.claude.json",
            profile_subdir="claude",
            profile_filename="claude.json.",
            merge_strategy="json_subtree_merge",
            owned_json_paths=(".mcpServers",),
        )


def test_config_file_rejects_invalid_owned_json_paths_entry():
    """Parse-fail-loud at model-load time, not later at switch time."""
    with pytest.raises(ValidationError, match="invalid owned_json_paths entry"):
        ConfigFile(
            posix_path="~/.claude.json",
            windows_path="%USERPROFILE%\\.claude.json",
            profile_subdir="claude",
            profile_filename="claude.json",
            merge_strategy="json_subtree_merge",
            owned_json_paths=("mcpServers",),  # missing leading dot
        )


def test_config_file_rejects_unsupported_owned_path_token():
    with pytest.raises(ValidationError, match="invalid owned_json_paths entry"):
        ConfigFile(
            posix_path="~/.claude.json",
            windows_path="%USERPROFILE%\\.claude.json",
            profile_subdir="claude",
            profile_filename="claude.json",
            merge_strategy="json_subtree_merge",
            owned_json_paths=(".foo[?(@.bar)]",),  # filter predicate
        )


def test_config_file_rejects_iter_as_leaf_in_owned_paths():
    """``[]`` as a leaf segment has no v1 semantics; ConfigFile must reject
    at load time, not defer to the walker."""
    with pytest.raises(ValidationError, match="invalid owned_json_paths entry"):
        ConfigFile(
            posix_path="~/.claude.json",
            windows_path="%USERPROFILE%\\.claude.json",
            profile_subdir="claude",
            profile_filename="claude.json",
            merge_strategy="json_subtree_merge",
            owned_json_paths=(".projects[]",),
        )


def test_config_file_rejects_duplicate_owned_json_paths():
    with pytest.raises(ValidationError, match="duplicate owned_json_paths entry"):
        ConfigFile(
            posix_path="~/.claude.json",
            windows_path="%USERPROFILE%\\.claude.json",
            profile_subdir="claude",
            profile_filename="claude.json",
            merge_strategy="json_subtree_merge",
            owned_json_paths=(".mcpServers", ".mcpServers"),
        )


def test_config_file_rejects_prefix_overlapping_owned_paths():
    """`.projects` and `.projects[].mcpServers` overlap — the first captures
    the whole subtree, the second writes into it. Order-sensitive at extract."""
    with pytest.raises(ValidationError, match="overlap"):
        ConfigFile(
            posix_path="~/.claude.json",
            windows_path="%USERPROFILE%\\.claude.json",
            profile_subdir="claude",
            profile_filename="claude.json",
            merge_strategy="json_subtree_merge",
            owned_json_paths=(".projects", ".projects[].mcpServers"),
        )


def test_tool_accepts_config_files_referencing_an_existing_subdir():
    cf = ConfigFile(
        posix_path="~/.claude.json",
        windows_path="%USERPROFILE%\\.claude.json",
        profile_subdir="claude",
        profile_filename="claude.json",
        merge_strategy="json_subtree_merge",
        owned_json_paths=(".mcpServers",),
    )
    tool = Tool(
        id="claude",
        name="Claude Code",
        config_dirs=(_claude_dir(),),
        config_files=(cf,),
    )
    assert tool.config_files[0].profile_filename == "claude.json"


def test_tool_rejects_config_files_with_unknown_subdir():
    cf = ConfigFile(
        posix_path="~/.claude.json",
        windows_path="%USERPROFILE%\\.claude.json",
        profile_subdir="not-a-real-dir",
        profile_filename="claude.json",
        merge_strategy="json_subtree_merge",
        owned_json_paths=(".mcpServers",),
    )
    with pytest.raises(ValidationError, match="unknown config_dir"):
        Tool(
            id="claude",
            name="Claude Code",
            config_dirs=(_claude_dir(),),
            config_files=(cf,),
        )


def test_tool_defaults_config_files_to_empty_tuple():
    tool = Tool(id="claude", name="Claude Code", config_dirs=(_claude_dir(),))
    assert tool.config_files == ()
