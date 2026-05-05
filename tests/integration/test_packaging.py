"""Packaging: verify the built wheel ships the resources `load_builtin_tools`
needs. Source-tree unit tests can't catch the case where pyproject.toml is
misconfigured and the .toml files don't make it into the installed artifact —
this integration test builds the wheel and inspects it directly."""

from __future__ import annotations

import subprocess
import sys
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
    # Both day-one builtins must be in the wheel — otherwise an installed
    # switcher would crash at first use of `load_builtin_tools()`.
    assert "switcher/builtins/claude.toml" in names
    assert "switcher/builtins/copilot.toml" in names
    # No duplicate entries: a redundant force-include alongside the
    # `packages` directive used to ship every builtin twice. Hatch warns
    # but still builds the wheel, so a content-level check is the only
    # automated guard against regression.
    assert len(set(names)) == len(names), f"duplicate entries in wheel: {names}"
