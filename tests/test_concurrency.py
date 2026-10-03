"""Concurrency regression tests for append-publish compare-and-set.

Two independent workers (threads with their own database connections, and
separate OS processes) that share one SQLite file must never both create the
same next revision: exactly one request gets 201, every other request gets the
existing 409 envelope with ``current_revision`` pointing at the new head, and
no extra revision or half-committed row may remain.
"""
import multiprocessing
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from starlette.testclient import TestClient

from app.main import app


def _append_revision(dataset_id: str, sample_collection: bytes, barrier) -> tuple[int, dict]:
    # A dedicated client per thread gives each request its own event-loop
    # thread and therefore its own SQLite connection, mirroring two workers.
    client = TestClient(app)
    barrier.wait()
    resp = client.post(
        f"/datasets/{dataset_id}/revisions",
        files={"file": ("d.geojson", sample_collection, "application/geo+json")},
        data={"expected_revision": "1"},
    )
    return resp.status_code, resp.json()


def test_concurrent_appends_single_winner(client, publish, sample_collection):
    ds = publish()["id"]
    n = 6
    barrier = threading.Barrier(n)

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(
            pool.map(
                lambda _: _append_revision(ds, sample_collection, barrier),
                range(n),
            )
        )

    statuses = sorted(code for code, _ in results)
    assert statuses.count(201) == 1, results
    assert statuses.count(409) == n - 1, results

    winner = next(body for code, body in results if code == 201)
    assert winner["revision"] == 2
    for code, body in results:
        if code == 409:
            assert body == {
                "error": "revision conflict: expected_revision does not match current head",
                "dataset_id": ds,
                "expected_revision": 1,
                "current_revision": 2,
            }

    meta = client.get(f"/datasets/{ds}").json()
    assert meta["latest_revision"] == 2
    assert [r["revision"] for r in meta["revisions"]] == [1, 2]

    # Historical revision stays readable; head and pinned tiles still resolve.
    old = client.get(f"/datasets/{ds}/tiles/0/0/0.mvt?revision=1")
    assert old.status_code == 200
    assert old.headers["x-revision"] == "1"
    head_tile = client.get(f"/datasets/{ds}/tiles/0/0/0.mvt")
    assert head_tile.status_code == 200
    assert head_tile.headers["x-revision"] == "2"


def _cross_process_child(dataset_id, barrier, queue):
    # Imported after fork: a brand-new connection to the shared database file.
    from app import db

    barrier.wait()
    try:
        with db.connection() as conn:
            result = db.publish_next_revision(
                conn,
                dataset_id=dataset_id,
                geojson_blob=b"{}",
                content_hash="h",
                feature_count=1,
                expected_revision=1,
            )
        queue.put(("ok", result["revision"]))
    except db.RevisionConflict as exc:
        queue.put(("conflict", exc.current_revision))
    except Exception as exc:  # pragma: no cover - surfaces the raw failure
        queue.put(("error", f"{type(exc).__name__}: {exc}"))


@pytest.mark.skipif(
    not hasattr(multiprocessing, "get_context")
    or "fork" not in multiprocessing.get_all_start_methods(),
    reason="cross-process test requires the 'fork' start method",
)
def test_cross_process_concurrent_append(client, publish):
    ds = publish()["id"]
    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(2)
    queue = ctx.Queue()
    procs = [
        ctx.Process(target=_cross_process_child, args=(ds, barrier, queue))
        for _ in range(2)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=60)
        assert proc.exitcode == 0, f"child exited with {proc.exitcode}"

    outcomes = sorted(queue.get_nowait() for _ in range(2))
    assert outcomes == [("conflict", 2), ("ok", 2)], outcomes

    meta = client.get(f"/datasets/{ds}").json()
    assert meta["latest_revision"] == 2
    assert [r["revision"] for r in meta["revisions"]] == [1, 2]
