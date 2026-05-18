"""JSON owned-path walker for ConfigFile snapshots.

Implements the narrow jq-ish grammar from spec §3.4:

- ``.key`` — descend into an object key
- ``[]`` — iterate entries of a **JSON object** (string keys preserved)
- Composition: ``.projects[].mcpServers``

Not supported in v1: array iteration, filter predicates, wildcards,
recursive descent. Out-of-grammar tokens raise ``InvalidOwnedPath``.

Three modes:

- :func:`parse_owned_path` — string → tuple of segments
- :func:`extract_owned_paths` — live JSON → snapshot projection
- :func:`apply_owned_paths` — overlay snapshot onto live with delete-on-absence

The walker never mutates its inputs; :func:`apply_owned_paths` returns a new
dict (caller is responsible for serialization).
"""

from __future__ import annotations

import copy
import re
from typing import Any

# Object keys in v1 grammar: alphanumerics, underscore, hyphen. No dot —
# `.` is always a segment separator in ``parse_owned_path``, so allowing it
# inside the key class would document a grammar feature the parser can
# never produce. Keys containing dots are explicitly out of scope for v1
# (spec §3.4: "key names with dots/special chars: not in scope for v1").
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class InvalidOwnedPath(ValueError):
    """Raised when an ``owned_json_paths`` entry doesn't parse."""


class UnsupportedWalkTarget(ValueError):
    """Raised when the walker hits a JSON shape v1 doesn't support
    (e.g. ``[]`` applied to a JSON array rather than an object)."""


_Segment = tuple[str, ...]  # ("key", "name") or ("iter",)


def parse_owned_path(path: str) -> tuple[_Segment, ...]:
    """Parse an owned path string into a tuple of segments.

    Grammar (per spec §3.4)::

        path  := ('.' KEY | '[]')+
        KEY   := matches ``[A-Za-z0-9_-]+`` (no dots — ``.`` is always a
                 segment separator; keys with dots are out of scope for v1)
    """
    if not path or not path.startswith("."):
        raise InvalidOwnedPath(f"owned path must start with '.': {path!r}")

    segments: list[_Segment] = []
    i = 0
    while i < len(path):
        ch = path[i]
        if ch == ".":
            j = i + 1
            while j < len(path) and path[j] not in ".[":
                j += 1
            key = path[i + 1 : j]
            if not key:
                raise InvalidOwnedPath(f"empty key segment in {path!r}")
            if not _KEY_RE.fullmatch(key):
                raise InvalidOwnedPath(
                    f"unsupported key {key!r} in {path!r}; v1 grammar is jq-ish "
                    "and does not support wildcards, filters, or recursion"
                )
            segments.append(("key", key))
            i = j
        elif ch == "[":
            if path[i : i + 2] != "[]":
                raise InvalidOwnedPath(
                    f"unsupported token at {path[i:]!r}: v1 grammar only supports '[]', "
                    "not array indices, filters, or wildcards"
                )
            segments.append(("iter",))
            i += 2
        else:
            raise InvalidOwnedPath(
                f"unexpected character {ch!r} at position {i} in {path!r}"
            )

    return tuple(segments)


def extract_owned_paths(
    live: dict[str, Any], owned_paths: tuple[str, ...]
) -> dict[str, Any]:
    """Project owned subtrees from ``live`` into a new dict (snapshot shape).

    The output preserves the keys/indices needed to reconstruct each owned
    path. For ``.projects[].mcpServers`` against a live JSON with two
    projects, the output keeps both project keys (object iteration preserves
    string keys; flat "stream of values" extraction would lose them).

    A missing inner key under ``[]`` leaves the parent object as ``{}`` in
    the snapshot — the iter found the project, but its ``mcpServers`` wasn't
    there to extract. Apply's delete-on-absence rule handles this on the
    way back.
    """
    snapshot: dict[str, Any] = {}
    for raw_path in owned_paths:
        segments = parse_owned_path(raw_path)
        _extract_into(live, segments, snapshot)
    return snapshot


def _extract_into(
    live_node: Any, segments: tuple[_Segment, ...], snap_node: dict[str, Any]
) -> None:
    """Walk ``segments`` against ``live_node`` and graft results into ``snap_node``."""
    if not segments:
        return
    head, *tail = segments
    rest: tuple[_Segment, ...] = tuple(tail)

    if head[0] == "key":
        key = head[1]
        if not isinstance(live_node, dict) or key not in live_node:
            return
        if not rest:
            snap_node[key] = copy.deepcopy(live_node[key])
            return
        snap_child = snap_node.setdefault(key, {})
        if not isinstance(snap_child, dict):
            # An earlier owned-path produced a non-dict here. Owned paths
            # shouldn't normally collide; if they do, the later path wins.
            snap_child = {}
            snap_node[key] = snap_child
        _extract_into(live_node[key], rest, snap_child)

    elif head[0] == "iter":
        if not isinstance(live_node, dict):
            raise UnsupportedWalkTarget(
                "v1 '[]' only iterates JSON objects (string-keyed); "
                f"got {type(live_node).__name__}"
            )
        for k, v in live_node.items():
            if not rest:
                raise InvalidOwnedPath(
                    "v1 grammar does not allow '[]' as a leaf segment"
                )
            child_snap = snap_node.setdefault(k, {})
            if not isinstance(child_snap, dict):
                child_snap = {}
                snap_node[k] = child_snap
            _extract_into(v, rest, child_snap)


def apply_owned_paths(
    live: dict[str, Any],
    snapshot: dict[str, Any],
    owned_paths: tuple[str, ...],
) -> dict[str, Any]:
    """Overlay ``snapshot``'s owned subtrees onto ``live`` and return a new dict.

    Delete-on-absence (spec §3.4): for each owned path, if the snapshot has
    no value at that path, the corresponding live path is deleted. This is
    what closes the original MCP-leak class.

    For ``[]`` paths: iterate keys present in *live*'s parent object; for
    each key, look up the same key in the snapshot's parent object.
    Overwrite or delete the owned-leaf under that key based on snapshot
    presence. Then iterate keys present *only* in the snapshot so a profile
    that registered an MCP under a project that doesn't yet exist on this
    machine still gets restored.

    Never mutates ``live`` or ``snapshot``.
    """
    out = copy.deepcopy(live)
    for raw_path in owned_paths:
        segments = parse_owned_path(raw_path)
        _apply_segments(out, snapshot, segments)
    return out


def _apply_segments(
    live_node: dict[str, Any],
    snap_node: Any,
    segments: tuple[_Segment, ...],
) -> None:
    """Walk ``segments`` against ``live_node``, overlaying ``snap_node``.

    Both nodes are at the *same* logical position in their respective trees.
    """
    if not segments:
        return
    head, *tail = segments
    rest: tuple[_Segment, ...] = tuple(tail)

    if head[0] == "key":
        key = head[1]
        snap_has = isinstance(snap_node, dict) and key in snap_node
        snap_child: Any = snap_node[key] if snap_has else None

        if not rest:
            if snap_has:
                live_node[key] = copy.deepcopy(snap_child)
            else:
                live_node.pop(key, None)
            return

        if snap_has and key not in live_node:
            live_node[key] = {}
        if key not in live_node:
            return
        if not isinstance(live_node[key], dict):
            # Live has a scalar where the path expects descent; refuse to
            # overwrite — deleting would silently destroy a machine-global
            # value.
            return
        _apply_segments(live_node[key], snap_child if snap_has else {}, rest)

    elif head[0] == "iter":
        if not isinstance(live_node, dict):
            raise UnsupportedWalkTarget(
                "v1 '[]' only iterates JSON objects (string-keyed); "
                f"got {type(live_node).__name__}"
            )
        if not rest:
            # Mirror _extract_into: `[]` as a leaf has no defined semantics
            # in v1. Without this check, apply would silently no-op while
            # extract on the same path raises — asymmetric fail-fast.
            raise InvalidOwnedPath(
                "v1 grammar does not allow '[]' as a leaf segment"
            )
        snap_is_dict = isinstance(snap_node, dict)
        live_keys = set(live_node.keys())
        snap_keys = set(snap_node.keys()) if snap_is_dict else set()
        for k in live_keys | snap_keys:
            child_snap = snap_node[k] if snap_is_dict and k in snap_node else {}
            if k not in live_node:
                # Snapshot-only key: only materialize the live entry if the
                # snapshot subtree actually has owned data here. An empty
                # ``{}`` child_snap is a placeholder produced by extract when
                # the iter key existed but the owned leaf was absent at save
                # time; restoring it should not create a ghost entry on a
                # machine where the iter key never existed live.
                if not (isinstance(child_snap, dict) and child_snap):
                    continue
                live_node[k] = {}
            if not isinstance(live_node[k], dict):
                continue
            _apply_segments(live_node[k], child_snap, rest)
