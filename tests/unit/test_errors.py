"""All error classes derive from SwitcherError and carry a message."""

import pytest

from switcher.errors import (
    AlreadyLinkedError,
    PathNotADirectoryError,
    ProfileExistsError,
    ProfileIsActiveError,
    ProfileTargetExistsError,
    PruneError,
    RescanCaptureError,
    StateAlreadyInitializedError,
    StateNotInitializedError,
    StorageError,
    SwitcherError,
    ToolHasNoActiveProfileError,
    ToolNotInProfileError,
    UninstallPreflightError,
    UnknownProfileError,
    UnknownToolError,
)


@pytest.mark.parametrize(
    "cls",
    [
        StateAlreadyInitializedError,
        StateNotInitializedError,
        AlreadyLinkedError,
        PathNotADirectoryError,
        UnknownProfileError,
        ProfileExistsError,
        ProfileTargetExistsError,
        ProfileIsActiveError,
        UnknownToolError,
        ToolNotInProfileError,
        ToolHasNoActiveProfileError,
        StorageError,
    ],
)
def test_all_errors_inherit_from_switcher_error(cls: type[Exception]) -> None:
    err = cls("test message")
    assert isinstance(err, SwitcherError)
    assert str(err) == "test message"


def test_switcher_error_is_exception() -> None:
    assert issubclass(SwitcherError, Exception)


def test_uninstall_preflight_error_inherits_switcher_error():
    assert issubclass(UninstallPreflightError, SwitcherError)


def test_rescan_capture_error_inherits_switcher_error():
    assert issubclass(RescanCaptureError, SwitcherError)


def test_prune_error_inherits_switcher_error():
    assert issubclass(PruneError, SwitcherError)


def test_uninstall_preflight_error_carries_message():
    err = UninstallPreflightError("test")
    assert str(err) == "test"


def test_rescan_capture_error_carries_message():
    err = RescanCaptureError("test")
    assert str(err) == "test"


def test_prune_error_carries_message():
    err = PruneError("test")
    assert str(err) == "test"
