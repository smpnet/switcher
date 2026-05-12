"""Exception hierarchy for switcher.

The CLI catches SwitcherError at the boundary, prints the message, and exits 1.
Any other exception bubbles as a real Python traceback (it's a bug).
"""


class SwitcherError(Exception):
    """Base class for all user-facing switcher errors."""


class StateAlreadyInitializedError(SwitcherError):
    """Raised by init() when the store already contains profiles."""


class StateNotInitializedError(SwitcherError):
    """Raised by mutating operations when the store has no profiles yet."""


class AlreadyLinkedError(SwitcherError):
    """Raised by init() when a live config dir is already a symlink/junction."""


class PathNotADirectoryError(SwitcherError):
    """Raised by init() when a live config path exists but is not a directory."""


class ProfileTargetExistsError(SwitcherError):
    """Raised by move_or_seed_dir() when the profile destination already exists.

    This guards against silent data loss on init reruns after a partial
    failure: rather than letting `live.replace(profile_target)` produce a
    raw OSError (or worse, succeed-and-overwrite-empty-target on POSIX),
    we surface a domain error the CLI can translate to clear advice.
    """


class UnknownProfileError(SwitcherError):
    """Raised when an operation references a profile that doesn't exist."""


class ProfileExistsError(SwitcherError):
    """Raised when create()/rename() targets a profile name already in use."""


class ProfileIsActiveError(SwitcherError):
    """Raised by delete() when the target profile is active for one or more tools."""


class UnknownToolError(SwitcherError):
    """Raised when an operation references a tool ID not in the registry."""


class ToolNotInProfileError(SwitcherError):
    """Raised when use --only includes a tool not present in the target profile."""


class ToolHasNoActiveProfileError(SwitcherError):
    """Raised by which() when the tool isn't in the active map."""


class StorageError(SwitcherError):
    """Raised on JSON corruption, IO failures, or other storage-layer problems."""


class UninstallPreflightError(SwitcherError):
    """`uninstall` pre-flight rejected the run (state corruption, missing data, or unsafe combination)."""


class RescanCaptureError(SwitcherError):
    """`rescan` per-tool capture failed and (where applicable) rollback also failed."""


class PruneError(SwitcherError):
    """`prune` orphan walk failed for a non-classification reason (e.g. permissions)."""


class NothingToInitializeError(SwitcherError):
    """Raised when init is given explicit filters (--only/--skip/--interactive)
    but the resolved target set is empty. Bare init with zero detected tools
    preserves v0.1.3 behavior (warn + empty profiles) and does NOT raise this."""


class ToolNotManagedError(SwitcherError):
    """Raised when a command operates on a tool id that's not in the active
    map. Surfaces by unmanage (no entry to remove), by use --only (tool not
    currently managed), and by tools-table pathological-row detection."""


class NoToolsManagedError(SwitcherError):
    """Raised when save / create / use is called with an empty active map.
    The state is valid (after `unmanage` of the last tool, or after
    `uninstall` without purge), but these mutating commands have no work
    to do; better to fail loud than create empty profiles silently."""


class OpLogCorruptError(SwitcherError):
    """Raised when oplog.json is unreadable: malformed JSON, empty file,
    fails Pydantic validation, or describes an in-flight op whose disk
    state is unrecognizable. Manual recovery required."""


class InitInProgressError(SwitcherError):
    """Raised when a mutating command runs while an interrupted init is
    detected in the op-log. The CLI handler surfaces the recovery hint
    (run `switcher init --continue` or `switcher init --abort`)."""


class RescanInProgressError(SwitcherError):
    """Same shape as InitInProgressError, for an interrupted rescan."""


class NoInProgressInitError(SwitcherError):
    """Raised when `switcher init --continue` or `--abort` is invoked
    but no in-flight `_InitOp` record is present. Distinct from
    StateAlreadyInitializedError (the latter means init already
    finished cleanly)."""


class NoInProgressRescanError(SwitcherError):
    """Same shape as NoInProgressInitError, for rescan."""


class AbortPreflightError(SwitcherError):
    """Raised by op-log abort when a mapping's pre-op state was a link
    or file (which init/rescan pre-flight rejects). Reaching this state
    at abort time means external drift since intent-write; refuse
    defensively rather than guess."""
