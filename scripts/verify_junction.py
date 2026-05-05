"""Verify junction creation + atomic os.replace on Windows.

The atomicity test runs a concurrent reader against a junction being replaced
and reports whether the reader ever observes a missing or unreadable target.

This is a *probe*, not a proof. A non-zero miss count is real evidence that
the swap is unsafe under contention -- pessimistic, deterministic, and
actionable: swap_link's Windows path needs the delete-then-create fallback
documented in spec section 4. A zero miss count is *probabilistic
confidence* over 200 swap iterations and one reader, not a guarantee that
no race window exists. Maintainers reading a green run should treat it as
'no gross atomicity failure observed,' not 'atomicity proven.' If atomic
replacement turns out to be load-bearing for production safety, prefer the
fallback unconditionally rather than relying on this probe's silence.

Exits non-zero (1) if any verification fails, the reader observes any
OSError during the swap window (FileNotFoundError, PermissionError, etc.),
or the writer hits an OSError mid-loop (reported with the iteration index
for diagnosis).
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

    # Synchronization primitives: a script that exists to reason about race
    # safety mustn't introduce its own unsynchronized cross-thread state.
    # `state_lock` guards every read/write of `misses` and `seen` from main
    # vs. reader. `saw_initial_a` is the explicit signal main waits on
    # before starting swaps, instead of polling `seen`.
    misses = 0
    seen: set[str] = set()
    state_lock = threading.Lock()
    stop = threading.Event()
    saw_initial_a = threading.Event()

    def reader() -> None:
        nonlocal misses
        while not stop.is_set():
            try:
                value = (link / "v").read_text()
                with state_lock:
                    seen.add(value)
                if value == "A":
                    saw_initial_a.set()
            except OSError:
                # Catch any read-window failure, not only FileNotFoundError --
                # PermissionError or other transient OSErrors during the swap
                # also indicate the path isn't safely readable through the
                # replacement, which is what we're trying to detect.
                with state_lock:
                    misses += 1
            time.sleep(0.0001)

    writer_errors: list[str] = []
    t = threading.Thread(target=reader)
    t.start()

    # Wait for the reader to observe the initial 'A' before starting swaps,
    # so the {A, B} coverage check below isn't sensitive to thread-scheduling
    # delays at startup. If the reader can't even observe the initial junction
    # within a generous deadline, something is structurally broken and the
    # whole atomicity claim is moot -- fail loudly rather than continue into
    # a 200-iter loop that would just confirm the same thing.
    if not saw_initial_a.wait(timeout=2.0):
        stop.set()
        t.join()
        print("FAIL atomic replace; reader never observed the initial 'A'")
        return False

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
                # First writer error guarantees a FAIL verdict; bail out
                # rather than burn through the remaining iterations
                # accumulating duplicate noise.
                break
    finally:
        stop.set()
        t.join()

    # Snapshot the shared state under the lock. After t.join() the reader
    # has terminated and Python's join() provides a happens-before edge,
    # but going through the same lock the reader used keeps the
    # synchronization story consistent and makes the contract obvious to
    # anyone editing the script later.
    with state_lock:
        final_misses = misses
        final_seen = set(seen)

    # Both target values must be observed for the run to mean anything: a
    # green miss count alongside `seen={"A"}` would prove only that no read
    # error happened while one value was visible, NOT that the reader
    # successfully traversed any actual swap. abby round 13 caught this as
    # a false-positive surface; require {"A", "B"} so a one-sided run fails
    # loudly instead of masquerading as evidence of atomic behavior.
    expected_seen = {"A", "B"}
    incomplete_observation = final_seen != expected_seen
    if writer_errors or final_misses > 0 or incomplete_observation:
        print(
            f"FAIL atomic replace; misses={final_misses}, "
            f"writer_errors={len(writer_errors)}, seen={final_seen}"
        )
        for err in writer_errors[:10]:
            print(f"  writer: {err}")
        if final_misses > 0:
            print(
                "  reader: atomicity hedge confirmed needed -- "
                "switch to delete-then-create fallback"
            )
        if incomplete_observation:
            missing = expected_seen - final_seen
            print(
                f"  reader: only observed {sorted(final_seen)!r}; expected to see "
                f"both 'A' and 'B' across the swap window (missing: "
                f"{sorted(missing)!r}). Run did not actually exercise the "
                f"swap -- treat the result as inconclusive."
            )
        return False
    print(f"PASS atomic replace; misses=0, seen={final_seen}")
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
