"""File-backed profile store.

Layout under <state_dir>:
    config.json                    {"active": {"toolID": "profileName", ...}}
    profiles/<name>/metadata.json  Profile JSON
    profiles/<name>/<subdir>/...   the actual managed dirs

All writes go through `_atomic_write` (tmp + os.replace) so a crash mid-write
can't tear config.json or metadata.json.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from switcher.errors import ProfileExistsError, StorageError, UnknownProfileError
from switcher.models import Profile


class ProfileStore(Protocol):
    """Surface that ProfileService and CLI use. Tests can supply fakes."""

    def create(self, name: str, tools: Mapping[str, bool]) -> Profile: ...
    def get(self, name: str) -> Profile: ...
    def list(self) -> list[Profile]: ...
    def delete(self, name: str) -> None: ...
    def rename(self, old: str, new: str) -> None: ...
    def set_active(self, mapping: Mapping[str, str]) -> None: ...
    def get_active(self) -> dict[str, str]: ...
    def profile_dir(self, name: str) -> Path: ...
    def state_dir(self) -> Path: ...


class FileProfileStore:
    def __init__(self, state_dir: Path) -> None:
        self._state_dir = Path(state_dir)

    # -- paths --------------------------------------------------------------

    def state_dir(self) -> Path:
        return self._state_dir

    def profile_dir(self, name: str) -> Path:
        return self._state_dir / "profiles" / name

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
        tmp.write_text(data, encoding="utf-8")
        tmp.replace(path)

    # -- profile CRUD -------------------------------------------------------

    def create(self, name: str, tools: Mapping[str, bool]) -> Profile:
        if self.profile_dir(name).exists():
            raise ProfileExistsError(f"profile {name!r} already exists")
        profile = Profile(
            name=name,
            created_at=datetime.now(UTC).replace(microsecond=0),
            tools=dict(tools),
        )
        self.profile_dir(name).mkdir(parents=True, exist_ok=True)
        self._atomic_write(
            self._metadata_path(name),
            profile.model_dump_json(by_alias=True),
        )
        return profile

    def get(self, name: str) -> Profile:
        path = self._metadata_path(name)
        if not path.exists():
            raise UnknownProfileError(f"profile {name!r} not found")
        try:
            return Profile.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as e:
            raise StorageError(f"error reading {path}: {e}") from e

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
        if not self.profile_dir(old).exists():
            raise UnknownProfileError(f"profile {old!r} not found")
        if self.profile_dir(new).exists():
            raise ProfileExistsError(f"profile {new!r} already exists")
        self.profile_dir(new).parent.mkdir(parents=True, exist_ok=True)
        self.profile_dir(old).replace(self.profile_dir(new))
        existing = self.get(new)
        renamed = Profile(name=new, created_at=existing.created_at, tools=existing.tools)
        self._atomic_write(
            self._metadata_path(new),
            renamed.model_dump_json(by_alias=True),
        )

    # -- active map ---------------------------------------------------------

    def set_active(self, mapping: Mapping[str, str]) -> None:
        data = json.dumps({"active": dict(mapping)}, sort_keys=True)
        self._atomic_write(self._config_path(), data)

    def get_active(self) -> dict[str, str]:
        path = self._config_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise StorageError(f"error reading config.json: {e}") from e
        if not isinstance(data, dict):
            raise StorageError(f"malformed config.json: {path}")
        active = cast("dict[object, object]", data).get("active", {})
        if not isinstance(active, dict):
            raise StorageError(f"malformed config.json: {path}")
        active_typed = cast("dict[object, object]", active)
        return {str(k): str(v) for k, v in active_typed.items()}
