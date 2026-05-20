"""Packaging: verify both wheel and sdist ship the resources
`load_builtin_tools` needs. Source-tree unit tests can't catch the case
where pyproject.toml is misconfigured and the .toml files don't make it
into the installed artifact — this integration test builds each kind of
distribution and inspects it directly. Wheel and sdist use independent
inclusion rules in hatchling, so both are checked."""

from __future__ import annotations

import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.integration
def test_wheel_bundles_builtin_tomls(tmp_path: Path) -> None:
    out = tmp_path / "wheel"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--outdir",
            str(out),
            str(REPO_ROOT),
        ],
        check=True,
        capture_output=True,
    )
    wheels = list(out.glob("switcher-*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    with zipfile.ZipFile(wheels[0]) as zf:
        names = zf.namelist()
    # Every day-one builtin must be in the wheel — otherwise an installed
    # switcher would crash at first use of `load_builtin_tools()`.
    assert "switcher/builtins/claude.toml" in names
    assert "switcher/builtins/codex.toml" in names
    assert "switcher/builtins/copilot.toml" in names
    # No duplicate entries: a redundant force-include alongside the
    # `packages` directive used to ship every builtin twice. Hatch warns
    # but still builds the wheel, so a content-level check is the only
    # automated guard against regression.
    assert len(set(names)) == len(names), f"duplicate entries in wheel: {names}"


@pytest.mark.integration
def test_sdist_bundles_builtin_tomls(tmp_path: Path) -> None:
    """Source distributions use independent inclusion rules from wheels;
    a working wheel doesn't imply a working sdist. Users installing from
    a source tarball would otherwise hit a runtime crash on first use of
    `load_builtin_tools()` even though the wheel test passed."""
    out = tmp_path / "sdist"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--sdist",
            "--outdir",
            str(out),
            str(REPO_ROOT),
        ],
        check=True,
        capture_output=True,
    )
    sdists = list(out.glob("switcher-*.tar.gz"))
    assert len(sdists) == 1, f"expected exactly one sdist, got {sdists}"
    with tarfile.open(sdists[0]) as tf:
        names = tf.getnames()
    # sdists prefix entries with the project-version directory; match by suffix.
    suffixes = {n.split("/", 1)[1] if "/" in n else n for n in names}
    assert "src/switcher/builtins/claude.toml" in suffixes
    assert "src/switcher/builtins/codex.toml" in suffixes
    assert "src/switcher/builtins/copilot.toml" in suffixes
