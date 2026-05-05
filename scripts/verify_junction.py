"""Verify junction creation + atomic os.replace on Windows.

The atomicity test runs a concurrent reader against a junction being replaced
and asserts the reader never observes a missing or unreadable target -- only
old or new.

Exits non-zero (1) if any verification fails or if the atomicity reader
observes any OSError during the swap window (FileNotFoundError,
PermissionError, etc.). A non-zero miss count means swap_link's Windows
path needs the delete-then-create fallback documented in spec section 4;
writer-side OSErrors during the loop also trip the same failure mode and
are reported with their iteration index for diagnosis.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path


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
            except OSError:
                # Catch any read-window failure, not only FileNotFoundError --
                # PermissionError or other transient OSErrors during the swap
                # also indicate the path isn't safely readable through the
                # replacement, which is what we're trying to detect.
                misses += 1
            time.sleep(0.0001)

    writer_errors: list[str] = []
    t = threading.Thread(target=reader)
    t.start()
    try:
        for i in range(200):
            new = workdir / f"link.{i}.tmp"
            try:
                _winapi.CreateJunction(
                    str(target_b if i % 2 else target_a), str(new)
                )
                os.replace(new, link)
            except OSError as e:
                # Surface writer-side errors as a deterministic FAIL instead
                # of an untrapped exception. We don't tolerate or retry them
                # -- if CreateJunction or os.replace can't run cleanly under
                # contention, that's a real signal that swap_link's Windows
                # path needs the delete-then-create fallback (spec §4), same
                # category as a non-zero reader miss count. The catch just
                # ensures a useful summary line gets printed instead of a
                # mid-loop traceback drowning the actual failure mode.
                writer_errors.append(f"iter {i}: {type(e).__name__}: {e}")
    finally:
        stop.set()
        t.join()
    if writer_errors or misses > 0:
        print(
            f"FAIL atomic replace; misses={misses}, "
            f"writer_errors={len(writer_errors)}, seen={seen}"
        )
        for err in writer_errors[:10]:
            print(f"  writer: {err}")
        if misses > 0:
            print(
                "  reader: atomicity hedge confirmed needed -- "
                "switch to delete-then-create fallback"
            )
        return False
    print(f"PASS atomic replace; misses=0, seen={seen}")
    return True


def main() -> int:
    if sys.platform != "win32":
        print("SKIP: Windows only")
        return 0
    # tempfile.mkdtemp instead of a relative path so the spike works regardless
    # of which CWD pixi run / the CI runner happens to invoke us from.
    workdir = Path(tempfile.mkdtemp(prefix="junction-spike-"))
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
