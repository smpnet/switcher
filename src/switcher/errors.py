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
