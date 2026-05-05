"""Verify junction creation + atomic os.replace on Windows.

The atomicity test runs a concurrent reader against a junction being replaced
and asserts the reader never observes a missing target -- only old or new.

Exits non-zero (1) if any verification fails or if the atomicity reader
observes any FileNotFoundError, since a non-zero miss count means
swap_link's Windows path needs the delete-then-create fallback documented
in spec section 4.
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


def test_create_junction(workdir: Path) -> bool:
    target = workdir / "target"
    target.mkdir()
    (target / "marker").write_text("hello")
    link = workdir / "link"
    try:
        import _winapi  # type: ignore[import-not-found]

        _winapi.CreateJunction(str(target), str(link))
    except Exception as e:
        print(f"FAIL _winapi.CreateJunction failed: {e}")
        return False
    if (link / "marker").read_text() != "hello":
        print("FAIL junction does not resolve to target contents")
        return False
    if not os.path.isjunction(link):
        print("FAIL os.path.isjunction did not recognize the junction")
        return False
    print("PASS _winapi.CreateJunction works")
    return True


def test_atomic_replace(workdir: Path) -> bool:
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
    if misses > 0:
        print(
            f"FAIL atomic replace; misses={misses}, seen={seen} -- "
            "atomicity hedge confirmed needed: "
            "switch to delete-then-create fallback"
        )
        return False
    print(f"PASS atomic replace; misses=0, seen={seen}")
    return True


def main() -> int:
    workdir = Path("./.junction-spike")
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir()
    # Each test gets its own subdir so a junction created by one test cannot
    # collide with the destination path of the next.
    try:
        create_dir = workdir / "create"
        create_dir.mkdir()
        atomic_dir = workdir / "atomic"
        atomic_dir.mkdir()
        results = [
            test_create_junction(create_dir),
            test_atomic_replace(atomic_dir),
        ]
        return 0 if all(results) else 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
