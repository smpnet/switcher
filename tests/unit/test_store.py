"""File-backed profile store with atomic JSON writes."""

import json
from pathlib import Path

import pytest

from switcher.errors import ProfileExistsError, StorageError, UnknownProfileError
from switcher.store import FileProfileStore


@pytest.fixture
def store(tmp_path: Path) -> FileProfileStore:
    return FileProfileStore(tmp_path / "state")


def test_create_writes_metadata(store: FileProfileStore) -> None:
    p = store.create("vanilla", {"claude": True, "copilot": True})
    assert p.name == "vanilla"
    assert p.tools == {"claude": True, "copilot": True}
    assert (store.profile_dir("vanilla") / "metadata.json").exists()


def test_create_rejects_duplicate(store: FileProfileStore) -> None:
    store.create("vanilla", {})
    with pytest.raises(ProfileExistsError):
        store.create("vanilla", {})


def test_get_round_trips(store: FileProfileStore) -> None:
    store.create("work", {"claude": True})
    p = store.get("work")
    assert p.name == "work"
    assert p.tools == {"claude": True}


def test_get_unknown_raises(store: FileProfileStore) -> None:
    with pytest.raises(UnknownProfileError):
        store.get("missing")


def test_get_raises_storage_error_on_corrupt_metadata(store: FileProfileStore) -> None:
    """Corrupt metadata.json must surface as StorageError so callers can
    distinguish 'profile is broken' from 'profile is missing'."""
    store.create("corrupt", {})
    (store.profile_dir("corrupt") / "metadata.json").write_text("not valid json", encoding="utf-8")
    with pytest.raises(StorageError):
        store.get("corrupt")


def test_list_returns_sorted_profiles(store: FileProfileStore) -> None:
    store.create("b-second", {})
    store.create("a-first", {})
    profiles = store.list()
    assert [p.name for p in profiles] == ["a-first", "b-second"]


def test_list_empty_when_no_profiles_dir(store: FileProfileStore) -> None:
    assert store.list() == []


def test_delete_removes_profile(store: FileProfileStore) -> None:
    store.create("doomed", {})
    store.delete("doomed")
    with pytest.raises(UnknownProfileError):
        store.get("doomed")


def test_delete_unknown_raises(store: FileProfileStore) -> None:
    with pytest.raises(UnknownProfileError):
        store.delete("missing")


def test_rename_moves_dir_and_updates_metadata(store: FileProfileStore) -> None:
    store.create("old", {"claude": True})
    store.rename("old", "new")
    p = store.get("new")
    assert p.name == "new"
    assert p.tools == {"claude": True}
    with pytest.raises(UnknownProfileError):
        store.get("old")


def test_rename_unknown_raises(store: FileProfileStore) -> None:
    with pytest.raises(UnknownProfileError):
        store.rename("missing", "new")


def test_rename_to_existing_raises(store: FileProfileStore) -> None:
    store.create("a", {})
    store.create("b", {})
    with pytest.raises(ProfileExistsError):
        store.rename("a", "b")


def test_set_active_then_get_active_round_trips(store: FileProfileStore) -> None:
    store.set_active({"claude": "vanilla", "copilot": "work"})
    assert store.get_active() == {"claude": "vanilla", "copilot": "work"}


def test_get_active_empty_when_no_config(store: FileProfileStore) -> None:
    assert store.get_active() == {}


def test_get_active_raises_on_corrupt_config(store: FileProfileStore, tmp_path: Path) -> None:
    cfg = tmp_path / "state" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("not valid json", encoding="utf-8")
    with pytest.raises(StorageError):
        store.get_active()


@pytest.mark.parametrize(
    "payload",
    [
        '{"active": {"claude": null}}',
        '{"active": {"claude": 42}}',
        '{"active": {"claude": ["vanilla"]}}',
        '{"active": {"claude": {"nested": "x"}}}',
    ],
)
def test_get_active_rejects_non_string_values(
    store: FileProfileStore, tmp_path: Path, payload: str
) -> None:
    """Coercing arbitrary JSON values via str() turns config corruption into
    bogus profile names (e.g. null → "None"). Reject up-front so the
    caller sees the real failure mode."""
    cfg = tmp_path / "state" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(payload, encoding="utf-8")
    with pytest.raises(StorageError):
        store.get_active()


def test_atomic_write_no_partial_file(store: FileProfileStore, tmp_path: Path) -> None:
    """After set_active, no leftover .tmp file remains."""
    store.set_active({"claude": "vanilla"})
    state = tmp_path / "state"
    files = sorted(state.iterdir())
    names = [f.name for f in files]
    assert "config.json" in names
    assert all(not n.endswith(".tmp") for n in names)


def test_atomic_write_cleans_tmp_on_replace_failure(
    store: FileProfileStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If replace() fails mid-write, the .tmp sibling must not survive."""

    def _boom(self: Path, _target: Path) -> Path:
        raise OSError("boom")

    monkeypatch.setattr(Path, "replace", _boom)
    with pytest.raises(OSError, match="boom"):
        store.set_active({"claude": "vanilla"})
    state = tmp_path / "state"
    leftover = [p.name for p in state.iterdir() if p.name.endswith(".tmp")]
    assert leftover == []


def test_create_cleans_up_dir_when_metadata_write_fails(
    store: FileProfileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the metadata write fails, create must not leave an empty profile
    directory behind — otherwise a retry hits ProfileExistsError and list()
    surfaces a phantom entry whose get() then errors."""

    def _boom(self: Path, _target: Path) -> Path:
        raise OSError("simulated metadata write failure")

    monkeypatch.setattr(Path, "replace", _boom)
    with pytest.raises(OSError, match="simulated"):
        store.create("vanilla", {"claude": True})

    assert not store.profile_dir("vanilla").exists()
    monkeypatch.undo()
    # Retry must succeed cleanly — no ProfileExistsError from a stale dir.
    store.create("vanilla", {"claude": True})
    assert store.get("vanilla").tools == {"claude": True}


def test_list_during_partial_rename_uses_dir_name_as_source_of_truth(
    store: FileProfileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If metadata is rewritten but the directory move fails, list() must
    report the directory name (still 'old'), not the metadata's name field
    (now 'new'). Otherwise concurrent observers and post-crash callers see
    a profile under a name whose dir doesn't exist — get('new') would
    raise UnknownProfileError despite list() showing 'new'."""
    store.create("old", {"claude": True})

    original = Path.replace
    state = {"calls": 0}

    def fail_dir_move(self: Path, target: Path) -> Path:
        state["calls"] += 1
        if state["calls"] == 2:
            raise OSError("simulated dir move failure")
        return original(self, target)

    monkeypatch.setattr(Path, "replace", fail_dir_move)
    with pytest.raises(OSError, match="simulated"):
        store.rename("old", "new")

    # Crucial: the visible store must still call this profile "old".
    profiles = store.list()
    assert [p.name for p in profiles] == ["old"]
    # And get("old") must return a Profile named "old", not the metadata's
    # stale "new" — that would let callers ignore a successful retry.
    assert store.get("old").name == "old"


def test_rename_is_recoverable_when_directory_move_fails(
    store: FileProfileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the directory move fails *after* metadata has been rewritten in
    place (call 1 = _atomic_write's tmp.replace; call 2 = old_dir.replace),
    re-running rename(old, new) must complete the operation. This guards
    the rename ordering: rewrite metadata in old_dir first, then move the
    dir — so the only intermediate state is one a retry can finish.
    Reverse order leaves the user stuck (dir at new, metadata stale,
    old_dir gone)."""
    store.create("old", {"claude": True})

    original = Path.replace
    state = {"calls": 0}

    def fail_second_replace(self: Path, target: Path) -> Path:
        state["calls"] += 1
        if state["calls"] == 2:
            raise OSError("simulated metadata write failure")
        return original(self, target)

    monkeypatch.setattr(Path, "replace", fail_second_replace)

    with pytest.raises(OSError, match="simulated"):
        store.rename("old", "new")

    # Restore real Path.replace before retry so the recovery exercises real
    # filesystem behavior, not the patch's pass-through.
    monkeypatch.setattr(Path, "replace", original)
    store.rename("old", "new")
    p = store.get("new")
    assert p.name == "new"
    assert p.tools == {"claude": True}
    with pytest.raises(UnknownProfileError):
        store.get("old")


@pytest.mark.parametrize(
    "bad_name", ["../etc", "../../escape", "a/b", ".hidden", "", " ", "\t", "."]
)
def test_profile_methods_reject_unsafe_names(store: FileProfileStore, bad_name: str) -> None:
    """profile_dir is a choke point — every method routing through it must
    refuse path-traversal-style names so callers cannot escape the state dir."""
    with pytest.raises(ValueError):
        store.get(bad_name)
    with pytest.raises(ValueError):
        store.delete(bad_name)
    with pytest.raises(ValueError):
        store.create(bad_name, {})
    with pytest.raises(ValueError):
        store.rename(bad_name, "new")
    with pytest.raises(ValueError):
        store.rename("old", bad_name)


# --- active_live_paths cache (v0.1.3) -----------------------------------------


def _bare_state(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "profiles").mkdir()
    return state_dir


def test_get_active_live_paths_returns_empty_dict_when_missing(tmp_path: Path) -> None:
    state_dir = _bare_state(tmp_path)
    (state_dir / "config.json").write_text(json.dumps({"active": {}}), encoding="utf-8")
    s = FileProfileStore(state_dir)
    assert s.get_active_live_paths() == {}


def test_get_active_live_paths_treats_null_top_level_as_empty(tmp_path: Path) -> None:
    """Spec §2.3: top-level active_live_paths null defaults to empty."""
    state_dir = _bare_state(tmp_path)
    (state_dir / "config.json").write_text(
        json.dumps({"active": {}, "active_live_paths": None}), encoding="utf-8"
    )
    s = FileProfileStore(state_dir)
    assert s.get_active_live_paths() == {}


def test_get_active_live_paths_normalizes_null_per_tool_entries(tmp_path: Path) -> None:
    """Spec §2.3: per-tool null/[] entries are treated as missing (eligible for derivation)."""
    state_dir = _bare_state(tmp_path)
    (state_dir / "config.json").write_text(
        json.dumps(
            {
                "active": {"copilot": "A", "claude": "A"},
                "active_live_paths": {
                    "copilot": ["/home/u/.copilot"],
                    "claude": None,  # null entry → treated as missing
                },
            }
        ),
        encoding="utf-8",
    )
    s = FileProfileStore(state_dir)
    result = s.get_active_live_paths()
    assert result == {"copilot": ["/home/u/.copilot"]}  # claude absent, not raised


def test_get_active_live_paths_normalizes_empty_list_per_tool_entries(tmp_path: Path) -> None:
    state_dir = _bare_state(tmp_path)
    (state_dir / "config.json").write_text(
        json.dumps(
            {
                "active": {"claude": "A"},
                "active_live_paths": {"claude": []},  # empty list → treated as missing
            }
        ),
        encoding="utf-8",
    )
    s = FileProfileStore(state_dir)
    assert s.get_active_live_paths() == {}


def test_set_active_state_writes_both_maps_atomically(tmp_path: Path) -> None:
    state_dir = _bare_state(tmp_path)
    s = FileProfileStore(state_dir)
    s.set_active_state(
        {"copilot": "client-A"},
        {"copilot": ["/home/u/.config/github-copilot", "/home/u/.copilot"]},
    )

    assert s.get_active() == {"copilot": "client-A"}
    assert s.get_active_live_paths() == {
        "copilot": ["/home/u/.config/github-copilot", "/home/u/.copilot"],
    }


def test_set_active_state_writes_empty_dicts_explicitly(tmp_path: Path) -> None:
    """Spec §3.7: post-uninstall, both maps are written as empty dicts."""
    state_dir = _bare_state(tmp_path)
    s = FileProfileStore(state_dir)
    s.set_active_state({"copilot": "A"}, {"copilot": ["/p"]})
    s.set_active_state({}, {})

    raw = json.loads((state_dir / "config.json").read_text(encoding="utf-8"))
    assert raw["active"] == {}
    assert raw["active_live_paths"] == {}  # explicit empty, NOT omitted


def test_set_active_alone_preserves_existing_active_live_paths(tmp_path: Path) -> None:
    """Wrapper safety: `set_active(...)` must not silently drop the cache."""
    state_dir = _bare_state(tmp_path)
    s = FileProfileStore(state_dir)
    s.set_active_state({"copilot": "A"}, {"copilot": ["/p"]})
    s.set_active({"copilot": "B"})  # wrapper — should preserve cache
    assert s.get_active_live_paths() == {"copilot": ["/p"]}


def test_set_active_preserves_empty_cache_entries_for_zero_mapping_tools(
    tmp_path: Path,
) -> None:
    """v0.1.5 carry-forward of the [] preservation contract.

    A zero-mapping tool (registry entry with no ``config_dirs``)
    serializes as ``cache[tid] = []`` on disk — clean init writes that
    shape deliberately (see ``test_continue_serializes_empty_cache_entry_for_zero_mapping_tool``).

    Active-only writes via the ``set_active`` wrapper MUST preserve
    those entries. The wrapper previously round-tripped through the
    normalizing reader ``get_active_live_paths`` which drops ``[]``
    entries — flows like ``ProfileService.rename`` (which calls
    ``set_active`` to update the active map after a profile rename)
    would then silently erase the zero-mapping cache state every
    other layer (init compensation, abort) is now careful to preserve.

    CR pass-2 major finding.
    """
    state_dir = _bare_state(tmp_path)
    s = FileProfileStore(state_dir)
    # Pre-stage a [] cache entry for a zero-mapping tool.
    s.set_active_state(
        {"copilot": "A", "zero-mapping-tool": "B"},
        {"copilot": ["/p"], "zero-mapping-tool": []},
    )
    # Active-only wrapper write (e.g., rename re-pointing active entries).
    s.set_active({"copilot": "A", "zero-mapping-tool": "C"})
    # Raw reader sees both entries with their original shapes.
    assert s.get_active_live_paths_raw() == {
        "copilot": ["/p"],
        "zero-mapping-tool": [],
    }


def test_set_active_live_paths_alone_preserves_existing_active(tmp_path: Path) -> None:
    state_dir = _bare_state(tmp_path)
    s = FileProfileStore(state_dir)
    s.set_active_state({"copilot": "A"}, {})
    s.set_active_live_paths({"copilot": ["/p"]})  # wrapper — preserve active
    assert s.get_active() == {"copilot": "A"}


def test_update_profile_tools_round_trips(store: FileProfileStore) -> None:
    """Happy path: rescan --into reuses this to add a new tool."""
    store.create("work", {"claude": True})
    store.update_profile_tools("work", {"claude": True, "copilot": False})
    assert store.get("work").tools == {"claude": True, "copilot": False}


def test_update_profile_tools_unknown_profile_raises(store: FileProfileStore) -> None:
    with pytest.raises(UnknownProfileError):
        store.update_profile_tools("missing", {"claude": True})


def test_update_profile_tools_wraps_read_oserror_as_storage_error(
    store: FileProfileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OSError on metadata.json read (PermissionError, transient ENOENT after
    the exists() check, sharing violation) must surface as StorageError so
    the layer's contract — only Switcher-flavored exceptions leak — holds."""
    store.create("work", {"claude": True})

    original = Path.read_text

    def boom(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "metadata.json":
            raise PermissionError("simulated read denial")
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", boom)
    with pytest.raises(StorageError, match="simulated read denial"):
        store.update_profile_tools("work", {"claude": True, "copilot": True})


def test_update_profile_tools_preserves_unknown_metadata_keys(store: FileProfileStore) -> None:
    """Symmetric with config.json's unknown-key preservation: a forward-compat
    metadata field written by a newer version must survive a v0.1.3
    rescan/rollback rewrite, otherwise update_profile_tools silently
    truncates downgrade-incompatible state."""
    store.create("work", {"claude": True})
    meta_path = store.profile_dir("work") / "metadata.json"
    raw = json.loads(meta_path.read_text(encoding="utf-8"))
    raw["future_field"] = {"nested": True}
    raw["managed_tools"] = ["claude"]
    meta_path.write_text(json.dumps(raw), encoding="utf-8")

    store.update_profile_tools("work", {"claude": True, "copilot": True})

    written = json.loads(meta_path.read_text(encoding="utf-8"))
    # Typed fields updated via the constructor (validators re-ran).
    assert written["tools"] == {"claude": True, "copilot": True}
    assert written["name"] == "work"
    # Unknown keys round-tripped.
    assert written["future_field"] == {"nested": True}
    assert written["managed_tools"] == ["claude"]


def test_unknown_top_level_keys_are_preserved_on_round_trip(tmp_path: Path) -> None:
    state_dir = _bare_state(tmp_path)
    (state_dir / "config.json").write_text(
        json.dumps(
            {
                "active": {"copilot": "A"},
                "active_live_paths": {"copilot": ["/p"]},
                "managed_tools": ["copilot"],  # synthetic future v0.1.4 key
                "future_thing": {"nested": True},
            }
        ),
        encoding="utf-8",
    )
    s = FileProfileStore(state_dir)

    s.set_active_state({"copilot": "B"}, {"copilot": ["/p"]})

    raw = json.loads((state_dir / "config.json").read_text(encoding="utf-8"))
    assert raw["active"] == {"copilot": "B"}
    assert raw["active_live_paths"] == {"copilot": ["/p"]}
    # Unknown keys preserved.
    assert raw["managed_tools"] == ["copilot"]
    assert raw["future_thing"] == {"nested": True}
