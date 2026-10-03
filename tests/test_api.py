"""End-to-end HTTP API behaviour."""


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def test_publish_creates_revision_one(client, publish):
    meta = publish()
    assert meta["revision"] == 1
    assert len(meta["id"]) == 32
    assert meta["layer"] == "demo"
    assert meta["feature_count"] == 4
    assert len(meta["content_hash"]) == 64
    assert "published_at" in meta


def test_publish_accepts_raw_geojson_body(client, sample_collection):
    resp = client.post(
        "/datasets?layer=raw",
        content=sample_collection,
        headers={"content-type": "application/geo+json"},
    )
    assert resp.status_code == 201
    assert resp.json()["layer"] == "raw"


def test_publish_default_layer_name(client, sample_collection):
    resp = client.post(
        "/datasets",
        content=sample_collection,
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 201
    assert resp.json()["layer"] == "default"


def test_publish_invalid_json_is_400(client):
    resp = client.post("/datasets", content=b"{not-json")
    assert resp.status_code == 400
    assert "invalid JSON" in resp.json()["error"]


def test_publish_empty_body_is_400(client):
    assert client.post("/datasets", content=b"").status_code == 400


def test_publish_requires_feature_collection(client):
    body = b'{"type":"Feature","properties":{},"geometry":null}'
    assert client.post("/datasets", content=body).status_code == 400


def test_publish_rejects_empty_collection(client):
    body = b'{"type":"FeatureCollection","features":[]}'
    assert client.post("/datasets", content=body).status_code == 400


def test_publish_rejects_null_geometry(client):
    body = (
        b'{"type":"FeatureCollection","features":['
        b'{"type":"Feature","properties":{},"geometry":null}]}'
    )
    resp = client.post("/datasets", content=body)
    assert resp.status_code == 400
    assert "null geometry" in resp.json()["error"]


def test_publish_rejects_out_of_range_coordinates(client):
    body = (
        b'{"type":"FeatureCollection","features":['
        b'{"type":"Feature","id":1,"properties":{},"geometry":'
        b'{"type":"Point","coordinates":[200,10]}}]}'
    )
    assert client.post("/datasets", content=body).status_code == 400


def test_publish_rejects_self_intersecting_polygon(client):
    # Bowtie polygon: topologically invalid in OGC / GeoJSON terms.
    body = (
        b'{"type":"FeatureCollection","features":['
        b'{"type":"Feature","id":1,"properties":{},"geometry":'
        b'{"type":"Polygon","coordinates":[[[0,0],[1,1],[1,0],[0,1],[0,0]]]}}]}'
    )
    assert client.post("/datasets", content=body).status_code == 400


def test_publish_rejects_geometry_collection(client):
    body = (
        b'{"type":"FeatureCollection","features":['
        b'{"type":"Feature","id":1,"properties":{},"geometry":'
        b'{"type":"GeometryCollection","geometries":[]}}]}'
    )
    assert client.post("/datasets", content=body).status_code == 400


def test_publish_rejects_bad_layer_name(client, sample_collection):
    resp = client.post(
        "/datasets",
        files={"file": ("d.geojson", sample_collection)},
        data={"layer": "bad layer!"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Revisioning / concurrency
# ---------------------------------------------------------------------------


def _new_revision(client, dataset_id, body, expected):
    return client.post(
        f"/datasets/{dataset_id}/revisions",
        files={"file": ("d.geojson", body, "application/geo+json")},
        data={"expected_revision": str(expected)},
    )


def test_append_revision_advances(client, publish, sample_collection):
    ds = publish()["id"]
    resp = _new_revision(client, ds, sample_collection, 1)
    assert resp.status_code == 201
    assert resp.json()["revision"] == 2


def test_conflict_returns_409(client, publish, sample_collection):
    ds = publish()["id"]
    assert _new_revision(client, ds, sample_collection, 1).status_code == 201
    resp = _new_revision(client, ds, sample_collection, 1)
    assert resp.status_code == 409
    body = resp.json()
    assert body["current_revision"] == 2
    assert body["expected_revision"] == 1
    assert body["dataset_id"] == ds


def test_revision_requires_expected_revision(client, publish, sample_collection):
    ds = publish()["id"]
    resp = client.post(
        f"/datasets/{ds}/revisions",
        files={"file": ("d.geojson", sample_collection)},
    )
    assert resp.status_code == 400


def test_revision_unknown_dataset_404(client, sample_collection):
    resp = _new_revision(client, "does-not-exist", sample_collection, 1)
    assert resp.status_code == 404


def test_revisions_are_immutable_and_listed(client, publish, sample_collection):
    ds = publish()["id"]
    _new_revision(client, ds, sample_collection, 1)
    meta = client.get(f"/datasets/{ds}").json()
    assert meta["latest_revision"] == 2
    assert [r["revision"] for r in meta["revisions"]] == [1, 2]
    # Immutable: re-posting revision 1 still conflicts (head is 2).
    assert _new_revision(client, ds, sample_collection, 1).status_code == 409


# ---------------------------------------------------------------------------
# Listing / metadata / health
# ---------------------------------------------------------------------------


def test_list_and_health(client, publish):
    publish()
    listing = client.get("/datasets").json()
    assert len(listing["datasets"]) == 1
    assert listing["datasets"][0]["latest_revision"] == 1
    assert client.get("/healthz").json() == {"status": "ok"}


def test_get_unknown_dataset_404(client):
    assert client.get("/datasets/nope").status_code == 404
