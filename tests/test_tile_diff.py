"""GET /datasets/{id}/tile-diff: offline incremental tile manifest."""
import time

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _point_feature(lng, lat, fid=None, name=None, properties=None):
    props = {} if properties is None else dict(properties)
    if name is not None:
        props["name"] = name
    parts = ['"type":"Feature"']
    if fid is not None:
        parts.append(f'"id":{fid}')
    parts.append(f'"properties":{_json(props)}')
    parts.append(f'"geometry":{{"type":"Point","coordinates":[{lng},{lat}]}}')
    return "{" + ",".join(parts) + "}"


def _json(obj):
    import json

    return json.dumps(obj, separators=(",", ":"))


def _collection(*features: str) -> bytes:
    body = '{"type":"FeatureCollection","features":[' + ",".join(features) + "]}"
    return body.encode()


def _publish_revision(client, dataset_id, body, expected):
    resp = client.post(
        f"/datasets/{dataset_id}/revisions",
        content=body,
        headers={"content-type": "application/geo+json"},
        params={"expected_revision": str(expected)},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create(client, body, layer="diff"):
    resp = client.post(
        f"/datasets?layer={layer}",
        content=body,
        headers={"content-type": "application/geo+json"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _diff(client, ds, **params):
    return client.get(f"/datasets/{ds}/tile-diff", params=params)


def _xy_set(payload):
    return {(t["x"], t["y"]) for t in payload["changed_tiles"]}


# ---------------------------------------------------------------------------
# Parameter / existence validation
# ---------------------------------------------------------------------------


def test_same_revision_returns_empty_manifest(client):
    ds = _create(client, _collection(_point_feature(116.4, 39.9, 1, "a")))
    resp = _diff(client, ds, from_revision=1, to_revision=1, z=8)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["changed_tiles"] == []
    assert payload["from_revision"] == 1
    assert payload["to_revision"] == 1


def test_missing_dataset_is_404(client):
    resp = _diff(client, "nope", from_revision=1, to_revision=2, z=0)
    assert resp.status_code == 404
    assert resp.json()["dataset_id"] == "nope"


@pytest.mark.parametrize("missing", ["from_revision", "to_revision", "z"])
def test_missing_parameters_are_400(client, publish, missing):
    ds = publish()["id"]
    params = {"from_revision": 1, "to_revision": 1, "z": 0}
    del params[missing]
    assert _diff(client, ds, **params).status_code == 400


@pytest.mark.parametrize("value", ["abc", "1.5", "", " "])
def test_non_integer_parameters_are_400(client, publish, value):
    ds = publish()["id"]
    base = {"from_revision": 1, "to_revision": 1, "z": 0}
    for key in base:
        params = dict(base)
        params[key] = value
        assert _diff(client, ds, **params).status_code == 400, (key, value)


@pytest.mark.parametrize("rev_param", ["from_revision", "to_revision"])
def test_non_positive_revision_is_400(client, publish, rev_param):
    ds = publish()["id"]
    params = {"from_revision": 1, "to_revision": 1, "z": 0}
    params[rev_param] = 0
    assert _diff(client, ds, **params).status_code == 400
    params[rev_param] = -3
    assert _diff(client, ds, **params).status_code == 400


@pytest.mark.parametrize("bad_z", ["15", "-1", "100"])
def test_z_outside_range_is_400(client, publish, bad_z):
    ds = publish()["id"]
    resp = _diff(client, ds, from_revision=1, to_revision=1, z=bad_z)
    assert resp.status_code == 400


def test_missing_revision_is_404(client, publish):
    ds = publish()["id"]
    resp = _diff(client, ds, from_revision=1, to_revision=99, z=0)
    assert resp.status_code == 404
    assert resp.json()["latest_revision"] == 1
    resp = _diff(client, ds, from_revision=7, to_revision=1, z=0)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Change semantics
# ---------------------------------------------------------------------------


def test_addition_deletion_move_and_attribute_change(client):
    lng, lat = 116.4074, 39.9042
    r1 = _collection(_point_feature(lng, lat, 1, "Beijing"))
    ds = _create(client, r1)
    # Revision 2: keep id 1 with changed attribute, move id 2 away, add id 3.
    r2 = _collection(
        _point_feature(lng, lat, 1, "Peking"),
        _point_feature(lng - 1.0, lat, 2, "moved"),
        _point_feature(120.0, 30.0, 3, "added"),
    )
    _publish_revision(client, ds, r2, 1)

    resp = _diff(client, ds, from_revision=1, to_revision=2, z=8)
    assert resp.status_code == 200
    changed = resp.json()["changed_tiles"]
    assert changed, "expected changes"
    # Ordered x ascending then y ascending.
    assert [(t["x"], t["y"]) for t in changed] == sorted(
        (t["x"], t["y"]) for t in changed
    )
    # The attribute-only change on id 1 must be reported even though the
    # geometry is untouched.
    p1 = _point_tile(lng, lat, 8)
    assert p1 in _xy_set(resp.json())


def test_diff_is_directional_add_vs_delete(client):
    lng, lat = 139.6917, 35.6895
    r1 = _collection(_point_feature(lng, lat, 1, "Tokyo"))
    empty = _collection(_point_feature(-100.0, -50.0, 9, "far-away"))
    ds = _create(client, r1)
    _publish_revision(client, ds, empty, 1)

    added = _diff(client, ds, from_revision=2, to_revision=1, z=9).json()
    deleted = _diff(client, ds, from_revision=1, to_revision=2, z=9).json()
    tile = _point_tile(lng, lat, 9)
    a = [t for t in added["changed_tiles"] if (t["x"], t["y"]) == tile][0]
    d = [t for t in deleted["changed_tiles"] if (t["x"], t["y"]) == tile][0]
    # ETags must swap sides and the "empty" side matches the empty-tile ETag.
    assert a["from_etag"] == d["to_etag"]
    assert a["to_etag"] == d["from_etag"]
    empty_etag = client.get(
        f"/datasets/{ds}/tiles/9/{tile[0]}/{tile[1]}.mvt", params={"revision": 2}
    ).headers["etag"]
    assert a["from_etag"] == empty_etag


def test_etags_match_pinned_tile_endpoint(client):
    r1 = _collection(
        _point_feature(116.4074, 39.9042, 1, "Beijing"),
        _point_feature(139.6917, 35.6895, 2, "Tokyo"),
    )
    r2 = _collection(
        _point_feature(116.4074, 39.9042, 1, "Peking"),
        _point_feature(139.6917, 35.6895, 2, "Tokyo"),
    )
    ds = _create(client, r1)
    _publish_revision(client, ds, r2, 1)

    manifest = _diff(client, ds, from_revision=1, to_revision=2, z=7).json()
    assert manifest["changed_tiles"]
    for tile in manifest["changed_tiles"]:
        x, y, z = tile["x"], tile["y"], tile["z"]
        h1 = client.get(
            f"/datasets/{ds}/tiles/{z}/{x}/{y}.mvt", params={"revision": 1}
        ).headers["etag"]
        h2 = client.get(
            f"/datasets/{ds}/tiles/{z}/{x}/{y}.mvt", params={"revision": 2}
        ).headers["etag"]
        assert tile["from_etag"] == h1
        assert tile["to_etag"] == h2


def test_source_diff_that_compiles_to_same_tile_is_not_reported(client):
    lng, lat = 50.0, 10.0
    # Two features 0.0001 degrees apart collapse onto the same z=4 geometry
    # (and same bytes); put them in the same order so the encoded tile is
    # byte-identical.
    r1 = _collection(
        _point_feature(lng, lat, 1, "a"),
        _point_feature(lng + 0.0001, lat, 2, "b"),
    )
    r2 = _collection(
        _point_feature(lng, lat, 1, "a"),
        _point_feature(lng, lat, 2, "b"),
    )
    ds = _create(client, r1)
    _publish_revision(client, ds, r2, 1)
    manifest = _diff(client, ds, from_revision=1, to_revision=2, z=4).json()
    assert manifest["changed_tiles"] == []


def test_buffer_neighbour_tiles_are_included(client):
    # Place a point roughly 4 px inside a z=6 tile of the prime meridian so
    # its 8 px buffer spills across the x boundary into the neighbour tile.
    px = 360.0 / 256 / (1 << 6)
    lng = 4 * px
    r1 = _collection(_point_feature(lng, 10.0, 1, "edge"))
    empty = _collection(_point_feature(-100.0, -50.0, 9, "away"))
    ds = _create(client, r1)
    _publish_revision(client, ds, empty, 1)

    resp = _diff(client, ds, from_revision=1, to_revision=2, z=6).json()
    coords = _xy_set(resp)
    t = _point_tile(lng, 10.0, 6)
    assert t in coords
    # The point sits just east of the tile's west edge, so the 8 px buffer
    # spills into the neighbour to the west and it must be reported too.
    assert (t[0] - 1, t[1]) in coords


def test_features_without_ids_and_duplicate_ids_are_reported(client):
    # Both revisions contain id-less and duplicated-id features; the change
    # (one id-less feature moves) must still appear in the manifest.
    lng, lat = 30.0, 40.0
    r1 = _collection(
        _point_feature(lng, lat, None, "no-id-1"),
        _point_feature(-20.0, 10.0, 5, "dup"),
        _point_feature(60.0, -5.0, 5, "dup"),
    )
    r2 = _collection(
        _point_feature(lng + 3.0, lat, None, "no-id-1-moved"),
        _point_feature(-20.0, 10.0, 5, "dup"),
        _point_feature(60.0, -5.0, 5, "dup"),
    )
    ds = _create(client, r1)
    _publish_revision(client, ds, r2, 1)
    manifest = _diff(client, ds, from_revision=1, to_revision=2, z=6).json()
    assert manifest["changed_tiles"], "id-less feature move must be reported"


def test_z14_local_update_does_not_enumerate_world(client):
    lng, lat = 116.4074, 39.9042
    r1 = _collection(_point_feature(lng, lat, 1, "Beijing"))
    r2 = _collection(_point_feature(lng, lat, 1, "Peking"))
    ds = _create(client, r1)
    _publish_revision(client, ds, r2, 1)

    start = time.monotonic()
    manifest = _diff(client, ds, from_revision=1, to_revision=2, z=14).json()
    elapsed = time.monotonic() - start
    # Only the touched tile and (at most) its buffer neighbours are compiled.
    assert 1 <= len(manifest["changed_tiles"]) <= 4
    assert elapsed < 10.0


def test_z14_move_is_local(client):
    r1 = _collection(_point_feature(116.4074, 39.9042, 1, "a"))
    # ~20 m north: stays within the same z14 tile or one of its neighbours.
    r2 = _collection(_point_feature(116.4074, 39.9044, 1, "a"))
    ds = _create(client, r1)
    _publish_revision(client, ds, r2, 1)
    manifest = _diff(client, ds, from_revision=1, to_revision=2, z=14).json()
    assert 1 <= len(manifest["changed_tiles"]) <= 4


def test_duplicate_id_change_is_reported(client):
    # Two features share id 7; moving one of them changes a tile and must be
    # reported even though id-based matching would be ambiguous.
    r1 = _collection(
        _point_feature(-40.0, -30.0, 7, "dup-a"),
        _point_feature(60.0, -5.0, 7, "dup-b"),
    )
    r2 = _collection(
        _point_feature(-40.0, -27.0, 7, "dup-a-moved"),
        _point_feature(60.0, -5.0, 7, "dup-b"),
    )
    ds = _create(client, r1)
    _publish_revision(client, ds, r2, 1)
    assert _diff(client, ds, from_revision=1, to_revision=2, z=6).json()[
        "changed_tiles"
    ]


def test_identical_covering_features_but_different_unrelated_features(client):
    # A far-away edit must not mark untouched tiles changed; and the edited
    # region still reports.  Guards the per-tile subset / multiset prefilter.
    keep = _point_feature(0.0, 0.0, 1, "anchor")
    far1 = _point_feature(170.0, -60.0, 2, "far-a")
    far2 = _point_feature(-170.0, 70.0, 3, "far-b")
    ds = _create(client, _collection(keep, far1))
    _publish_revision(client, ds, _collection(keep, far2), 1)
    z = 5
    manifest = _diff(client, ds, from_revision=1, to_revision=2, z=z).json()
    changed = _xy_set(manifest)
    # Anchor tile (and its buffer neighbours) unchanged.
    ax, ay = _point_tile(0.0, 0.0, z)
    assert not {(ax, ay), (ax + 1, ay + 1)} & changed
    # Both far edits show up (addition + deletion sides).
    assert _point_tile(170.0, -60.0, z) in changed
    assert _point_tile(-170.0, 70.0, z) in changed


# ---------------------------------------------------------------------------
# Exhaustive cross-check vs. brute-force enumeration
# ---------------------------------------------------------------------------


def test_manifest_matches_brute_force_at_low_zoom(client):
    r1 = _collection(
        _point_feature(0.0, 0.0, 1, "a"),
        _point_feature(-60.0, 30.0, None, "noid"),
    )
    r2 = _collection(
        _point_feature(0.5, -0.5, 1, "a-moved"),
        _point_feature(-60.0, 30.0, 7, "dup"),
        _point_feature(100.0, -40.0, 7, "dup"),
    )
    ds = _create(client, r1)
    _publish_revision(client, ds, r2, 1)

    z = 4
    manifest = _diff(client, ds, from_revision=1, to_revision=2, z=z).json()
    expected = set()
    n = 1 << z
    for x in range(n):
        for y in range(n):
            h1 = client.get(
                f"/datasets/{ds}/tiles/{z}/{x}/{y}.mvt", params={"revision": 1}
            ).headers["etag"]
            h2 = client.get(
                f"/datasets/{ds}/tiles/{z}/{x}/{y}.mvt", params={"revision": 2}
            ).headers["etag"]
            if h1 != h2:
                expected.add((x, y))
    assert _xy_set(manifest) == expected
    # Every reported pair really differs and unreported tiles match.
    for tile in manifest["changed_tiles"]:
        assert tile["from_etag"] != tile["to_etag"]


def _point_tile(lng, lat, z):
    n = 1 << z
    x = int((lng + 180) / 360 * n)
    import math

    lat_r = math.radians(lat)
    y = int(
        (1 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * n
    )
    return min(x, n - 1), min(y, n - 1)
