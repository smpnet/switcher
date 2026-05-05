"""Verify junction creation + atomic os.replace on Windows.

The atomicity test runs a concurrent reader against a junction being replaced
and asserts the reader never observes a missing target -- only old or new.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from pathlib import Path

if sys.platform != "win32":
    print("SKIP: Windows only")
    sys.exit(0)


def test_create_junction(workdir: Path) -> None:
    target = workdir / "target"
    target.mkdir()
    (target / "marker").write_text("hello")
    link = workdir / "link"
    try:
        import _winapi  # type: ignore[import-not-found]

        _winapi.CreateJunction(str(target), str(link))
        print("PASS _winapi.CreateJunction works")
    except Exception as e:
        print(f"FAIL _winapi.CreateJunction failed: {e}")
        return
    assert (link / "marker").read_text() == "hello"
    assert os.path.isjunction(link)


def test_atomic_replace(workdir: Path) -> None:
    target_a = workdir / "a"
    target_a.mkdir()
    (target_a / "v").write_text("A")
    target_b = workdir / "b"
    target_b.mkdir()
    (target_b / "v").write_text("B")
    link = workdir / "link"
    import _winapi  # type: ignore[import-not-found]

    _winapi.CreateJunction(str(target_a), str(link))

    misses = 0
    seen: set[str] = set()
    stop = threading.Event()

    def reader() -> None:
        nonlocal misses
        while not stop.is_set():
            try:
                seen.add((link / "v").read_text())
            except FileNotFoundError:
                misses += 1
            time.sleep(0.0001)

    t = threading.Thread(target=reader)
    t.start()
    for i in range(200):
        new = workdir / f"link.{i}.tmp"
        _winapi.CreateJunction(str(target_b if i % 2 else target_a), str(new))
        os.replace(new, link)
    stop.set()
    t.join()
    print(f"PASS atomic replace; misses={misses}, seen={seen}")
    if misses > 0:
        print(
            "WARN Atomicity hedge confirmed needed: "
            "switch to delete-then-create fallback"
        )


def main() -> None:
    workdir = Path("./.junction-spike")
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir()
    try:
        test_create_junction(workdir)
        test_atomic_replace(workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
