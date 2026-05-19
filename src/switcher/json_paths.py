"""JSON owned-path walker for ConfigFile snapshots.

Implements the narrow jq-ish grammar from spec §3.4:

- ``.key`` — descend into an object key
- ``[]`` — iterate entries of a **JSON object** (string keys preserved)
- Composition: ``.projects[].mcpServers``

Not supported in v1: array iteration, filter predicates, wildcards,
recursive descent. Out-of-grammar tokens raise ``InvalidOwnedPathError``.

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


class InvalidOwnedPathError(ValueError):
    """Raised when an ``owned_json_paths`` entry doesn't parse."""


class UnsupportedWalkTargetError(ValueError):
    """Raised when the walker hits a JSON shape v1 doesn't support
    (e.g. ``[]`` applied to a JSON array rather than an object)."""


_Segment = tuple[str, ...]  # ("key", "name") or ("iter",)


def parse_owned_path(path: str) -> tuple[_Segment, ...]:
    """Parse an owned path string into a tuple of segments.

    Grammar (per spec §3.4)::

        path     := '.' KEY (subpath)*
        subpath  := '.' KEY | '[]' '.' KEY
        KEY      := matches ``[A-Za-z0-9_-]+`` (no dots — ``.`` is always a
                    segment separator; keys with dots are out of scope for v1)

    Concretely: paths start with a ``.KEY`` segment, may compose further
    ``.KEY`` and ``[]`` segments, and **must end on a ``.KEY`` segment**.
    ``[]`` as a leaf has no defined semantics in v1 (would mean "iterate
    every entry as a snapshot value") and is rejected here so the parser
    is the single source of truth — the walker's defense-in-depth check
    is then unreachable in practice.
    """
    if not path or not path.startswith("."):
        raise InvalidOwnedPathError(f"owned path must start with '.': {path!r}")

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
                raise InvalidOwnedPathError(f"empty key segment in {path!r}")
            if not _KEY_RE.fullmatch(key):
                raise InvalidOwnedPathError(
                    f"unsupported key {key!r} in {path!r}; v1 grammar is jq-ish "
                    "and does not support wildcards, filters, or recursion"
                )
            segments.append(("key", key))
            i = j
        elif ch == "[":
            if path[i : i + 2] != "[]":
                raise InvalidOwnedPathError(
                    f"unsupported token at {path[i:]!r}: v1 grammar only supports '[]', "
                    "not array indices, filters, or wildcards"
                )
            segments.append(("iter",))
            i += 2
            # Grammar (spec §3.4): subpath after '[]' is '.' KEY only.
            # Consecutive iter (``.a[][]``) and unprefixed continuation
            # (``.a[]b``) both have no v1 semantics — reject here so
            # ConfigFile's parse-based validator catches them at load.
            if i < len(path) and path[i] != ".":
                raise InvalidOwnedPathError(
                    f"'[]' must be followed by '.' KEY (got {path[i]!r}) "
                    f"in {path!r}; consecutive '[]' is not allowed"
                )
        else:
            raise InvalidOwnedPathError(f"unexpected character {ch!r} at position {i} in {path!r}")

    if segments and segments[-1][0] == "iter":
        raise InvalidOwnedPathError(
            f"owned path must not end with '[]': {path!r}; "
            "v1 grammar does not allow '[]' as a leaf segment"
        )
    return tuple(segments)


def extract_owned_paths(live: dict[str, Any], owned_paths: tuple[str, ...]) -> dict[str, Any]:
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
        # Non-leaf descent: live[key] must be a dict for the path to apply.
        # Eagerly creating ``snap_node[key] = {}`` here would manufacture a
        # placeholder that lies about descendability — apply on the unchanged
        # live would later see ``snap_has=True`` at this path with a non-dict
        # live value and raise ``UnsupportedWalkTargetError`` (the symmetric fix
        # in apply's r7). Skipping the setdefault keeps extract/apply
        # round-trip stable on malformed live state.
        #
        # If the next segment is ``iter``, fall through so the iter branch's
        # ``UnsupportedWalkTargetError`` fires on the non-dict — matches apply's
        # iter-fail-fast behavior and preserves the test that exercises
        # ``{"projects": []}`` shapes.
        if not isinstance(live_node[key], dict) and not (rest and rest[0][0] == "iter"):
            return
        snap_child = snap_node.setdefault(key, {})
        if not isinstance(snap_child, dict):
            # An earlier owned-path produced a non-dict here. Owned paths
            # shouldn't normally collide (ConfigFile validates non-overlap);
            # if they do, the later path wins.
            snap_child = {}
            snap_node[key] = snap_child
        _extract_into(live_node[key], rest, snap_child)

    elif head[0] == "iter":
        if not isinstance(live_node, dict):
            raise UnsupportedWalkTargetError(
                f"v1 '[]' only iterates JSON objects (string-keyed); got {type(live_node).__name__}"
            )
        for k, v in live_node.items():
            if not rest:
                raise InvalidOwnedPathError("v1 grammar does not allow '[]' as a leaf segment")
            # Mirror the keyed branch's descendability gate: if ``v`` is
            # not a dict, the descent under the next "key" segment would
            # silently skip — but the ``setdefault(k, {})`` would still
            # plant an empty placeholder, and apply would later interpret
            # that as "iter entry present in snapshot but owned leaf
            # absent" and DELETE the leaf from a healthy live (Hermes
            # pass-PR-3 blocker). For example, live
            # ``{"projects": {"/repo": 1}}`` paired with
            # ``.projects[].mcpServers`` would extract to
            # ``{"projects": {"/repo": {}}}``, then a later apply onto
            # ``{"projects": {"/repo": {"mcpServers": {"keep": {}}}}}``
            # would delete ``mcpServers`` from /repo.
            #
            # If the next segment is also ``iter``, fall through so the
            # nested iter branch raises ``UnsupportedWalkTargetError`` on
            # the non-dict — matches the keyed branch's fail-loud
            # behavior on iter-into-non-object.
            if not isinstance(v, dict) and not (rest and rest[0][0] == "iter"):
                continue
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

        # Non-leaf descent: if snap has a value here it must be a dict.
        # A scalar/list at this point is a malformed snapshot (corrupted
        # on disk or hand-edited); treating it as "absence" would silently
        # delete owned data downstream. Raise so the user sees corruption
        # rather than data loss.
        if snap_has and not isinstance(snap_child, dict):
            raise UnsupportedWalkTargetError(
                f"snapshot at key {key!r} has type "
                f"{type(snap_child).__name__}, expected JSON object for "
                "non-leaf descent (snapshot is likely corrupted)"
            )

        created = False
        if snap_has and key not in live_node:
            live_node[key] = {}
            created = True
        if key not in live_node:
            return
        if not isinstance(live_node[key], dict):
            # Live has a non-dict at a non-leaf descent point. Three sub-cases:
            #
            # - Next segment is ``iter``: fall through so the iter branch's
            #   ``UnsupportedWalkTargetError`` fires — iter on a non-dict is
            #   structurally undefined regardless of snapshot state, and
            #   matches extract's behavior.
            #
            # - Next segment is ``key`` and snapshot has data to write below:
            #   raise. Overwriting would destroy a machine-global scalar;
            #   skipping would silently drop owned data the snapshot was
            #   trying to restore. Surface the mismatch to the user.
            #
            # - Next segment is ``key`` and snapshot has no data below
            #   (delete-on-absence path): return silently. The path doesn't
            #   apply to current live shape; preserving the scalar matches
            #   spec §3.4's empty-after-delete philosophy.
            if rest and rest[0][0] == "iter":
                pass
            elif snap_has:
                raise UnsupportedWalkTargetError(
                    f"path requires descent through key {key!r}, but live has "
                    f"a {type(live_node[key]).__name__} here; snapshot has owned "
                    "data below this point — overwriting would destroy "
                    "machine-global state, skipping would drop owned data"
                )
            else:
                return
        _apply_segments(live_node[key], snap_child if snap_has else {}, rest)
        if created and not live_node[key]:
            # We created this container just to descend; recursion wrote
            # nothing. Pruning keeps "machine never had this key" stable
            # under restore. This is distinct from spec §3.4's
            # "empty-after-delete preservation" rule — there the container
            # already existed in live; here it didn't.
            del live_node[key]

    elif head[0] == "iter":
        # Static type narrows ``live_node`` to ``dict[str, Any]``, but the
        # recursive descent at the "key" branch above passes ``live_node[key]``
        # (typed ``Any``) into the next ``_apply_segments`` call — meaning the
        # actual runtime value here can be a scalar / list when the snapshot
        # disagrees with the schema. The isinstance check is defensive
        # runtime validation, NOT a static narrowing redundancy.
        if not isinstance(live_node, dict):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise UnsupportedWalkTargetError(
                f"v1 '[]' only iterates JSON objects (string-keyed); got {type(live_node).__name__}"
            )
        if not rest:
            # Defense in depth — ``parse_owned_path`` rejects leaf ``[]``,
            # so the only way to land here is a direct walker call with a
            # hand-built segment tuple (test fixtures, future callers).
            raise InvalidOwnedPathError("v1 grammar does not allow '[]' as a leaf segment")
        snap_is_dict = isinstance(snap_node, dict)
        # Iterate live keys in their existing dict order first, then append
        # snapshot-only keys in snapshot dict order. ``set | set`` would
        # produce a hash-derived order, and although our serializer uses
        # ``sort_keys=True`` so the on-disk file is stable regardless, the
        # walker shouldn't bake in an assumption about the caller's
        # serialization. Stable iteration here keeps walker output
        # order-independent of set hashing.
        ordered: list[str] = list(live_node.keys())
        seen = set(ordered)
        if snap_is_dict:
            for k in snap_node:
                if k not in seen:
                    ordered.append(k)
        for k in ordered:
            child_snap = snap_node[k] if snap_is_dict and k in snap_node else {}
            # Non-leaf descent: snap entry at this iter key must be a dict
            # if it's present. A scalar/list is malformed snapshot data
            # (rest is non-empty here per the leaf-iter check above), and
            # treating it as absence would silently delete owned data.
            if snap_is_dict and k in snap_node and not isinstance(child_snap, dict):
                raise UnsupportedWalkTargetError(
                    f"snapshot at iter key {k!r} has type "
                    f"{type(child_snap).__name__}, expected JSON object "
                    "(snapshot is likely corrupted)"
                )
            created = False
            if k not in live_node:
                live_node[k] = {}
                created = True
            if not isinstance(live_node[k], dict):
                # Same rule as the key branch (r7/r8): descent blocked at
                # this iter entry. If the snapshot has owned data here,
                # raise — overwriting destroys machine-global; skipping
                # silently loses owned data. If the snapshot has nothing
                # to write (delete-on-absence or empty placeholder),
                # preserve the scalar silently.
                if isinstance(child_snap, dict) and child_snap:
                    raise UnsupportedWalkTargetError(
                        f"iter entry {k!r} requires descent, but live has "
                        f"a {type(live_node[k]).__name__} here; snapshot "
                        "has owned data below this point — overwriting "
                        "would destroy machine-global state, skipping "
                        "would drop owned data"
                    )
                continue
            _apply_segments(live_node[k], child_snap, rest)
            if created and not live_node[k]:
                # Same rule as the key branch: a container we created
                # solely to descend and that gained no content under
                # recursion gets pruned, so a snapshot's iter placeholder
                # (``{"/r/A": {}}``) doesn't materialize ghost entries on
                # a machine that never had that iter key.
                del live_node[k]
