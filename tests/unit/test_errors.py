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
        UninstallPreflightError,
        RescanCaptureError,
        PruneError,
    ],
)
def test_all_errors_inherit_from_switcher_error(cls: type[Exception]) -> None:
    err = cls("test message")
    assert isinstance(err, SwitcherError)
    assert str(err) == "test message"


def test_switcher_error_is_exception() -> None:
    assert issubclass(SwitcherError, Exception)
