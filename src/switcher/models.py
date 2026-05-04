"""Pydantic v2 models for switcher's domain types.

Models defined here are loaded from TOML (registry) and JSON (profile metadata),
and serialized back the same way. Field aliases keep on-disk shapes clean.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, cast

from pydantic import BaseModel, field_validator, model_validator

# ---------------------------------------------------------------------------
# Validation helpers (also exported so callers can validate raw inputs).
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")
_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)


def validate_safe_name(value: str) -> str:
    """Reject names that aren't safe to use as filesystem path segments.

    Cross-platform: rejects POSIX traversal and unsafe characters via the
    name regex; rejects Windows reserved device names by stem (e.g. CON.txt
    is reserved, not just CON) and Windows-illegal trailing dots.
    """
    if not _NAME_RE.fullmatch(value):
        raise ValueError(f"invalid name {value!r}: must match {_NAME_RE.pattern}")
    if value.endswith("."):
        raise ValueError(f"invalid name {value!r}: must not end with a dot")
    # Windows reserves device names by stem (CON.txt is also reserved)
    stem = value.split(".", 1)[0].upper()
    if stem in _RESERVED:
        raise ValueError(f"reserved name {value!r}")
    return value


def validate_credential_path(value: str) -> str:
    """Credential paths must be relative and not contain `..`.

    Cross-platform: rejects POSIX-absolute (/etc/...), Windows-absolute
    (C:\\...), UNC (\\\\server\\share), and home-relative (~/...) regardless
    of the host OS so the same registry data is portable.
    """
    if not value or value == ".":
        raise ValueError(f"credential path must name a file: {value!r}")
    if value.startswith("~"):
        raise ValueError(f"credential path must be relative: {value!r}")
    if PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute():
        raise ValueError(f"credential path must be relative: {value!r}")
    posix_parts = PurePosixPath(value).parts
    windows_parts = PureWindowsPath(value).parts
    if ".." in posix_parts or ".." in windows_parts:
        raise ValueError(f"credential path must not contain '..': {value!r}")
    return value


# ---------------------------------------------------------------------------
# DirMapping
# ---------------------------------------------------------------------------


class DirMapping(BaseModel):
    """One on-disk directory managed by a tool, plus the per-OS source paths.

    A multi-dir tool has multiple DirMappings. Each may carry its own env override
    so that env-driven relocation works correctly per dir.
    """

    posix_path: str
    windows_path: str
    profile_subdir: str
    env_override: str | None = None

    @field_validator("profile_subdir")
    @classmethod
    def _validate_subdir(cls, v: str) -> str:
        return validate_safe_name(v)


# ---------------------------------------------------------------------------
# CredentialFile
# ---------------------------------------------------------------------------


class CredentialFile(BaseModel):
    """A single credential file that should be preserved across new profiles.

    `config_dir` references the `profile_subdir` of one of the tool's config_dirs.
    `path` is relative to that config_dir.
    """

    config_dir: str
    path: str

    @field_validator("config_dir")
    @classmethod
    def _validate_config_dir(cls, v: str) -> str:
        return validate_safe_name(v)

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        return validate_credential_path(v)


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class Tool(BaseModel):
    """A managed AI-agent CLI: ID, display name, on-disk dirs, credentials.

    Loaded from TOML at startup (builtins + user). The `mode="before"` validator
    accepts the shorthand `credential_files = [...]` form and expands it into
    structured `CredentialFile` entries pointing at the first config dir.
    """

    id: str
    name: str
    config_dirs: tuple[DirMapping, ...]
    credentials: tuple[CredentialFile, ...] = ()

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        return validate_safe_name(v)

    @model_validator(mode="before")
    @classmethod
    def _expand_credential_files(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        shorthand = data.pop("credential_files", None)
        if shorthand is None:
            return data
        config_dirs = data.get("config_dirs") or []
        if not config_dirs:
            raise ValueError("credential_files shorthand requires at least one config_dir")
        first_subdir = config_dirs[0]["profile_subdir"]
        expanded: list[Any] = [{"config_dir": first_subdir, "path": p} for p in shorthand]
        existing = cast(list[Any], data.get("credentials") or [])
        data["credentials"] = expanded + existing
        return data

    @model_validator(mode="after")
    def _validate_credential_dirs(self) -> Tool:
        valid = {d.profile_subdir for d in self.config_dirs}
        for c in self.credentials:
            if c.config_dir not in valid:
                raise ValueError(
                    f"credential references unknown config_dir {c.config_dir!r}; "
                    f"expected one of {sorted(valid)}"
                )
        return self
