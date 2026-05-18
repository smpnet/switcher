"""Pydantic v2 models for switcher's domain types.

Models defined here are loaded from TOML (registry) and JSON (profile metadata),
and serialized back the same way. Field aliases keep on-disk shapes clean.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal, cast

from pydantic import (
    AliasChoices,
    BaseModel,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

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
    (C:\\...), UNC (\\\\server\\share), home-relative (~/...), Windows
    drive-relative (C:foo) and root-of-current-drive (\\Windows\\foo)
    regardless of the host OS, so the same registry data is portable.
    """
    if not value or value == ".":
        raise ValueError(f"credential path must name a file: {value!r}")
    if value.startswith("~"):
        raise ValueError(f"credential path must be relative: {value!r}")
    pp = PurePosixPath(value)
    pw = PureWindowsPath(value)
    # is_absolute() misses Windows drive-relative ("C:foo") and root-only
    # ("\\Windows\\foo") forms; reject anything with a drive or root too.
    if pp.is_absolute() or pw.is_absolute() or pw.drive or pw.root:
        raise ValueError(f"credential path must be relative: {value!r}")
    if ".." in pp.parts or ".." in pw.parts:
        raise ValueError(f"credential path must not contain '..': {value!r}")
    return value


def validate_absolute_path(value: str) -> str:
    """Reject paths that aren't absolute on the **host platform**.

    The mirror of ``validate_credential_path``: where credential paths
    must be relative + free of ``..``, absolute paths (e.g. an op-log
    ``live_path``) must be absolute on the host + free of ``..`` and
    not start with ``~``.

    Host-native, intentionally. Downstream consumers (e.g. the op-log
    classifier) parse the value via ``pathlib.Path``, which on POSIX
    is ``PosixPath`` and on Windows is ``WindowsPath``. A cross-
    platform "absolute" form like ``C:\\Users\\me`` on Linux or
    ``/home/u/.claude`` on Windows is ``is_absolute() == False`` for
    the host's Path and would silently be treated as relative against
    CWD — exactly the silent-misclassification class this validator
    exists to prevent. Accepting only the host-native shape keeps the
    validator's contract aligned with what the runtime consumer
    actually does.

    The intent writer is expected to have produced a canonical absolute
    path before persisting. That's a two-step contract: (a) resolve env
    vars + ``~`` via :meth:`PathResolver.expand`, then (b) canonicalize
    via ``os.path.normpath`` / ``Path.resolve(strict=False)`` to fold
    out ``..`` segments — ``expand()`` itself does *not* normalize.
    A ``"~/..."`` value here means step (a) was skipped (corruption);
    a ``"..``-containing absolute path means step (b) was skipped
    (writer bug). Both surface as ``ValueError`` so the journal stays
    fail-fast at the corruption boundary.
    """
    if not value:
        raise ValueError(f"absolute path must not be empty: {value!r}")
    if value.startswith("~"):
        raise ValueError(f"absolute path must be expanded, not tilde-prefixed: {value!r}")
    host_path = Path(value)
    if not host_path.is_absolute():
        raise ValueError(f"absolute path must be absolute on this host platform: {value!r}")
    if ".." in host_path.parts:
        raise ValueError(f"absolute path must not contain '..': {value!r}")
    # `parts` for a filesystem root is a single-element tuple:
    # ``('/')`` on POSIX, ``('C:\\\\',)`` on Windows,
    # ``('\\\\\\\\server\\share\\\\',)`` for a UNC share. Real live
    # paths the writer emits always have at least one additional
    # segment (e.g. ``/home/u/.claude``). Rejecting root-only forms
    # keeps the corruption boundary as fail-fast as possible —
    # nothing the writer produces should land here, and accepting
    # them would let a hand-edited journal point compensation at
    # the filesystem root.
    if len(host_path.parts) <= 1:
        raise ValueError(f"absolute path must not be a filesystem root: {value!r}")
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
# ConfigFile
# ---------------------------------------------------------------------------


class ConfigFile(BaseModel):
    """One on-disk JSON config file managed via owned-path read-merge-write.

    Unlike DirMapping (which the tool reaches via a symlink to a per-profile
    directory), ConfigFile names a single file the tool writes to via atomic
    rename(2). Switcher owns specific JSON subtrees inside it and reconciles
    them on every switch by reading live, overlaying the profile's snapshot,
    and atomically rewriting.

    ``profile_subdir`` is a *grouping key* that ties this config file to the
    same logical owner as a sibling DirMapping. The snapshot lives under
    ``<profile>/.switcher/config_files/<profile_subdir>/<profile_filename>`` —
    ``.switcher`` is a reserved subtree no DirMapping can collide with
    (``validate_safe_name`` rejects leading dots), so the reservation is
    structurally unforgeable.

    ``owned_json_paths`` uses a narrow jq-ish grammar; see ``json_paths.py``
    for the supported tokens (``.key``, ``[]`` over JSON objects, composition).
    """

    posix_path: str
    windows_path: str
    profile_subdir: str
    profile_filename: str
    merge_strategy: Literal["json_subtree_merge"]
    owned_json_paths: tuple[str, ...]

    @field_validator("profile_subdir")
    @classmethod
    def _validate_subdir(cls, v: str) -> str:
        return validate_safe_name(v)

    @field_validator("profile_filename")
    @classmethod
    def _validate_filename(cls, v: str) -> str:
        return validate_safe_name(v)

    @model_validator(mode="after")
    def _validate_owned_paths_non_empty(self) -> ConfigFile:
        if self.merge_strategy == "json_subtree_merge" and not self.owned_json_paths:
            raise ValueError(
                "owned_json_paths must be non-empty when merge_strategy is 'json_subtree_merge'"
            )
        return self

    @model_validator(mode="after")
    def _validate_owned_paths_parse(self) -> ConfigFile:
        # Local import: ``json_paths`` is pure logic with no model deps, but
        # importing at module scope would tighten the import graph for no
        # benefit. Parse-fail-loud at config-load time so a typo'd path
        # surfaces at registry load, not on the first ``use`` that touches it.
        from switcher.json_paths import InvalidOwnedPath, parse_owned_path

        parsed: list[tuple[tuple[str, ...], ...]] = []
        for raw in self.owned_json_paths:
            try:
                segments = parse_owned_path(raw)
            except InvalidOwnedPath as e:
                raise ValueError(
                    f"invalid owned_json_paths entry {raw!r}: {e}"
                ) from e
            parsed.append(segments)

        # Reject duplicates and overlaps. A "prefix overlap" means the
        # longer path writes into a subtree the shorter path already
        # captured whole, producing order-sensitive extraction and silent
        # clobbering at apply time.
        #
        # The overlap check treats ``iter`` as a wildcard that matches any
        # key. ``.a[].b`` and ``.a.c`` both touch ``a["c"]["b"]`` whenever
        # ``"c"`` is one of the iterated keys (abby r7) — the earlier
        # strict-tuple-prefix check missed this because the segment tuples
        # ``(key 'a', iter, key 'b')`` and ``(key 'a', key 'c')`` aren't
        # literal prefixes of each other. Two paths overlap iff their
        # first ``min(len, len)`` positions are pairwise compatible (same
        # key, or at least one ``iter``).
        for i, a in enumerate(parsed):
            for j, b in enumerate(parsed):
                if i == j:
                    continue
                if a == b and i < j:
                    raise ValueError(
                        f"duplicate owned_json_paths entry "
                        f"{self.owned_json_paths[j]!r}"
                    )
                if len(a) > len(b):
                    # The pair will surface on the (j, i) iteration with
                    # a shorter-or-equal; only check the longer side once.
                    continue
                if a == b:
                    # Already reported above when i < j; the j < i half is
                    # otherwise a no-op for equal tuples.
                    continue
                # len(a) <= len(b) and a != b: a is a candidate prefix.
                # Check pairwise compatibility under iter-as-wildcard.
                compatible = True
                for k in range(len(a)):
                    sa, sb = a[k], b[k]
                    if sa[0] == "iter" or sb[0] == "iter":
                        continue
                    # Both segments are ("key", name).
                    if sa[1] != sb[1]:
                        compatible = False
                        break
                if compatible:
                    raise ValueError(
                        f"owned_json_paths entries overlap: "
                        f"{self.owned_json_paths[i]!r} is a "
                        f"(wildcard-compatible) prefix of "
                        f"{self.owned_json_paths[j]!r}; one captures a "
                        f"subtree the other writes into. '[]' iter "
                        f"matches any object key, so e.g. '.a[].b' "
                        f"overlaps with '.a.c.b' (when 'c' is iterated)."
                    )
        return self


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
    config_files: tuple[ConfigFile, ...] = ()

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        return validate_safe_name(v)

    @model_validator(mode="before")
    @classmethod
    def _expand_credential_files(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "credential_files" not in data:
            return data
        # Copy so we don't mutate the caller's dict — re-validating the same
        # payload twice must produce the same result.
        src = cast(dict[str, Any], data)
        out: dict[str, Any] = dict(src)
        shorthand = out.pop("credential_files")
        # mode="before" runs before child fields are coerced, so malformed
        # shapes have to be rejected here with a clean message — otherwise a
        # bare string expands to one credential per character, and a `[{}]`
        # config_dirs raises KeyError instead of ValidationError.
        if isinstance(shorthand, str) or not isinstance(shorthand, (list, tuple)):
            raise ValueError(
                "credential_files must be a list of relative file paths "
                f"(got {type(shorthand).__name__})"
            )
        if not all(isinstance(p, str) for p in shorthand):
            raise ValueError("credential_files entries must be strings")
        config_dirs = out.get("config_dirs") or []
        if not config_dirs:
            raise ValueError("credential_files shorthand requires at least one config_dir")
        first: Any = cast(Any, config_dirs[0])
        # `mode="before"` runs before child models are coerced, but Python
        # callers can still pass already-built DirMapping instances. Handle
        # both shapes so the shorthand works from TOML and from Python.
        if isinstance(first, dict):
            if "profile_subdir" not in first:
                raise ValueError(
                    "credential_files shorthand requires config_dirs[0].profile_subdir"
                )
            first_subdir = first["profile_subdir"]
        else:
            first_subdir = getattr(first, "profile_subdir", None)
            if first_subdir is None:
                raise ValueError(
                    "credential_files shorthand requires config_dirs[0].profile_subdir"
                )
        expanded: list[Any] = [{"config_dir": first_subdir, "path": p} for p in shorthand]
        # `credentials` may arrive as a tuple (Python idiom) or list (TOML);
        # normalize via list() so concatenation never raises.
        existing = list(cast(Any, out.get("credentials") or ()))
        out["credentials"] = expanded + existing
        return out

    @model_validator(mode="after")
    def _validate_credential_dirs(self) -> Tool:
        valid = {d.profile_subdir for d in self.config_dirs}
        for c in self.credentials:
            if c.config_dir not in valid:
                raise ValueError(
                    f"credential references unknown config_dir {c.config_dir!r}; "
                    f"expected one of {sorted(valid)}"
                )
        for cf in self.config_files:
            if cf.profile_subdir not in valid:
                raise ValueError(
                    f"config_file references unknown config_dir {cf.profile_subdir!r}; "
                    f"expected one of {sorted(valid)}"
                )
        # Uniqueness across config_files. Two entries that share a snapshot
        # slot (same profile_subdir + profile_filename) would race on write
        # and produce order-dependent results. Two entries that share a
        # live path on either OS would extract from / apply to the same
        # file twice — silent clobbering. Reject both at load time.
        #
        # Comparison is case-insensitive (``str.casefold``). Default Windows
        # filesystems (NTFS) and default macOS filesystems (APFS) are
        # case-insensitive-but-preserving, so ``~/.claude.json`` and
        # ``~/.Claude.json`` resolve to the same file. Raw string compare
        # would silently accept them as distinct and re-introduce the
        # clobber class. Linux case-sensitive ext4 may produce a rare
        # false positive (two genuinely distinct paths flagged as
        # duplicates), but the convention in tool configs is lowercase
        # only, so the trade-off favors safety.
        seen_slots: set[tuple[str, str]] = set()
        seen_posix: set[str] = set()
        seen_windows: set[str] = set()
        for cf in self.config_files:
            slot = (cf.profile_subdir.casefold(), cf.profile_filename.casefold())
            if slot in seen_slots:
                raise ValueError(
                    f"duplicate config_file snapshot slot "
                    f"(profile_subdir={cf.profile_subdir!r}, "
                    f"profile_filename={cf.profile_filename!r}); "
                    "comparison is case-insensitive"
                )
            seen_slots.add(slot)
            posix_key = cf.posix_path.casefold()
            if posix_key in seen_posix:
                raise ValueError(
                    f"duplicate config_file posix_path {cf.posix_path!r}; "
                    "comparison is case-insensitive"
                )
            seen_posix.add(posix_key)
            windows_key = cf.windows_path.casefold()
            if windows_key in seen_windows:
                raise ValueError(
                    f"duplicate config_file windows_path {cf.windows_path!r}; "
                    "comparison is case-insensitive"
                )
            seen_windows.add(windows_key)
        return self

    @model_validator(mode="after")
    def _validate_at_most_one_config_file(self) -> Tool:
        # v0.1.5 ships one ConfigFile per tool max. The use() commit phase
        # writes the N planned applies in an unrolled loop with no rollback
        # if write #k fails after write #1 has been swapped into place. With
        # N <= 1 the loop is degenerate and safe; allowing N > 1 reaches a
        # commit-time partial-mutation window that the op-log compensation
        # (plan Task 11, spec §2.7) is the architectural answer to.
        #
        # When op-log compensation lands and use() gains rollback for the
        # commit phase, this validator should be removed. Until then,
        # multi-file support belongs behind that integration, not ahead of
        # it. Users with multiple owned subtrees in a single file express
        # that via multiple ``owned_json_paths`` on one ConfigFile, which
        # the v0.1.5 walker already handles end-to-end.
        #
        # Runs *after* _validate_credential_dirs so a 2-CF input that's
        # also internally inconsistent (e.g. duplicate snapshot slot)
        # surfaces the more-specific "duplicate" error first — the dead-code
        # duplicate-detection path is preserved for the post-op-log world
        # where multi-file becomes legal.
        if len(self.config_files) > 1:
            raise ValueError(
                f"tool {self.id!r}: multiple config_files entries are not "
                "supported in v0.1.5 — multi-file commit atomicity awaits "
                "op-log compensation (plan Task 11). Combine the owned "
                "subtrees into a single config_files entry with multiple "
                "owned_json_paths instead."
            )
        return self


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


class Profile(BaseModel):
    """A named configuration snapshot that switcher can re-link to.

    JSON is written with createdAt (camelCase). The serializer emits Z-suffixed
    UTC ISO 8601 truncated to whole seconds (matches the spec example), so any
    sub-second precision on `created_at` is intentionally dropped on write.
    JSON read accepts either createdAt or created_at so manually-edited
    metadata.json files don't break.
    """

    name: str
    created_at: datetime = Field(
        validation_alias=AliasChoices("createdAt", "created_at"),
        serialization_alias="createdAt",
    )
    tools: dict[str, bool]
    # v0.1.5 PR5: opaque token written by ``_store.create`` when the
    # profile is created as part of a journaled op (currently:
    # fresh-mode rescan). Lets recovery refuse fresh-mode targets
    # whose ``.tools`` match by coincidence but whose journal_id
    # doesn't match the in-flight record's ``rescan_id`` — closes
    # the cross-profile data-loss path from a reservation-vs-capture
    # name-collision race (Hermes pass-PR-2 blocker). ``None`` for
    # profiles created by older versions or non-rescan code paths.
    journal_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("journalId", "journal_id"),
        serialization_alias="journalId",
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        return validate_safe_name(v)

    @field_validator("created_at")
    @classmethod
    def _normalize_created_at(cls, v: datetime) -> datetime:
        # Reject naive datetimes: astimezone(UTC) on a naive value silently
        # interprets it as local time and shifts the wall clock the user
        # typed. Forcing tz-awareness keeps round-trips stable.
        if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
            raise ValueError("created_at must include a timezone (got naive datetime)")
        # Normalize to the wire form on input too: convert to UTC and drop
        # microseconds. Otherwise a +02:00 datetime would survive in memory
        # but read back as UTC after JSON round-trip, breaking Profile equality.
        return v.astimezone(UTC).replace(microsecond=0)

    @field_serializer("created_at")
    def _serialize_created_at(self, v: datetime) -> str:
        # Truncate to seconds, force UTC, emit Z (not +00:00)
        utc = v.astimezone(UTC).replace(microsecond=0)
        return utc.strftime("%Y-%m-%dT%H:%M:%SZ")
