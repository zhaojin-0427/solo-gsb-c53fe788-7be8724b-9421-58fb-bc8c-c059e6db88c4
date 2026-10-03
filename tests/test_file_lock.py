"""Regression tests for the non-blocking init file lock.

A separate process that keeps holding ``<db>.init.lock`` must not leave a
worker stuck forever inside a blocking ``flock``:

* the lock attempt is non-blocking and shares the ``init_max_attempts`` /
  backoff budget with SQLite busy errors, so a persistent holder results in a
  bounded ``SchemaUnavailable`` rather than an unbounded startup hang;
* when the holder releases within the budget, the waiter proceeds, builds the
  schema serially and sees the complete schema;
* a lock taken by the init path is released even when the guarded DDL raises.
"""
import dataclasses
import multiprocessing
import os
import time

import pytest

from app import config
from app import db

try:
    import fcntl  # noqa: F401

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover - POSIX only
    _HAS_FCNTL = False


def _patch_settings(monkeypatch, tmp_path, **overrides):
    """Swap in a frozen Settings copy (Settings is a frozen dataclass)."""
    overrides.setdefault("db_path", str(tmp_path / "tiles.db"))
    new_settings = dataclasses.replace(config.settings, **overrides)
    monkeypatch.setattr(db, "settings", new_settings)
    return new_settings


@pytest.mark.skipif(not _HAS_FCNTL, reason="requires fcntl/flock")
def test_init_file_lock_is_non_blocking(tmp_path, monkeypatch):
    _patch_settings(monkeypatch, tmp_path)
    import fcntl

    with open(str(tmp_path / "tiles.db.init.lock"), "a+") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        with pytest.raises(db.InitLockUnavailable):
            with db._init_file_lock():
                pass
        # A blocking flock would never return; the non-blocking one fails at
        # once instead of waiting for the holder to release.
        assert time.monotonic() - started < 1.0


@pytest.mark.skipif(not _HAS_FCNTL, reason="requires fcntl/flock")
def test_init_file_lock_releases_after_body_succeeds(tmp_path, monkeypatch):
    _patch_settings(monkeypatch, tmp_path)
    with db._init_file_lock():
        pass
    # Re-entering once released must work immediately.
    with db._init_file_lock():
        pass


# ---------------------------------------------------------------------------
# Cross-process: a real external holder of <db>.init.lock
# ---------------------------------------------------------------------------


def _hold_lock_forever(db_path, acquired):
    import fcntl

    fh = open(db_path + ".init.lock", "a+")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    acquired.set()
    # Keep the flock until the process is terminated by the parent.
    while True:
        time.sleep(3600)


def _hold_then_release(db_path, acquired, release_after, done):
    import fcntl

    fh = open(db_path + ".init.lock", "a+")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    acquired.set()
    deadline = time.monotonic() + release_after
    while time.monotonic() < deadline:
        time.sleep(0.02)
    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    fh.close()
    done.wait(timeout=30)


def _child_init(db_path, queue):
    os.environ["VT_DB_PATH"] = db_path
    import importlib

    from app import config as config_mod
    from app import db as db_mod

    importlib.reload(config_mod)
    importlib.reload(db_mod)
    started = time.monotonic()
    try:
        db_mod.init_db()
        with db_mod.connection() as conn:
            mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            tables = sorted(
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            )
        queue.put(("ok", round(time.monotonic() - started, 2), mode, tables))
    except Exception as exc:
        queue.put(
            (
                "error",
                type(exc).__name__,
                str(exc),
                round(time.monotonic() - started, 2),
            )
        )


def test_persistent_lock_holder_bounds_startup(tmp_path, monkeypatch):
    """init_db must give up with SchemaUnavailable instead of hanging."""
    db_path = str(tmp_path / "tiles.db")
    ctx = multiprocessing.get_context("spawn")
    acquired = ctx.Event()
    queue = ctx.Queue()

    holder = ctx.Process(target=_hold_lock_forever, args=(db_path, acquired))
    holder.start()
    assert acquired.wait(timeout=10)

    # Tight retry budget in the child: failure must be quick and clearly
    # bounded (the default 8-attempt budget would also work, just slower).
    monkeypatch.setenv("VT_INIT_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("VT_INIT_RETRY_DELAY_S", "0.02")
    worker = ctx.Process(target=_child_init, args=(db_path, queue))
    worker.start()
    worker.join(timeout=30)

    assert not worker.is_alive(), "worker hung on the blocking init lock"
    assert worker.exitcode == 0, "worker should report failure via the queue, not crash"

    outcome = queue.get_nowait()
    assert outcome[0] == "error", outcome
    assert outcome[1] == "SchemaUnavailable", outcome
    assert outcome[3] < 15, outcome  # bounded: no unbounded flock wait

    holder.terminate()
    holder.join(timeout=10)


def test_lock_released_within_budget_allows_startup(tmp_path, monkeypatch):
    """Once the holder lets go, the waiting worker builds the full schema."""
    db_path = str(tmp_path / "tiles.db")
    ctx = multiprocessing.get_context("spawn")
    acquired = ctx.Event()
    done = ctx.Event()
    queue = ctx.Queue()

    # Holds the lock for ~0.3s: the worker retries the non-blocking attempt
    # and proceeds once it is released.
    holder = ctx.Process(
        target=_hold_then_release, args=(db_path, acquired, 0.3, done)
    )
    holder.start()
    assert acquired.wait(timeout=10)

    monkeypatch.setenv("VT_INIT_MAX_ATTEMPTS", "20")
    monkeypatch.setenv("VT_INIT_RETRY_DELAY_S", "0.05")
    worker = ctx.Process(target=_child_init, args=(db_path, queue))
    worker.start()
    worker.join(timeout=30)
    assert worker.exitcode == 0, "worker should initialise after lock release"

    outcome = queue.get_nowait()
    assert outcome[0] == "ok", outcome
    _, elapsed, mode, tables = outcome
    assert mode == "wal", outcome
    assert {"datasets", "revisions", "tile_cache"} <= set(tables), outcome
    assert elapsed >= 0.1, outcome  # it really retried behind the holder

    done.set()
    holder.join(timeout=10)

    # The database now holds exactly the schema the winning worker committed.
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name, COUNT(*) FROM sqlite_master GROUP BY name"
        ).fetchall()
        assert all(count == 1 for _, count in rows), rows
        assert {"datasets", "revisions", "tile_cache"} <= {name for name, _ in rows}
    finally:
        conn.close()


def _hold_then_fail_init(db_path, queue):
    """Enter the init lock, raise inside it, then prove the flock was freed."""
    os.environ["VT_DB_PATH"] = db_path
    import fcntl

    from app import db as db_mod

    try:
        with db_mod._init_file_lock():
            raise RuntimeError("simulated DDL failure")
    except RuntimeError:
        pass
    # A fresh open can only re-lock immediately if the context manager
    # released on the exception; a leaked lock would make LOCK_NB fail.
    fh = open(db_path + ".init.lock", "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        queue.put("relocked")
    except OSError:
        queue.put("still-locked")


def test_lock_is_released_when_guarded_section_raises(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()

    proc = ctx.Process(
        target=_hold_then_fail_init,
        args=(str(tmp_path / "tiles.db"), queue),
    )
    proc.start()
    proc.join(timeout=15)
    assert proc.exitcode == 0
    assert queue.get_nowait() == "relocked"
