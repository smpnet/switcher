"""File-backed profile store with atomic JSON writes."""

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


def test_rename_is_recoverable_when_metadata_rewrite_fails(
    store: FileProfileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If rewriting metadata fails midway through rename, re-running
    rename(old, new) must complete the operation. This guards the rename
    ordering: rewrite metadata in old_dir first, then move the dir — so the
    only intermediate state is one a retry can finish. Reverse order leaves
    the user stuck (dir at new, metadata stale, old_dir gone)."""
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


@pytest.mark.parametrize("bad_name", ["../etc", "../../escape", "a/b", ".hidden"])
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
