"""Pydantic models for switcher's domain types."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from switcher.models import (
    CredentialFile,
    DirMapping,
    Profile,
    Tool,
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


@pytest.mark.parametrize(
    "name",
    ["CON.txt", "con.json", "PRN.log", "Aux.bat", "nul.cfg", "COM1.dat", "lpt9.tmp"],
)
def test_safe_name_rejects_windows_reserved_by_stem(name: str) -> None:
    """Windows reserves device names by stem, not just exact match."""
    with pytest.raises(ValueError, match="reserved"):
        validate_safe_name(name)


@pytest.mark.parametrize("name", ["profile.", "client.A.", "v_1.2."])
def test_safe_name_rejects_trailing_dot(name: str) -> None:
    """Windows disallows filenames with trailing dots."""
    with pytest.raises(ValueError):
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
        "C:foo",  # Windows drive-relative — not is_absolute(), but unsafe
        "\\Windows\\foo",  # Windows root-of-current-drive — not is_absolute()
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


# ---------------- Tool ----------------


def _claude_tool() -> Tool:
    return Tool(
        id="claude",
        name="Claude Code",
        config_dirs=(
            DirMapping(
                posix_path="~/.claude",
                windows_path="%USERPROFILE%\\.claude",
                profile_subdir="claude",
                env_override="CLAUDE_CONFIG_DIR",
            ),
        ),
        credentials=(CredentialFile(config_dir="claude", path=".credentials.json"),),
    )


def test_tool_basic() -> None:
    tool = _claude_tool()
    assert tool.id == "claude"
    assert len(tool.config_dirs) == 1
    assert tool.config_dirs[0].env_override == "CLAUDE_CONFIG_DIR"
    assert tool.credentials[0].path == ".credentials.json"


def test_tool_id_is_validated() -> None:
    with pytest.raises(ValidationError):
        Tool(
            id="../escape",
            name="x",
            config_dirs=(
                DirMapping(
                    posix_path="~/.x",
                    windows_path="%USERPROFILE%\\.x",
                    profile_subdir="x",
                ),
            ),
        )


def test_tool_credential_must_reference_known_config_dir() -> None:
    with pytest.raises(ValidationError, match="unknown config_dir"):
        Tool(
            id="claude",
            name="Claude Code",
            config_dirs=(
                DirMapping(
                    posix_path="~/.claude",
                    windows_path="%USERPROFILE%\\.claude",
                    profile_subdir="claude",
                ),
            ),
            credentials=(CredentialFile(config_dir="other", path="foo"),),
        )


def test_tool_shorthand_credential_files_expand() -> None:
    """A flat credential_files list should expand to CredentialFile entries
    pointing at the first config_dir."""
    tool = Tool.model_validate(
        {
            "id": "claude",
            "name": "Claude Code",
            "credential_files": [".credentials.json"],
            "config_dirs": [
                {
                    "posix_path": "~/.claude",
                    "windows_path": "%USERPROFILE%\\.claude",
                    "profile_subdir": "claude",
                }
            ],
        }
    )
    assert len(tool.credentials) == 1
    assert tool.credentials[0].config_dir == "claude"
    assert tool.credentials[0].path == ".credentials.json"


def test_tool_shorthand_works_with_dirmapping_instances() -> None:
    """Python callers can pass DirMapping instances directly; shorthand
    expansion must handle both raw dicts (TOML path) and instances."""
    tool = Tool.model_validate(
        {
            "id": "claude",
            "name": "Claude Code",
            "credential_files": [".credentials.json"],
            "config_dirs": [
                DirMapping(
                    posix_path="~/.claude",
                    windows_path="%USERPROFILE%\\.claude",
                    profile_subdir="claude",
                )
            ],
        }
    )
    assert tool.credentials[0].config_dir == "claude"
    assert tool.credentials[0].path == ".credentials.json"


def test_tool_shorthand_does_not_mutate_caller_input() -> None:
    """The mode='before' validator must not pop keys from the caller's dict.
    Re-using the same payload twice should still expand both times."""
    payload = {
        "id": "claude",
        "name": "Claude Code",
        "credential_files": [".credentials.json"],
        "config_dirs": [
            {
                "posix_path": "~/.claude",
                "windows_path": "%USERPROFILE%\\.claude",
                "profile_subdir": "claude",
            }
        ],
    }
    Tool.model_validate(payload)
    # Caller dict still carries the shorthand key, untouched.
    assert payload.get("credential_files") == [".credentials.json"]
    # Validating again from the same dict still produces credentials.
    again = Tool.model_validate(payload)
    assert again.credentials[0].path == ".credentials.json"


def test_tool_shorthand_with_tuple_explicit_credentials() -> None:
    """Python callers idiomatically pass tuple values for tuple-typed fields.
    Shorthand expansion must produce a list-compatible result so concatenating
    onto a tuple input does not raise TypeError."""
    tool = Tool.model_validate(
        {
            "id": "claude",
            "name": "Claude Code",
            "credential_files": [".credentials.json"],
            "config_dirs": (
                DirMapping(
                    posix_path="~/.claude",
                    windows_path="%USERPROFILE%\\.claude",
                    profile_subdir="claude",
                ),
            ),
            "credentials": (CredentialFile(config_dir="claude", path="other.json"),),
        }
    )
    assert [c.path for c in tool.credentials] == [".credentials.json", "other.json"]


def test_tool_shorthand_and_explicit_credentials_coexist() -> None:
    """Both forms can appear; shorthand entries are listed first."""
    tool = Tool.model_validate(
        {
            "id": "copilot",
            "name": "GitHub Copilot CLI",
            "credential_files": ["x.json"],
            "config_dirs": [
                {
                    "posix_path": "~/.config/github-copilot",
                    "windows_path": "%LOCALAPPDATA%\\github-copilot",
                    "profile_subdir": "copilot-auth",
                },
                {
                    "posix_path": "~/.copilot",
                    "windows_path": "%USERPROFILE%\\.copilot",
                    "profile_subdir": "copilot-config",
                },
            ],
            "credentials": [{"config_dir": "copilot-config", "path": "y.json"}],
        }
    )
    assert [c.config_dir for c in tool.credentials] == ["copilot-auth", "copilot-config"]
    assert [c.path for c in tool.credentials] == ["x.json", "y.json"]


# ---------------- Profile ----------------


def test_profile_basic() -> None:
    p = Profile(
        name="vanilla",
        created_at=datetime(2026, 5, 4, 13, 42, 11, tzinfo=UTC),
        tools={"claude": True, "copilot": True},
    )
    assert p.name == "vanilla"
    assert p.tools["claude"] is True


def test_profile_round_trips_json_with_camel_alias() -> None:
    """Serializing uses createdAt (camel), deserializing accepts both."""
    p = Profile(
        name="vanilla",
        created_at=datetime(2026, 5, 4, 13, 42, 11, tzinfo=UTC),
        tools={"claude": True},
    )
    raw = p.model_dump_json(by_alias=True)
    assert '"createdAt"' in raw
    assert '"2026-05-04T13:42:11Z"' in raw
    restored = Profile.model_validate_json(raw)
    assert restored == p


def test_profile_accepts_either_field_form() -> None:
    """Reading metadata.json should work whether the file uses createdAt or
    created_at (we always write the camel form, but be permissive on read)."""
    raw = '{"name": "vanilla", "createdAt": "2026-05-04T13:42:11Z", "tools": {"claude": true}}'
    p = Profile.model_validate_json(raw)
    assert p.created_at == datetime(2026, 5, 4, 13, 42, 11, tzinfo=UTC)


def test_profile_accepts_snake_case_on_read() -> None:
    """The docstring promises snake_case is also accepted on read."""
    raw = '{"name": "vanilla", "created_at": "2026-05-04T13:42:11Z", "tools": {"claude": true}}'
    p = Profile.model_validate_json(raw)
    assert p.created_at == datetime(2026, 5, 4, 13, 42, 11, tzinfo=UTC)


def test_profile_name_validated() -> None:
    with pytest.raises(ValidationError):
        Profile(
            name="../escape",
            created_at=datetime(2026, 5, 4, tzinfo=UTC),
            tools={},
        )


def test_profile_rejects_naive_datetime() -> None:
    """Naive datetimes silently shift on astimezone(UTC) — reject them so the
    written timestamp can never disagree with the wall clock the user typed."""
    with pytest.raises(ValidationError, match="timezone"):
        Profile(
            name="vanilla",
            created_at=datetime(2026, 5, 4, 13, 42, 11),
            tools={},
        )
