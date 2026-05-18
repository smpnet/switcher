"""Tests for the JSON owned-path walker (spec §3.4)."""

from __future__ import annotations

from copy import deepcopy

import pytest

from switcher.json_paths import (
    InvalidOwnedPath,
    UnsupportedWalkTarget,
    apply_owned_paths,
    extract_owned_paths,
    parse_owned_path,
)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parse_simple_key():
    assert parse_owned_path(".mcpServers") == (("key", "mcpServers"),)


def test_parse_nested_key():
    assert parse_owned_path(".a.b") == (("key", "a"), ("key", "b"))


def test_parse_iter():
    assert parse_owned_path(".projects[].mcpServers") == (
        ("key", "projects"),
        ("iter",),
        ("key", "mcpServers"),
    )


def test_parse_rejects_missing_leading_dot():
    with pytest.raises(InvalidOwnedPath):
        parse_owned_path("mcpServers")


def test_parse_rejects_empty_key():
    with pytest.raises(InvalidOwnedPath):
        parse_owned_path("..mcpServers")


def test_parse_rejects_wildcard():
    with pytest.raises(InvalidOwnedPath):
        parse_owned_path(".*")


def test_parse_rejects_filter():
    with pytest.raises(InvalidOwnedPath):
        parse_owned_path(".foo[?(@.bar)]")


def test_parse_rejects_iter_as_leaf():
    """``[]`` at the end has no defined semantics in v1. The parser is the
    single source of truth, so it must reject — not just the walker."""
    with pytest.raises(InvalidOwnedPath, match="must not end with"):
        parse_owned_path(".projects[]")


def test_parse_rejects_consecutive_iter():
    """``[][]`` violates the grammar (subpath after ``[]`` must be ``.KEY``)."""
    with pytest.raises(InvalidOwnedPath, match="must be followed by"):
        parse_owned_path(".a[][].b")


def test_parse_rejects_iter_without_dot_continuation():
    """``[]b`` (no dot after iter) violates the grammar."""
    with pytest.raises(InvalidOwnedPath):
        parse_owned_path(".a[]b")


# ---------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------


def test_extract_top_level_key():
    live = {"mcpServers": {"server1": {"command": "x"}}, "other": 1}
    snap = extract_owned_paths(live, (".mcpServers",))
    assert snap == {"mcpServers": {"server1": {"command": "x"}}}


def test_extract_preserves_object_keys_under_iter():
    live = {
        "projects": {
            "/repo/A": {"mcpServers": {"s": {}}, "lastSessionId": "ABC"},
            "/repo/B": {"mcpServers": {"t": {}}, "trustAccepted": True},
        }
    }
    snap = extract_owned_paths(live, (".projects[].mcpServers",))
    assert snap == {
        "projects": {
            "/repo/A": {"mcpServers": {"s": {}}},
            "/repo/B": {"mcpServers": {"t": {}}},
        }
    }


def test_extract_skips_missing_key():
    live = {"other": 1}
    snap = extract_owned_paths(live, (".mcpServers",))
    assert snap == {}


def test_extract_skips_missing_inner_key_under_iter():
    live = {"projects": {"/repo/A": {"lastSessionId": "ABC"}}}
    snap = extract_owned_paths(live, (".projects[].mcpServers",))
    # Project key kept as {} because the iter found it, but the inner
    # mcpServers key is absent live so apply's delete-on-absence handles it.
    assert snap == {"projects": {"/repo/A": {}}}


def test_extract_rejects_array_iter():
    live = {"projects": [{"mcpServers": {}}]}
    with pytest.raises(UnsupportedWalkTarget):
        extract_owned_paths(live, (".projects[].mcpServers",))


def test_extract_skips_non_dict_descent_target():
    """Round-trip stability: extract on ``.a.b`` against ``{"a": 1}`` must
    NOT produce ``{"a": {}}``. The placeholder would lie about
    descendability and apply on the unchanged live would raise."""
    live = {"a": 1}
    snap = extract_owned_paths(live, (".a.b",))
    assert snap == {}


def test_extract_apply_round_trip_stable_on_blocked_key_descent():
    """The motivating regression: extract followed by apply on the same
    unchanged live must be a no-op even when the descent path doesn't fit
    live's shape."""
    live = {"a": 1}
    snap = extract_owned_paths(live, (".a.b",))
    out = apply_owned_paths(live, snap, (".a.b",))
    assert out == live


def test_extract_handles_multiple_owned_paths():
    live = {
        "mcpServers": {"a": {}},
        "projects": {"/r": {"mcpServers": {"b": {}}}},
        "oauthAccount": {"email": "x@y"},
        "untouched": True,
    }
    snap = extract_owned_paths(
        live,
        (".mcpServers", ".projects[].mcpServers", ".oauthAccount"),
    )
    assert snap == {
        "mcpServers": {"a": {}},
        "projects": {"/r": {"mcpServers": {"b": {}}}},
        "oauthAccount": {"email": "x@y"},
    }
    assert "untouched" not in snap


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def test_apply_overlays_top_level_scalar():
    live = {"mcpServers": {"old": {}}, "other": 1}
    snap = {"mcpServers": {"new": {}}}
    out = apply_owned_paths(live, snap, (".mcpServers",))
    assert out == {"mcpServers": {"new": {}}, "other": 1}


def test_apply_deletes_top_level_key_when_snapshot_absent():
    live = {"mcpServers": {"old": {}}, "other": 1}
    snap: dict[str, object] = {}
    out = apply_owned_paths(live, snap, (".mcpServers",))
    assert "mcpServers" not in out
    assert out["other"] == 1


def test_apply_does_not_mutate_input():
    live = {"mcpServers": {"old": {}}}
    snap = {"mcpServers": {"new": {}}}
    live_before = deepcopy(live)
    snap_before = deepcopy(snap)
    apply_owned_paths(live, snap, (".mcpServers",))
    assert live == live_before
    assert snap == snap_before


def test_apply_iter_overlays_per_project():
    live = {
        "projects": {
            "/r/A": {"mcpServers": {"old": {}}, "lastSessionId": "X"},
            "/r/B": {"mcpServers": {"old": {}}, "trustAccepted": True},
        }
    }
    snap = {
        "projects": {
            "/r/A": {"mcpServers": {"new-A": {}}},
            "/r/B": {"mcpServers": {"new-B": {}}},
        }
    }
    out = apply_owned_paths(live, snap, (".projects[].mcpServers",))
    assert out["projects"]["/r/A"]["mcpServers"] == {"new-A": {}}
    assert out["projects"]["/r/A"]["lastSessionId"] == "X"
    assert out["projects"]["/r/B"]["mcpServers"] == {"new-B": {}}
    assert out["projects"]["/r/B"]["trustAccepted"] is True


def test_apply_iter_deletes_when_snapshot_lacks_key_for_that_project():
    live = {
        "projects": {
            "/r/A": {"mcpServers": {"old": {}}, "lastSessionId": "X"},
        }
    }
    snap = {"projects": {"/r/A": {}}}
    out = apply_owned_paths(live, snap, (".projects[].mcpServers",))
    assert "mcpServers" not in out["projects"]["/r/A"]
    assert out["projects"]["/r/A"]["lastSessionId"] == "X"


def test_apply_iter_creates_project_when_only_in_snapshot():
    live = {"projects": {"/r/A": {"lastSessionId": "X"}}}
    snap = {"projects": {"/r/B": {"mcpServers": {"new": {}}}}}
    out = apply_owned_paths(live, snap, (".projects[].mcpServers",))
    assert out["projects"]["/r/A"]["lastSessionId"] == "X"
    assert out["projects"]["/r/B"] == {"mcpServers": {"new": {}}}


def test_apply_iter_creates_projects_root_when_missing_in_live():
    live: dict[str, object] = {}
    snap = {"projects": {"/r/A": {"mcpServers": {"new": {}}}}}
    out = apply_owned_paths(live, snap, (".projects[].mcpServers",))
    assert out["projects"]["/r/A"] == {"mcpServers": {"new": {}}}


def test_apply_iter_does_not_create_ghost_entry_for_empty_snapshot_placeholder():
    """An iter-only snapshot placeholder (``{"/r/A": {}}``) must not
    materialize a ghost project on a machine where ``/r/A`` never existed,
    AND must not materialize a ghost parent container (``projects: {}``).

    This is the shape extract produces when the iter found the project but
    its owned leaf was absent at save time. Restoring shouldn't pollute
    live with anything when there's no owned data to write.
    """
    live: dict[str, object] = {}
    snap = {"projects": {"/r/A": {}}}
    out = apply_owned_paths(live, snap, (".projects[].mcpServers",))
    assert out == {}, f"expected empty out, got {out!r}"


def test_apply_preserves_empty_parent_from_existing_live_after_delete():
    """Distinct from the ghost-pruning rule: when live ALREADY had a
    container and apply deletes its only owned leaf, the empty container
    is preserved per spec §3.4 (matches Claude's own ``mcp remove`` shape).
    """
    live = {"projects": {"/r/A": {"mcpServers": {"old": {}}}}}
    snap: dict[str, object] = {}
    out = apply_owned_paths(live, snap, (".projects[].mcpServers",))
    # Live already had /r/A — preserved as {} after delete (NOT pruned).
    assert out == {"projects": {"/r/A": {}}}


def test_apply_keeps_empty_project_object_after_delete():
    live = {"projects": {"/r/A": {"mcpServers": {"old": {}}}}}
    snap = {"projects": {"/r/A": {}}}
    out = apply_owned_paths(live, snap, (".projects[].mcpServers",))
    # Empty project object kept — matches Claude's own write shape after
    # `mcp remove` (it leaves empty project entries behind).
    assert out["projects"]["/r/A"] == {}


def test_apply_rejects_iter_as_leaf_segment():
    """Mirror extract: ``[]`` as a leaf has no defined semantics in v1."""
    live = {"projects": {"/r/A": {"mcpServers": {}}}}
    snap = {"projects": {"/r/A": {"mcpServers": {}}}}
    with pytest.raises(InvalidOwnedPath, match="leaf"):
        apply_owned_paths(live, snap, (".projects[]",))


def test_apply_raises_when_iter_target_is_list_in_live():
    """Apply must fail loudly when live has a non-dict at an iter target,
    matching extract's UnsupportedWalkTarget behavior. Silent no-op would
    hide a malformed live config."""
    live = {"projects": []}
    snap: dict[str, object] = {}
    with pytest.raises(UnsupportedWalkTarget):
        apply_owned_paths(live, snap, (".projects[].mcpServers",))


def test_apply_raises_when_iter_target_is_scalar_in_live():
    live = {"projects": 1}
    snap: dict[str, object] = {}
    with pytest.raises(UnsupportedWalkTarget):
        apply_owned_paths(live, snap, (".projects[].mcpServers",))


def test_apply_raises_when_key_descent_blocked_and_snapshot_has_data():
    """Snapshot has owned data below ``.a.b`` but live has a scalar at ``.a``.
    Overwriting destroys machine-global; skipping drops owned data; raise."""
    live = {"a": 1}
    snap = {"a": {"b": 2}}
    with pytest.raises(UnsupportedWalkTarget):
        apply_owned_paths(live, snap, (".a.b",))


def test_apply_preserves_scalar_when_key_descent_blocked_and_snapshot_empty():
    """Snapshot has nothing at ``.a.b`` (delete-on-absence). Live has a scalar
    at ``.a``. Path doesn't apply — preserve the scalar silently."""
    live = {"a": 1}
    snap: dict[str, object] = {}
    out = apply_owned_paths(live, snap, (".a.b",))
    assert out == {"a": 1}


def test_apply_raises_when_iter_entry_blocks_descent_and_snapshot_has_data():
    """Mirror the key-branch rule for per-entry iter descent: if snapshot
    has owned data below an iter entry but live has a scalar/list there,
    raise instead of silently dropping the owned data."""
    live = {"projects": {"/r/A": 1}}
    snap = {"projects": {"/r/A": {"mcpServers": {"x": {}}}}}
    with pytest.raises(UnsupportedWalkTarget):
        apply_owned_paths(live, snap, (".projects[].mcpServers",))


def test_apply_preserves_iter_entry_scalar_when_snapshot_has_nothing():
    """Delete-on-absence path through iter doesn't apply when the entry
    is a scalar — preserve silently."""
    live = {"projects": {"/r/A": 1}}
    snap: dict[str, object] = {}
    out = apply_owned_paths(live, snap, (".projects[].mcpServers",))
    assert out == {"projects": {"/r/A": 1}}


def test_apply_raises_on_malformed_snapshot_subtree_at_key():
    """Snapshot has a scalar where non-leaf descent expects an object.
    Without this check, snap_has=False would propagate and silently
    delete owned data from live."""
    live = {"a": {"b": "owned-data"}}
    snap = {"a": "corrupted"}
    with pytest.raises(UnsupportedWalkTarget, match="corrupted"):
        apply_owned_paths(live, snap, (".a.b",))


def test_apply_raises_on_malformed_snapshot_subtree_at_iter_entry():
    """Snapshot has a scalar at an iter key where descent expects an
    object. Critical: without this check, apply silently deletes
    mcpServers from live when snapshot is corrupt."""
    live = {"projects": {"/r/A": {"mcpServers": {"x": {}}}}}
    snap = {"projects": {"/r/A": 1}}  # corrupted
    with pytest.raises(UnsupportedWalkTarget, match="corrupted"):
        apply_owned_paths(live, snap, (".projects[].mcpServers",))
    # Critically: live was NOT mutated before the raise. Apply must be
    # all-or-nothing on the input dict — partial mutation on raise would
    # silently corrupt live state.
    # (Note: apply_owned_paths returns a new dict; the raise propagates
    # so the caller never sees a partial result.)


def test_apply_iter_preserves_live_key_order():
    """Iteration through ``[]`` must preserve live's existing dict order so
    a logically-no-op restore doesn't reshuffle key order in the output."""
    live = {
        "projects": {
            "/z-last": {"mcpServers": {"a": {}}},
            "/a-first": {"mcpServers": {"b": {}}},
            "/m-middle": {"mcpServers": {"c": {}}},
        }
    }
    # Snapshot matches live exactly — apply should be a no-op shape-wise.
    snap = {
        "projects": {
            "/m-middle": {"mcpServers": {"c": {}}},
            "/z-last": {"mcpServers": {"a": {}}},
            "/a-first": {"mcpServers": {"b": {}}},
        }
    }
    out = apply_owned_paths(live, snap, (".projects[].mcpServers",))
    # Live's original key order is preserved (z-last, a-first, m-middle),
    # NOT the snapshot's order or a hash-derived set order.
    assert list(out["projects"].keys()) == ["/z-last", "/a-first", "/m-middle"]


def test_apply_iter_appends_snapshot_only_keys_in_snapshot_order():
    """Snapshot-only keys (those not in live) get appended after live's
    keys, in the order they appear in the snapshot dict."""
    live = {"projects": {"/live-A": {"mcpServers": {"x": {}}}}}
    snap = {
        "projects": {
            "/snap-only-Z": {"mcpServers": {"y": {}}},
            "/live-A": {"mcpServers": {"x": {}}},
            "/snap-only-A": {"mcpServers": {"z": {}}},
        }
    }
    out = apply_owned_paths(live, snap, (".projects[].mcpServers",))
    # Live key first, then snapshot-only in snapshot dict order.
    assert list(out["projects"].keys()) == ["/live-A", "/snap-only-Z", "/snap-only-A"]


def test_apply_multiple_owned_paths_composes():
    live = {
        "mcpServers": {"old": {}},
        "oauthAccount": {"email": "old@x"},
        "untouched": 1,
    }
    snap = {
        "mcpServers": {"new": {}},
        "oauthAccount": {"email": "new@x"},
    }
    out = apply_owned_paths(live, snap, (".mcpServers", ".oauthAccount"))
    assert out["mcpServers"] == {"new": {}}
    assert out["oauthAccount"] == {"email": "new@x"}
    assert out["untouched"] == 1
