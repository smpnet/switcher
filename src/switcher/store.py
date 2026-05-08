"""File-backed profile store.

Layout under <state_dir>:
    config.json                    {"active": {"toolID": "profileName", ...}}
    profiles/<name>/metadata.json  Profile JSON
    profiles/<name>/<subdir>/...   the actual managed dirs

All writes go through `_atomic_write` (tmp + os.replace) so a crash mid-write
can't tear config.json or metadata.json.

The store is the persistence layer only. Cross-concern consistency — e.g.
updating the active map when a profile is renamed or deleted — is the
caller's responsibility (see `ProfileService` for the policy layer).
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from switcher.errors import ProfileExistsError, StorageError, UnknownProfileError
from switcher.models import Profile, validate_safe_name


class ProfileStore(Protocol):
    """Surface that ProfileService and CLI use. Tests can supply fakes."""

    def create(self, name: str, tools: Mapping[str, bool]) -> Profile: ...
    def get(self, name: str) -> Profile: ...
    def list(self) -> list[Profile]: ...
    def delete(self, name: str) -> None: ...
    def rename(self, old: str, new: str) -> None: ...

    # v0.1.3: combined writer is the canonical path. set_active /
    # set_active_live_paths remain as thin wrappers — they exist so callers
    # that only know about one map don't accidentally drop the other on
    # disk (the wrappers read the unmodified key from disk and round-trip it
    # through set_active_state).
    def set_active_state(
        self,
        active: Mapping[str, str],
        active_live_paths: Mapping[str, list[str]],
    ) -> None: ...

    def set_active(self, mapping: Mapping[str, str]) -> None: ...
    def get_active(self) -> dict[str, str]: ...
    def set_active_live_paths(self, mapping: Mapping[str, list[str]]) -> None: ...
    def get_active_live_paths(self) -> dict[str, list[str]]: ...

    def update_profile_tools(self, name: str, tools: Mapping[str, bool]) -> None: ...

    def profile_dir(self, name: str) -> Path: ...
    def state_dir(self) -> Path: ...


class FileProfileStore:
    def __init__(self, state_dir: Path) -> None:
        self._state_dir = Path(state_dir)

    # -- paths --------------------------------------------------------------

    def state_dir(self) -> Path:
        return self._state_dir

    def profile_dir(self, name: str) -> Path:
        # Validate at the choke point: every CRUD method routes through here,
        # so any caller (incl. the Protocol's external implementers) is forced
        # through validate_safe_name before a name can become a real path.
        return self._state_dir / "profiles" / validate_safe_name(name)

    def _profiles_dir(self) -> Path:
        return self._state_dir / "profiles"

    def _metadata_path(self, name: str) -> Path:
        return self.profile_dir(name) / "metadata.json"

    def _config_path(self) -> Path:
        return self._state_dir / "config.json"

    # -- atomic writes ------------------------------------------------------

    def _atomic_write(self, path: Path, data: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(data, encoding="utf-8")
            tmp.replace(path)
        except Exception:
            # Don't leave a half-written sibling on disk; missing_ok handles
            # the case where write_text never created the file.
            tmp.unlink(missing_ok=True)
            raise

    # -- profile CRUD -------------------------------------------------------

    def create(self, name: str, tools: Mapping[str, bool]) -> Profile:
        if self.profile_dir(name).exists():
            raise ProfileExistsError(f"profile {name!r} already exists")
        profile = Profile(
            name=name,
            created_at=datetime.now(UTC).replace(microsecond=0),
            tools=dict(tools),
        )
        d = self.profile_dir(name)
        d.mkdir(parents=True, exist_ok=True)
        try:
            self._atomic_write(
                self._metadata_path(name),
                profile.model_dump_json(by_alias=True),
            )
        except Exception:
            # No-debris discipline: a half-created profile would later
            # surface as ProfileExistsError (blocking retries) and as a
            # phantom in list() whose get() then errors. Roll back.
            shutil.rmtree(d, ignore_errors=True)
            raise
        return profile

    def get(self, name: str) -> Profile:
        path = self._metadata_path(name)
        if not path.exists():
            raise UnknownProfileError(f"profile {name!r} not found")
        try:
            profile = Profile.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as e:
            raise StorageError(f"error reading {path}: {e}") from e
        # The directory name is the canonical identifier; metadata.name is a
        # cached display copy that can drift during a partial-failure rename
        # (metadata rewritten, dir move failed). Reconcile here so list() and
        # other observers see a consistent view: caller-visible name matches
        # the on-disk path. The cached name is repaired on the next rewrite.
        if profile.name != name:
            profile = Profile(name=name, created_at=profile.created_at, tools=profile.tools)
        return profile

    def list(self) -> list[Profile]:
        if not self._profiles_dir().exists():
            return []
        names = sorted(p.name for p in self._profiles_dir().iterdir() if p.is_dir())
        return [self.get(n) for n in names]

    def delete(self, name: str) -> None:
        d = self.profile_dir(name)
        if not d.exists():
            raise UnknownProfileError(f"profile {name!r} not found")
        shutil.rmtree(d)

    def rename(self, old: str, new: str) -> None:
        # Validate both names up front so an invalid `new` is rejected even
        # when `old` doesn't exist (otherwise UnknownProfileError shadows the
        # safer-failure ValueError from validate_safe_name).
        old_dir = self.profile_dir(old)
        new_dir = self.profile_dir(new)
        if not old_dir.exists():
            raise UnknownProfileError(f"profile {old!r} not found")
        if new_dir.exists():
            raise ProfileExistsError(f"profile {new!r} already exists")
        # Order matters for crash-recovery: rewrite metadata in old_dir first,
        # then move the dir. If the metadata write fails, old_dir is intact
        # and a retry replays cleanly. If the dir move fails after the
        # metadata rewrite, old_dir holds metadata that already says `new` —
        # re-running rename(old, new) is idempotent. Reverse order leaves the
        # caller stuck (dir at new with stale metadata, old_dir gone).
        existing = self.get(old)
        renamed = Profile(name=new, created_at=existing.created_at, tools=existing.tools)
        self._atomic_write(
            self._metadata_path(old),
            renamed.model_dump_json(by_alias=True),
        )
        new_dir.parent.mkdir(parents=True, exist_ok=True)
        old_dir.replace(new_dir)

    # -- config.json layer (active + active_live_paths + unknown keys) ------

    def _load_config(self) -> dict[str, object]:
        """Load full config.json as a top-level dict, including unknown keys."""
        path = self._config_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise StorageError(f"error reading config.json: {e}") from e
        if not isinstance(data, dict):
            raise StorageError(f"malformed config.json: {path}")
        return cast("dict[str, object]", data)

    def _write_config(self, data: Mapping[str, object]) -> None:
        """Write the full top-level dict atomically. Caller is responsible for
        having merged any unknown keys back in via _load_config first."""
        text = json.dumps(dict(data), sort_keys=True)
        self._atomic_write(self._config_path(), text)

    def set_active_state(
        self,
        active: Mapping[str, str],
        active_live_paths: Mapping[str, list[str]],
    ) -> None:
        """Canonical write: both maps in one atomic config.json write.
        Always writes both keys, even when empty (spec §2.4 / §3.7)."""
        data = self._load_config()
        data["active"] = dict(active)
        data["active_live_paths"] = {k: list(v) for k, v in active_live_paths.items()}
        self._write_config(data)

    def set_active(self, mapping: Mapping[str, str]) -> None:
        """Convenience wrapper. Preserves the existing on-disk active_live_paths."""
        self.set_active_state(mapping, self.get_active_live_paths())

    def get_active(self) -> dict[str, str]:
        path = self._config_path()
        data = self._load_config()
        active = data.get("active", {})
        if not isinstance(active, dict):
            raise StorageError(f"malformed config.json: {path}")
        active_typed = cast("dict[object, object]", active)
        # Reject non-string keys/values up-front. Coercing via str() turns
        # config corruption into bogus profile names (null → "None") and
        # masks the real failure mode.
        result: dict[str, str] = {}
        for k, v in active_typed.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise StorageError(f"malformed config.json: {path}")
            result[k] = v
        return result

    def set_active_live_paths(self, mapping: Mapping[str, list[str]]) -> None:
        """Convenience wrapper. Preserves the existing on-disk active map."""
        self.set_active_state(self.get_active(), mapping)

    def get_active_live_paths(self) -> dict[str, list[str]]:
        """Read the cache, normalizing null/empty per spec §2.3.

        Tolerated shapes (treated as absence):
          - top-level active_live_paths missing or null → {}
          - per-tool entry null or [] → tool absent from result

        Rejected shapes (raise StorageError):
          - top-level active_live_paths is a non-dict, non-null type
          - per-tool entry is non-null, non-list
          - per-tool entry contains non-string elements
          - per-tool key is not a string
        """
        data = self._load_config()
        raw = data.get("active_live_paths")
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise StorageError(
                f"config.json: 'active_live_paths' must be an object or null, "
                f"got {type(raw).__name__}"
            )
        result: dict[str, list[str]] = {}
        for k, v in cast("dict[object, object]", raw).items():
            if not isinstance(k, str):
                raise StorageError("config.json: 'active_live_paths' keys must be strings")
            if v is None or v == []:
                continue  # normalize to absence; eligible for derivation
            if not isinstance(v, list):
                raise StorageError(
                    f"config.json: 'active_live_paths[{k}]' must be a list, null, "
                    f"or empty array; got {type(v).__name__}"
                )
            paths: list[str] = []
            for p in cast("list[object]", v):
                if not isinstance(p, str):
                    raise StorageError(
                        f"config.json: 'active_live_paths[{k}]' must contain strings"
                    )
                paths.append(p)
            result[k] = paths
        return result

    def update_profile_tools(self, name: str, tools: Mapping[str, bool]) -> None:
        """Re-write a profile's metadata.json with an updated tools map.

        Used by `rescan --into` to add a new tool to an existing profile's
        metadata (and by `rescan` rollback to revert that change). The
        write is atomic via _atomic_write; the in-memory Profile is rebuilt
        via model_copy so existing validation runs.
        """
        prof = self.get(name)
        new_prof = prof.model_copy(update={"tools": dict(tools)})
        meta_path = self.profile_dir(name) / "metadata.json"
        text = new_prof.model_dump_json(by_alias=True)
        self._atomic_write(meta_path, text)
