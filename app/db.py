"""SQLite persistence for datasets, immutable revisions and the tile cache."""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import settings

_logger = logging.getLogger("uvicorn.error")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    id              TEXT PRIMARY KEY,
    layer           TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    latest_revision INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS revisions (
    dataset_id    TEXT NOT NULL,
    revision      INTEGER NOT NULL,
    content_hash  TEXT NOT NULL,
    feature_count INTEGER NOT NULL,
    geojson       BLOB NOT NULL,
    published_at  TEXT NOT NULL,
    PRIMARY KEY (dataset_id, revision),
    FOREIGN KEY (dataset_id) REFERENCES datasets(id)
);

CREATE TABLE IF NOT EXISTS tile_cache (
    dataset_id TEXT NOT NULL,
    revision   INTEGER NOT NULL,
    z          INTEGER NOT NULL,
    x          INTEGER NOT NULL,
    y          INTEGER NOT NULL,
    etag       TEXT NOT NULL,
    body       BLOB NOT NULL,
    PRIMARY KEY (dataset_id, revision, z, x, y)
);

CREATE INDEX IF NOT EXISTS idx_tile_cache_rev
    ON tile_cache (dataset_id, revision);
"""


class RevisionConflict(Exception):
    """Raised when ``expected_revision`` does not match the stored head."""

    def __init__(self, current_revision: int):
        super().__init__(f"expected revision mismatch, current is {current_revision}")
        self.current_revision = current_revision


class NotFound(Exception):
    """Dataset or revision does not exist."""


class SchemaUnavailable(Exception):
    """The schema could not be initialised within the bounded retry budget."""


class InitLockUnavailable(SchemaUnavailable):
    """The non-blocking init file lock is currently held by another process."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_busy_error(exc: sqlite3.Error) -> bool:
    """True for transient SQLITE_BUSY / SQLITE_LOCKED style failures."""
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


def _connect() -> sqlite3.Connection:
    # isolation_level=None puts the driver in autocommit mode so that every
    # multi-statement write is wrapped in an *explicit* transaction.  Publish
    # paths use BEGIN IMMEDIATE (see ``immediate_transaction``), which acquires
    # the SQLite RESERVED lock up front; this serialises the
    # compare-head / insert-revision / update-head sequence across separate
    # OS processes (e.g. multiple uvicorn workers sharing one DB file),
    # whereas a process-local threading lock cannot.
    conn = sqlite3.connect(
        settings.db_path,
        timeout=settings.sqlite_busy_timeout_ms / 1000.0,
        check_same_thread=False,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    # Order matters on a brand-new data volume: install the busy handler
    # *before* switching journal modes.  ``PRAGMA journal_mode=WAL`` briefly
    # needs an exclusive lock; when two worker processes execute it at first
    # touch without a busy timeout active yet, the loser immediately gets
    # "database is locked" and its lifespan startup aborts.  With the timeout
    # in place first, the second worker blocks until the WAL switch finishes.
    conn.execute(f"PRAGMA busy_timeout={int(settings.sqlite_busy_timeout_ms)}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# Tables/index every initialised database must contain.
_SCHEMA_OBJECTS = ("datasets", "revisions", "tile_cache", "idx_tile_cache_rev")


@contextmanager
def _init_file_lock() -> Iterator[None]:
    """Serialise first-touch schema initialisation across worker processes.

    An exclusive ``flock`` on ``<db>.init.lock`` makes the two uvicorn workers
    initialise one after the other instead of racing inside the WAL switch /
    DDL.  The lock is taken **non-blocking** (``LOCK_EX | LOCK_NB``): a worker
    that cannot acquire it immediately raises ``InitLockUnavailable`` and lets
    the caller's bounded retry/backoff loop try again, instead of hanging in
    an uninterruptible blocking ``flock`` while another process keeps the lock
    (which would strand the lifespan and leave the worker half-started).

    The lock is released in the ``finally`` -- including when the guarded DDL
    raises -- and is also released automatically on process exit, so a crashed
    winner can never strand the followers.  Falls back to a no-op on platforms
    without ``fcntl`` (the idempotent DDL + bounded retry still apply there).
    """
    lock_path = settings.db_path + ".init.lock"
    try:
        import fcntl
    except ImportError:
        yield
        return
    with open(lock_path, "a+") as lock_fh:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise InitLockUnavailable(
                f"init lock is held by another process: {lock_path}"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def _assert_db_ready(conn: sqlite3.Connection) -> None:
    """Verify the shared schema (and WAL mode) is actually visible here.

    Runs after the init lock is released, so a worker that waited behind a
    peer proves it sees the committed schema rather than a half-initialised
    database.
    """
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if str(mode).lower() != "wal":
        raise SchemaUnavailable(f"journal_mode is {mode!r}, expected 'wal'")
    existing = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
        )
    }
    missing = [name for name in _SCHEMA_OBJECTS if name not in existing]
    if missing:
        raise SchemaUnavailable(f"schema initialisation incomplete, missing: {missing}")


def init_db() -> None:
    """Create the schema on a fresh volume, safe for concurrent workers.

    All DDL uses ``CREATE ... IF NOT EXISTS`` so the second worker simply
    confirms the first worker's result: no table is ever created twice.  The
    whole sequence is additionally guarded by a cross-process file lock and a
    bounded retry loop.  Both transient ``database is locked`` failures (the
    busy handler budget is exhausted, or an unusually slow peer holds the WAL
    switch) *and* contention on the non-blocking init file lock share the same
    ``init_max_attempts`` budget with capped exponential backoff.  When the
    budget runs out -- e.g. an unrelated process keeps holding
    ``<db>.init.lock`` indefinitely -- ``SchemaUnavailable`` is raised so
    lifespan startup fails fast and the worker exits, rather than blocking
    forever inside ``flock``.
    """
    os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)

    delay = settings.init_retry_delay_s
    last_exc: Exception | None = None
    for attempt in range(1, settings.init_max_attempts + 1):
        try:
            with _init_file_lock():
                conn = _connect()
                try:
                    conn.executescript(_SCHEMA)
                finally:
                    conn.close()

            # Reopen outside the lock and prove the shared state is visible.
            with connection() as verify_conn:
                _assert_db_ready(verify_conn)
            return
        except (sqlite3.OperationalError, SchemaUnavailable) as exc:
            # SchemaUnavailable covers both an incomplete shared schema and
            # InitLockUnavailable (non-blocking file lock contention); only
            # busy-style SQLite errors are retried, other operational errors
            # propagate immediately.
            last_exc = exc
            if not isinstance(exc, SchemaUnavailable) and not _is_busy_error(exc):
                raise
            if attempt >= settings.init_max_attempts:
                break
            _logger.warning(
                "init_db attempt %d/%d failed (%s); retrying in %.2fs",
                attempt,
                settings.init_max_attempts,
                exc,
                delay,
            )
            time.sleep(delay)
            delay = min(delay * 2, 2.0)

    _logger.error(
        "init_db gave up after %d attempts: %s",
        settings.init_max_attempts,
        last_exc,
    )
    raise SchemaUnavailable(
        f"database initialisation failed after {settings.init_max_attempts} attempts: {last_exc}"
    )


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in a cross-process write transaction.

    ``BEGIN IMMEDIATE`` takes the RESERVED lock immediately, so two concurrent
    publishers cannot both read the old head inside overlapping transactions:
    the second one blocks (up to ``busy_timeout``) until the first commits,
    then re-reads the new head and fails the revision check with 409 instead
    of colliding on the revisions primary key.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def get_dataset(conn: sqlite3.Connection, dataset_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM datasets WHERE id = ?", (dataset_id,)
    ).fetchone()


def get_revision(
    conn: sqlite3.Connection, dataset_id: str, revision: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM revisions WHERE dataset_id = ? AND revision = ?",
        (dataset_id, revision),
    ).fetchone()


def publish_initial_revision(
    conn: sqlite3.Connection,
    dataset_id: str,
    layer: str,
    geojson_blob: bytes,
    content_hash: str,
    feature_count: int,
) -> dict[str, Any]:
    """Create a brand-new dataset at revision 1.

    Raises ``RevisionConflict`` (HTTP 409 at the API layer) if a dataset with
    the generated id already exists; the id is random UUID4 so this is only a
    theoretical safety net.
    """
    now = utc_now_iso()
    try:
        with immediate_transaction(conn):
            existing = conn.execute(
                "SELECT 1 FROM datasets WHERE id = ?", (dataset_id,)
            ).fetchone()
            if existing is not None:
                raise RevisionConflict(0)
            conn.execute(
                "INSERT INTO datasets (id, layer, created_at, latest_revision) "
                "VALUES (?, ?, ?, 1)",
                (dataset_id, layer, now),
            )
            conn.execute(
                "INSERT INTO revisions "
                "(dataset_id, revision, content_hash, feature_count, geojson, published_at) "
                "VALUES (?, 1, ?, ?, ?, ?)",
                (dataset_id, content_hash, feature_count, geojson_blob, now),
            )
    except sqlite3.IntegrityError:
        # Safety net: BEGIN IMMEDIATE serialises creators, so a duplicate id can
        # only happen if two callers generated the same random UUID4.
        raise RevisionConflict(0)
    return {
        "id": dataset_id,
        "layer": layer,
        "revision": 1,
        "content_hash": content_hash,
        "feature_count": feature_count,
        "published_at": now,
    }


def publish_next_revision(
    conn: sqlite3.Connection,
    dataset_id: str,
    geojson_blob: bytes,
    content_hash: str,
    feature_count: int,
    expected_revision: int,
) -> dict[str, Any]:
    """Append an immutable revision using optimistic concurrency control.

    The head comparison, the ``revisions`` insert and the ``datasets`` head
    update run in one BEGIN IMMEDIATE transaction, so the compare-and-set is
    atomic across processes sharing this database: exactly one concurrent
    publisher with the same ``expected_revision`` wins; the rest get
    ``RevisionConflict`` with the new head.
    """
    now = utc_now_iso()
    try:
        with immediate_transaction(conn):
            row = conn.execute(
                "SELECT latest_revision, layer FROM datasets WHERE id = ?",
                (dataset_id,),
            ).fetchone()
            if row is None:
                raise NotFound(dataset_id)
            current = row["latest_revision"]
            if current != expected_revision:
                raise RevisionConflict(current)
            next_revision = current + 1
            conn.execute(
                "INSERT INTO revisions "
                "(dataset_id, revision, content_hash, feature_count, geojson, published_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    dataset_id,
                    next_revision,
                    content_hash,
                    feature_count,
                    geojson_blob,
                    now,
                ),
            )
            conn.execute(
                "UPDATE datasets SET latest_revision = ? WHERE id = ?",
                (next_revision, dataset_id),
            )
    except sqlite3.IntegrityError:
        # Defence in depth: with the IMMEDIATE transaction above the head
        # check already guarantees a free (dataset_id, revision) key, so this
        # only triggers if a writer bypassed that path.  Re-read the head and
        # report a normal 409 instead of leaking the database error.
        head = conn.execute(
            "SELECT latest_revision FROM datasets WHERE id = ?", (dataset_id,)
        ).fetchone()
        raise RevisionConflict(head["latest_revision"] if head else 0)
    return {
        "id": dataset_id,
        "layer": row["layer"],
        "revision": next_revision,
        "content_hash": content_hash,
        "feature_count": feature_count,
        "published_at": now,
    }


def list_revisions(conn: sqlite3.Connection, dataset_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT revision, content_hash, feature_count, published_at "
            "FROM revisions WHERE dataset_id = ? ORDER BY revision",
            (dataset_id,),
        )
    )


# ---------------------------------------------------------------------------
# Tile cache
# ---------------------------------------------------------------------------


def get_cached_tile(
    conn: sqlite3.Connection, dataset_id: str, revision: int, z: int, x: int, y: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT etag, body FROM tile_cache WHERE dataset_id=? AND revision=? "
        "AND z=? AND x=? AND y=?",
        (dataset_id, revision, z, x, y),
    ).fetchone()


def store_cached_tile(
    conn: sqlite3.Connection,
    dataset_id: str,
    revision: int,
    z: int,
    x: int,
    y: int,
    etag: str,
    body: bytes,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO tile_cache "
        "(dataset_id, revision, z, x, y, etag, body) VALUES (?,?,?,?,?,?,?)",
        (dataset_id, revision, z, x, y, etag, body),
    )
