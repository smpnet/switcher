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
