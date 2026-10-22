"""Manual offline check for T-101's LocalArtifactStore and lease (append-line safety,
lease takeover, runs pull). Run: python -m app.manual_tests.t101_artifact_store_check
"""

import shutil
import sys
import threading
from pathlib import Path

from app.store.artifacts import LocalArtifactStore, NotFound
from app.store.lease import FileLease, LeaseBusy
from app.store.pull import pull_run


def main() -> int:
    root = Path("runs") / "_t101_artifact_check"
    if root.exists():
        shutil.rmtree(root)
    run_id = "probe"
    store = LocalArtifactStore(root)

    # write_atomic + read_bytes
    store.write_atomic(run_id, "probe/hello.txt", b"hi")
    assert store.read_bytes(run_id, "probe/hello.txt") == b"hi"
    assert store.exists(run_id, "probe/hello.txt")
    assert not store.exists(run_id, "probe/nope.txt")
    try:
        store.read_bytes(run_id, "probe/nope.txt")
        print("FAIL: expected NotFound")
        return 1
    except NotFound:
        pass

    # append_line concurrency: 5 threads x 20 lines = 100 lines, none lost
    log_rel = "probe/log.jsonl"
    errors = []

    def worker(n: int) -> None:
        try:
            for i in range(20):
                store.append_line(run_id, log_rel, f"thread {n} line {i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        print(f"FAIL: {len(errors)} worker errors: {errors[:3]}")
        return 1
    lines = store.read_bytes(run_id, log_rel).decode("utf-8").splitlines()
    if len(lines) != 100:
        print(f"FAIL: expected 100 lines, got {len(lines)}")
        return 1
    print(f"OK: {len(lines)} lines written by 5 concurrent threads, none lost")

    # list() and copy_tree()
    paths = store.list(run_id, "")
    assert set(paths) == {"probe/hello.txt", "probe/log.jsonl"}, paths
    print(f"OK: list() -> {sorted(paths)}")

    # local_path()
    assert store.local_path(run_id, "probe/hello.txt") is not None

    # lease takeover: an expired lease is replaced, not waited out for 30s
    lease = FileLease(root)
    token = lease.acquire(run_id, "owner-a", seconds=0)  # already expired
    import time

    time.sleep(0.05)
    start = time.monotonic()
    token2 = lease.acquire(run_id, "owner-b", seconds=60, wait=25)
    elapsed = time.monotonic() - start
    if elapsed > 5:
        print(f"FAIL: expired-lease takeover took {elapsed:.1f}s, expected near-instant")
        return 1
    lease.release(run_id, "owner-b")
    print(f"OK: expired lease taken over in {elapsed:.2f}s")

    # lease contention: a held, non-expired lease blocks until the wait elapses
    lease.acquire(run_id, "owner-c", seconds=5)
    start = time.monotonic()
    try:
        lease.acquire(run_id, "owner-d", seconds=5, wait=1)
        print("FAIL: expected LeaseBusy")
        return 1
    except LeaseBusy as exc:
        elapsed = time.monotonic() - start
        print(f"OK: LeaseBusy raised after {elapsed:.2f}s ({exc})")
    lease.release(run_id, "owner-c")

    # runs pull
    dest = root / "_pulled"
    count = pull_run(store, run_id, dest)
    assert count == 2, count
    assert (dest / "probe" / "hello.txt").read_bytes() == b"hi"
    print(f"OK: runs pull copied {count} files to {dest}")

    shutil.rmtree(root)
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
