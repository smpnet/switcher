"""Pydantic models for switcher's domain types."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from switcher.models import (
    ConfigFile,
    CredentialFile,
    DirMapping,
    Profile,
    Tool,
    canonicalize_path_for_uniqueness,
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


# ---------------- canonicalize_path_for_uniqueness ----------------


@pytest.mark.parametrize(
    "spelling",
    [
        "~/.claude.json",
        "$HOME/.claude.json",
        "${HOME}/.claude.json",
        "~/.config/../.claude.json",
        "~/foo/../.claude.json",
    ],
)
def test_canonicalize_posix_home_spellings_collide(spelling: str) -> None:
    """Every spelling that runtime ``PathResolver.expand`` collapses to
    ``<home>/.claude.json`` must produce the same canonical key.
    Hermes pass-PR-4: validator-side uniqueness has to mirror runtime
    home-equivalence or two tools can silently co-manage the same file.
    """
    canonical = canonicalize_path_for_uniqueness("~/.claude.json", windows=False)
    assert canonicalize_path_for_uniqueness(spelling, windows=False) == canonical


@pytest.mark.parametrize(
    "spelling",
    [
        "~\\.claude.json",
        "%USERPROFILE%\\.claude.json",
        "%userprofile%\\.claude.json",
        "%UserProfile%\\.claude.json",
        "%USERPROFILE%\\foo\\..\\.claude.json",
    ],
)
def test_canonicalize_windows_home_spellings_collide(spelling: str) -> None:
    """Mirror of the POSIX case for Windows. ``%VAR%`` env-var lookup is
    case-insensitive at runtime so the regex match is too."""
    canonical = canonicalize_path_for_uniqueness("~\\.claude.json", windows=True)
    assert canonicalize_path_for_uniqueness(spelling, windows=True) == canonical


def test_canonicalize_distinct_paths_produce_distinct_keys() -> None:
    """Sanity check: two genuinely different paths under home produce
    different canonical keys."""
    a = canonicalize_path_for_uniqueness("~/.foo.json", windows=False)
    b = canonicalize_path_for_uniqueness("~/.bar.json", windows=False)
    assert a != b


def test_canonicalize_posix_tilde_backslash_folds_to_forward_slash() -> None:
    """Hermes pass-PR-4 blocker 2: POSIX runtime expansion accepts both
    ``~/`` AND ``~\\`` prefixes (paths.py:48 — ``expanded.startswith(("~/",
    "~\\\\"))``), so the two spellings resolve to the same live file.
    ``posixpath.normpath`` doesn't fold backslashes though, so without
    an explicit normalization step the validator key for ``~\\.claude.json``
    would differ from ``~/.claude.json`` — two tools using different
    separator spellings would slip past as distinct.
    """
    canonical = canonicalize_path_for_uniqueness("~/.claude.json", windows=False)
    assert canonicalize_path_for_uniqueness("~\\.claude.json", windows=False) == canonical


def test_canonicalize_posix_backslash_in_path_body_is_not_a_separator() -> None:
    """Hermes pass-PR-5 blocker: only the leading ``~\\`` is equivalent
    to ``~/``. ``PathResolver.expand`` strips that prefix and passes the
    remainder to ``Path()`` unchanged, so on POSIX a literal backslash
    inside the path body is a regular filename character — two such
    paths must NOT collide in the validator. Pre-fix, a global
    ``path.replace("\\\\", "/")`` collapsed them and could falsely
    reject valid registry entries as duplicate ``config_file`` paths.
    """
    with_backslash = canonicalize_path_for_uniqueness("/tmp/foo\\bar.json", windows=False)
    with_slash = canonicalize_path_for_uniqueness("/tmp/foo/bar.json", windows=False)
    assert with_backslash != with_slash


@pytest.mark.parametrize(
    "unrelated",
    [
        "$HOME_BACKUP/.cfg",
        "${HOME_DIR}/.cfg",
        "$HOMEBREW_PREFIX/.cfg",
    ],
)
def test_canonicalize_posix_home_regex_does_not_overmatch(unrelated: str) -> None:
    """Hermes pass-PR-4 blocker 3: ``$HOME_BACKUP`` and ``${HOME_DIR}``
    are NOT the user's home — they're unrelated env vars whose names
    happen to start with ``HOME``. Pre-fix, the regex matched the
    ``$HOME`` prefix and collapsed semantically distinct paths to the
    same canonical key, causing false-positive collision rejections.
    """
    home = canonicalize_path_for_uniqueness("$HOME/.cfg", windows=False)
    unrelated_key = canonicalize_path_for_uniqueness(unrelated, windows=False)
    assert unrelated_key != home


def test_canonicalize_does_not_consult_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validator-side canonicalization must be deterministic across hosts.
    Setting ``$HOME`` to something exotic at test time must NOT change
    the canonical key — the helper substitutes against a sentinel, not
    the host's actual home."""
    monkeypatch.setenv("HOME", "/some/strange/home")
    before = canonicalize_path_for_uniqueness("$HOME/.x.json", windows=False)
    monkeypatch.setenv("HOME", "/different/home")
    after = canonicalize_path_for_uniqueness("$HOME/.x.json", windows=False)
    assert before == after


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


def test_tool_rejects_multiple_config_files() -> None:
    """v0.1.5 caps config_files at 1 per tool until op-log compensation
    handles multi-file commit atomicity (plan Task 11). The two-file shape
    must be rejected at load time, with a message that points users at the
    single-CF / multiple-owned-paths workaround.
    """
    cf1 = {
        "posix_path": "~/.claude.json",
        "windows_path": "%USERPROFILE%\\.claude.json",
        "profile_subdir": "claude",
        "profile_filename": "claude.json",
        "merge_strategy": "json_subtree_merge",
        "owned_json_paths": [".mcpServers"],
    }
    cf2 = {
        "posix_path": "~/.claude-extra.json",
        "windows_path": "%USERPROFILE%\\.claude-extra.json",
        "profile_subdir": "claude",
        "profile_filename": "claude-extra.json",
        "merge_strategy": "json_subtree_merge",
        "owned_json_paths": [".extra"],
    }
    with pytest.raises(ValidationError, match="multiple config_files"):
        Tool.model_validate(
            {
                "id": "claude",
                "name": "Claude Code",
                "config_dirs": [
                    {
                        "posix_path": "~/.claude",
                        "windows_path": "%USERPROFILE%\\.claude",
                        "profile_subdir": "claude",
                    }
                ],
                "config_files": [cf1, cf2],
            }
        )


def test_tool_accepts_single_config_file() -> None:
    """Sanity-check the at-most-one validator: one entry must still pass."""
    tool = Tool(
        id="claude",
        name="Claude Code",
        config_dirs=(
            DirMapping(
                posix_path="~/.claude",
                windows_path="%USERPROFILE%\\.claude",
                profile_subdir="claude",
            ),
        ),
        config_files=(
            ConfigFile(
                posix_path="~/.claude.json",
                windows_path="%USERPROFILE%\\.claude.json",
                profile_subdir="claude",
                profile_filename="claude.json",
                merge_strategy="json_subtree_merge",
                owned_json_paths=(".mcpServers",),
            ),
        ),
    )
    assert len(tool.config_files) == 1


def test_tool_rejects_duplicate_config_dir_profile_subdir() -> None:
    """Hermes pass-PR-6: two ``config_dirs`` entries that share a
    ``profile_subdir`` both resolve to ``<profile>/<subdir>`` at
    init/use time. The first ``move_or_seed_dir`` succeeds and
    swaps the first live dir to a link; the second
    deterministically trips ``ProfileTargetExistsError`` AFTER live
    has already been mutated. Catch at registry-load time so the
    malformed entry surfaces immediately instead of mid-init.
    """
    with pytest.raises(ValidationError, match="duplicate config_dirs profile_subdir"):
        Tool.model_validate(
            {
                "id": "dupdirs",
                "name": "Dup Dirs",
                "config_dirs": [
                    {
                        "posix_path": "~/.a",
                        "windows_path": "%USERPROFILE%\\a",
                        "profile_subdir": "same",
                    },
                    {
                        "posix_path": "~/.b",
                        "windows_path": "%USERPROFILE%\\b",
                        "profile_subdir": "same",
                    },
                ],
            }
        )


def test_tool_rejects_duplicate_config_dir_profile_subdir_case_insensitive() -> None:
    """Matches the case-insensitive storage semantics on macOS / Windows:
    ``Same`` and ``SAME`` would land at the same on-disk dir."""
    with pytest.raises(ValidationError, match="duplicate config_dirs profile_subdir"):
        Tool.model_validate(
            {
                "id": "dupdirs",
                "name": "Dup Dirs",
                "config_dirs": [
                    {
                        "posix_path": "~/.a",
                        "windows_path": "%USERPROFILE%\\a",
                        "profile_subdir": "Same",
                    },
                    {
                        "posix_path": "~/.b",
                        "windows_path": "%USERPROFILE%\\b",
                        "profile_subdir": "SAME",
                    },
                ],
            }
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


def test_tool_shorthand_rejects_string_credential_files() -> None:
    """A bare string would otherwise expand to one credential per character —
    the user almost certainly meant `[".credentials.json"]`."""
    with pytest.raises(ValidationError, match="credential_files"):
        Tool.model_validate(
            {
                "id": "claude",
                "name": "Claude Code",
                "credential_files": ".credentials.json",
                "config_dirs": [
                    {
                        "posix_path": "~/.claude",
                        "windows_path": "%USERPROFILE%\\.claude",
                        "profile_subdir": "claude",
                    }
                ],
            }
        )


def test_tool_shorthand_rejects_missing_profile_subdir_on_first_dir() -> None:
    """An empty dict in config_dirs[0] would raise KeyError pre-validation —
    surface a clean ValidationError instead."""
    with pytest.raises(ValidationError, match="profile_subdir"):
        Tool.model_validate(
            {
                "id": "claude",
                "name": "Claude Code",
                "credential_files": [".credentials.json"],
                "config_dirs": [{}],
            }
        )


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


def test_profile_round_trip_stable_with_microseconds() -> None:
    """Serialization truncates to whole seconds; validation must normalize the
    same way so a Profile created with sub-second precision round-trips equal
    via JSON. Otherwise the wire format silently disagrees with in-memory state."""
    p = Profile(
        name="vanilla",
        created_at=datetime(2026, 5, 4, 13, 42, 11, 123456, tzinfo=UTC),
        tools={"claude": True},
    )
    restored = Profile.model_validate_json(p.model_dump_json(by_alias=True))
    assert restored == p
    assert p.created_at.microsecond == 0


def test_profile_round_trip_stable_with_non_utc_offset() -> None:
    """Serialization converts to UTC; validation must convert too, otherwise
    a +02:00 datetime survives in memory but reads back as UTC after JSON,
    which breaks Profile equality across the round-trip."""
    plus_two = timezone(timedelta(hours=2))
    p = Profile(
        name="vanilla",
        created_at=datetime(2026, 5, 4, 15, 42, 11, tzinfo=plus_two),
        tools={"claude": True},
    )
    # Same instant, normalized form
    assert p.created_at == datetime(2026, 5, 4, 13, 42, 11, tzinfo=UTC)
    assert p.created_at.utcoffset() == timedelta(0)
    restored = Profile.model_validate_json(p.model_dump_json(by_alias=True))
    assert restored == p


def test_profile_rejects_naive_datetime() -> None:
    """Naive datetimes silently shift on astimezone(UTC) — reject them so the
    written timestamp can never disagree with the wall clock the user typed."""
    with pytest.raises(ValidationError, match="timezone"):
        Profile(
            name="vanilla",
            created_at=datetime(2026, 5, 4, 13, 42, 11),
            tools={},
        )
