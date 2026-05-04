"""Pydantic models for switcher's domain types."""

import pytest
from pydantic import ValidationError

from switcher.models import (
    CredentialFile,
    DirMapping,
    validate_credential_path,
    validate_safe_name,
)

# ---------------- validate_safe_name ----------------


@pytest.mark.parametrize("name", ["work", "client-A", "2026-05-04-current", "v_1.2"])
def test_safe_name_accepts_normal_names(name: str) -> None:
    assert validate_safe_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",  # empty
        ".hidden",  # leading dot
        "with space",  # space
        "with/slash",  # slash
        "with\\back",  # backslash
        "..",  # parent traversal
        "with\nnewline",
    ],
)
def test_safe_name_rejects_unsafe(name: str) -> None:
    with pytest.raises(ValueError):
        validate_safe_name(name)


@pytest.mark.parametrize("name", ["CON", "con", "PRN", "Aux", "NUL", "COM1", "lpt9"])
def test_safe_name_rejects_windows_reserved(name: str) -> None:
    with pytest.raises(ValueError, match="reserved"):
        validate_safe_name(name)


# ---------------- validate_credential_path ----------------


@pytest.mark.parametrize(
    "p",
    [".credentials.json", "data/secrets.json", "agent/auth.json", "apps.json"],
)
def test_credential_path_accepts_relative(p: str) -> None:
    assert validate_credential_path(p) == p


@pytest.mark.parametrize(
    "p",
    [
        "/etc/passwd",  # absolute POSIX
        "C:\\Windows\\foo",  # absolute Windows
        "~/foo",  # home-relative
        "../escape",  # parent traversal
        "a/../b",  # parent in middle
        "\\\\server\\share",  # UNC
        "",  # empty
        ".",  # current directory (no file)
    ],
)
def test_credential_path_rejects_unsafe(p: str) -> None:
    with pytest.raises(ValueError):
        validate_credential_path(p)


# ---------------- DirMapping ----------------


def test_dir_mapping_basic() -> None:
    dm = DirMapping(
        posix_path="~/.claude",
        windows_path="%USERPROFILE%\\.claude",
        profile_subdir="claude",
    )
    assert dm.profile_subdir == "claude"
    assert dm.env_override is None


def test_dir_mapping_with_env_override() -> None:
    dm = DirMapping(
        posix_path="~/.claude",
        windows_path="%USERPROFILE%\\.claude",
        profile_subdir="claude",
        env_override="CLAUDE_CONFIG_DIR",
    )
    assert dm.env_override == "CLAUDE_CONFIG_DIR"


def test_dir_mapping_rejects_unsafe_subdir() -> None:
    with pytest.raises(ValidationError):
        DirMapping(
            posix_path="~/.claude",
            windows_path="%USERPROFILE%\\.claude",
            profile_subdir="../escape",
        )


# ---------------- CredentialFile ----------------


def test_credential_file_basic() -> None:
    cf = CredentialFile(config_dir="copilot-auth", path="apps.json")
    assert cf.config_dir == "copilot-auth"
    assert cf.path == "apps.json"


def test_credential_file_rejects_absolute_path() -> None:
    with pytest.raises(ValidationError):
        CredentialFile(config_dir="claude", path="/etc/passwd")


def test_credential_file_rejects_traversal() -> None:
    with pytest.raises(ValidationError):
        CredentialFile(config_dir="claude", path="../etc/passwd")
