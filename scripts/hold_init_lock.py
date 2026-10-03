"""Hold (or briefly hold then release) the DB init file lock for ops checks.

Usage::

    python scripts/hold_init_lock.py LOCK_PATH [SECONDS]

With no ``SECONDS`` the exclusive ``flock`` is held until the process is
stopped (e.g. ``docker compose stop init-lock-holder``). With ``SECONDS`` the
lock is released after that many seconds, so a worker retrying its
*non-blocking* init lock inside the bounded ``init_max_attempts`` budget can
proceed once the holder lets go.

This mirrors the production failure mode: an unrelated process that keeps an
exclusive lock on ``<db>.init.lock`` must make worker startup fail fast with
``SchemaUnavailable`` (bounded retries), never hang inside a blocking
``flock``.
"""
from __future__ import annotations

import fcntl
import sys
import time


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: hold_init_lock.py LOCK_PATH [SECONDS]", file=sys.stderr)
        return 2
    lock_path = sys.argv[1]
    hold_for = float(sys.argv[2]) if len(sys.argv) > 2 else None

    with open(lock_path, "a+") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        if hold_for is None:
            print(
                f"holding exclusive lock on {lock_path} until stopped",
                flush=True,
            )
            while True:
                time.sleep(3600)
        print(
            f"holding exclusive lock on {lock_path} for {hold_for}s, then releasing",
            flush=True,
        )
        time.sleep(hold_for)

    print(f"released exclusive lock on {lock_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
