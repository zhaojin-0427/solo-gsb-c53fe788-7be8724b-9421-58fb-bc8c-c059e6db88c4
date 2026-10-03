"""Pytest fixtures: isolated app with a fresh SQLite database per test."""
import os
import tempfile

import pytest

_tmp = tempfile.NamedTemporaryFile(prefix="vt-tests-", suffix=".db", delete=False)
_tmp.close()
os.environ["VT_DB_PATH"] = _tmp.name

from starlette.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _prepare_db():
    db.init_db()


@pytest.fixture(scope="session")
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def reset_db():
    """Wipe all rows before each test so tests are independent."""
    with db.connection() as conn:
        conn.execute("DELETE FROM tile_cache")
        conn.execute("DELETE FROM revisions")
        conn.execute("DELETE FROM datasets")
        conn.commit()
    yield


@pytest.fixture
def sample_collection() -> bytes:
    path = os.path.join(os.path.dirname(__file__), "..", "data", "sample.geojson")
    with open(path, "rb") as fh:
        return fh.read()


@pytest.fixture
def publish(client, sample_collection):
    """Factory publishing a GeoJSON document and returning its dataset id."""

    def _publish(payload: bytes = sample_collection, layer: str = "demo"):
        resp = client.post(
            "/datasets",
            files={"file": ("data.geojson", payload, "application/geo+json")},
            data={"layer": layer},
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    return _publish
