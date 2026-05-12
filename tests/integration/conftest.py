"""Shared helpers for integration tests."""

from __future__ import annotations

from pathlib import Path


def install_two_dir_copilot_override(tmp_state: Path) -> None:
    """Install a user-local copilot.toml that re-introduces the legacy
    two-dir copilot shape (copilot-config + copilot-auth).

    Why: the v0.1.4 builtin copilot.toml targets only the standalone
    `copilot` binary's single config dir (`~/.copilot`). Tests that
    exercise multi-dir capture/rollback/seeding/registry-shrink/reorder
    semantics need a two-dir tool registered. Rather than depend on a
    builtin no longer shipped, each test that needs it calls this
    helper before constructing its service.

    Order matches the legacy pre-rewrite bundled builtin:
    config_dirs[0] = ~/.config/github-copilot (POSIX) or
        %LOCALAPPDATA%\\github-copilot (Windows) → copilot-auth.
    config_dirs[1] = ~/.copilot → copilot-config.

    The order is load-bearing: tests reference COPILOT_FIRST_DIR (= the
    [0] live path) for detection setup and COPILOT_SECOND_DIR (= the [1]
    live path) for seeded-mapping assertions. Swapping the order would
    silently invert which is 'first' and break those assertions.
    """
    registry_d = tmp_state / "registry.d"
    registry_d.mkdir(parents=True, exist_ok=True)
    (registry_d / "copilot.toml").write_text(
        'id = "copilot"\n'
        'name = "GitHub Copilot CLI (test two-dir override)"\n'
        "[[config_dirs]]\n"
        'posix_path = "~/.config/github-copilot"\n'
        'windows_path = "%LOCALAPPDATA%\\\\github-copilot"\n'
        'profile_subdir = "copilot-auth"\n'
        "[[config_dirs]]\n"
        'posix_path = "~/.copilot"\n'
        'windows_path = "%USERPROFILE%\\\\.copilot"\n'
        'profile_subdir = "copilot-config"\n'
    )
