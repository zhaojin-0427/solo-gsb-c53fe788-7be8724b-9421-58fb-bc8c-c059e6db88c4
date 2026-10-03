"""Regression tests for concurrent first-touch schema initialisation.

Docker Compose starts two uvicorn worker processes that share one SQLite file.
On a *fresh* data volume both run lifespan's ``init_db`` at the same instant;
the WAL switch and DDL must be serialised/retried so both workers finish with
the same schema, instead of one dying with "database is locked".
"""
import dataclasses
import multiprocessing
import os
import sqlite3

import pytest

from app import config
from app import db


def _patch_settings(monkeypatch, tmp_path, **overrides):
    """Swap in a frozen Settings copy (Settings is a frozen dataclass)."""
    overrides.setdefault("db_path", str(tmp_path / "tiles.db"))
    new_settings = dataclasses.replace(config.settings, **overrides)
    monkeypatch.setattr(db, "settings", new_settings)
    return new_settings


def _open(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def test_init_db_is_idempotent_and_enables_wal(tmp_path, monkeypatch):
    nested = tmp_path / "nested" / "tiles.db"
    new_settings = dataclasses.replace(config.settings, db_path=str(nested))
    monkeypatch.setattr(db, "settings", new_settings)
    db.init_db()
    db.init_db()  # second worker / restart must not error or duplicate

    conn = _open(str(nested))
    try:
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"datasets", "revisions", "tile_cache"} <= tables
        # No duplicate objects of any kind survived.
        names = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master")]
        assert len(names) == len(set(names))
    finally:
        conn.close()


def test_init_db_retries_busy_then_succeeds(tmp_path, monkeypatch):
    _patch_settings(
        monkeypatch,
        tmp_path,
        init_max_attempts=3,
        init_retry_delay_s=0.0,
    )

    original_connect = db._connect
    calls = {"n": 0}

    def flaky_connect():
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return original_connect()

    monkeypatch.setattr(db, "_connect", flaky_connect)
    db.init_db()
    assert calls["n"] >= 2


def test_init_db_retries_when_schema_not_yet_visible(tmp_path, monkeypatch):
    _patch_settings(
        monkeypatch,
        tmp_path,
        init_max_attempts=3,
        init_retry_delay_s=0.0,
    )

    real_assert = db._assert_db_ready
    calls = {"n": 0}

    def flaky_assert(conn):
        calls["n"] += 1
        if calls["n"] == 1:
            raise db.SchemaUnavailable(
                "schema initialisation incomplete, missing: ['datasets']"
            )
        return real_assert(conn)

    monkeypatch.setattr(db, "_assert_db_ready", flaky_assert)
    db.init_db()
    assert calls["n"] == 2


def test_init_db_gives_up_after_bounded_attempts(tmp_path, monkeypatch):
    _patch_settings(
        monkeypatch,
        tmp_path,
        init_max_attempts=3,
        init_retry_delay_s=0.0,
    )

    calls = {"n": 0}

    def always_busy():
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "_connect", always_busy)
    with pytest.raises(db.SchemaUnavailable):
        db.init_db()
    assert calls["n"] == 3


def test_init_db_non_busy_error_is_not_retried(tmp_path, monkeypatch):
    _patch_settings(monkeypatch, tmp_path)

    calls = {"n": 0}

    def broken_connect():
        calls["n"] += 1
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(db, "_connect", broken_connect)
    with pytest.raises(sqlite3.OperationalError):
        db.init_db()
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Cross-process: mirrors the real two-worker Compose first-touch race.
# ---------------------------------------------------------------------------


def _init_worker(db_path, ready, start, result):
    os.environ["VT_DB_PATH"] = db_path
    # Reload configuration so the env var is picked up in the spawned process.
    import importlib

    from app import config as config_mod
    from app import db as db_mod

    importlib.reload(config_mod)
    importlib.reload(db_mod)
    ready.wait()
    start.wait()
    try:
        db_mod.init_db()
        with db_mod.connection() as conn:
            mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ]
        result.put(("ok", mode, sorted(tables)))
    except Exception as exc:  # pragma: no cover - surfaces the raw failure
        result.put(("error", f"{type(exc).__name__}: {exc}", []))


@pytest.mark.skipif(
    "spawn" not in multiprocessing.get_all_start_methods(),
    reason="cross-process test requires the 'spawn' start method",
)
def test_two_processes_initialise_fresh_database_concurrently(tmp_path):
    db_path = str(tmp_path / "tiles.db")
    ctx = multiprocessing.get_context("spawn")
    # 3-party barriers: each child signals readiness / waits for release
    # together with the parent, so both workers call init_db at the same time.
    ready = ctx.Barrier(3)
    start = ctx.Barrier(3)
    result = ctx.Queue()

    procs = [
        ctx.Process(target=_init_worker, args=(db_path, ready, start, result))
        for _ in range(2)
    ]
    for proc in procs:
        proc.start()
    ready.wait(timeout=30)
    start.wait(timeout=30)
    for proc in procs:
        proc.join(timeout=60)
        assert proc.exitcode == 0, f"worker exited with {proc.exitcode}"

    outcomes = [result.get_nowait() for _ in range(2)]
    assert all(outcome[0] == "ok" for outcome in outcomes), outcomes
    assert all(outcome[1] == "wal" for outcome in outcomes), outcomes
    for _, _, tables in outcomes:
        assert {"datasets", "revisions", "tile_cache"} <= set(tables)

    # The shared file is usable afterwards and contains no duplicate objects.
    conn = _open(db_path)
    try:
        rows = conn.execute("SELECT name, COUNT(*) FROM sqlite_master GROUP BY name")
        assert all(count == 1 for _, count in rows)
    finally:
        conn.close()
