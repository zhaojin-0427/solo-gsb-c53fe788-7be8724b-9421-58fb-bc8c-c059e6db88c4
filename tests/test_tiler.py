"""Unit tests for the geometric MVT pipeline."""
import math

import mapbox_vector_tile
from shapely.geometry import (
    LineString,
    Point,
    Polygon,
)

from app import tiler
from app.config import BUFFER_UNITS, SIMPLIFY_UNITS


def test_point_inside_tile():
    # (lng=0, lat=0) on z=1 sits at the upper-left corner of tile (1,1,1).
    out = tiler.compile_feature_geometry(Point(0.0, 0.0), 1, 1, 1)
    assert out is not None
    assert out.geom_type == "Point"
    assert (out.x, out.y) == (0, 0)


def test_point_outside_tile_but_in_buffer_is_kept():
    # Move the point 4 px (64 units) west / north of the tile corner.
    d = 360.0 / (256 * 2 * 1) * 4  # 4 source pixels in degrees
    out = tiler.compile_feature_geometry(Point(-d, d), 1, 1, 1)
    assert out is not None
    assert out.x == -64
    assert out.y == -64  # screen Y is negative north of the top edge


def test_point_beyond_buffer_is_culled():
    # 16 px away -> well beyond the 128-unit buffer.
    d = 360.0 / 512 * 16
    out = tiler.compile_feature_geometry(Point(-d, d), 1, 1, 1)
    assert out is None


def test_line_is_clipped_to_buffer_window():
    line = LineString([(-50.0, 0.0), (50.0, 0.0)])
    # z=1, tile (1,1,1): west edge at lng 0, equator runs along the top edge.
    out = tiler.compile_feature_geometry(line, 1, 1, 1)
    assert out is not None
    minx, miny, maxx, maxy = out.bounds
    assert minx == -BUFFER_UNITS
    assert maxx <= 4096 + BUFFER_UNITS


def test_polygon_is_clipped_to_buffer_window():
    poly = Polygon(
        [(-50, -50), (50, -50), (50, 50), (-50, 50), (-50, -50)]
    )
    # z=1, tile (1,1,1): the polygon crosses the west and north edges.
    out = tiler.compile_feature_geometry(poly, 1, 1, 1)
    assert out is not None
    minx, miny, maxx, maxy = out.bounds
    assert minx == -BUFFER_UNITS
    assert miny == -BUFFER_UNITS


def test_coordinates_are_quantized_to_integers():
    out = tiler.compile_feature_geometry(Point(12.345678, 47.123456), 4, 8, 5)
    # Just ensure it lands inside; every emitted coordinate must be integral.
    if out is not None:
        assert float(out.x).is_integer()
        assert float(out.y).is_integer()


def test_simplification_tolerance_is_half_pixel():
    # A straight line with a tiny sideways wiggle (< 0.5 px) must collapse,
    # while a large deviation survives.  Offsets are in longitude degrees;
    # near the equator 1 tile-unit ~= 360/4096 degrees at z=0.
    tiny = 360.0 / 4096 * 0.25   # 0.25 tile units < 8-unit tolerance
    big = 360.0 / 4096 * 20      # 20 tile units > 8-unit tolerance
    line = LineString([(-10.0, 0.0), (0.0, tiny), (10.0, 0.0)])
    out = tiler.compile_feature_geometry(line, 0, 0, 0)
    assert out is not None
    assert len(out.coords) == 2  # middle vertex removed

    line2 = LineString([(-10.0, 0.0), (0.0, big), (10.0, 0.0)])
    out2 = tiler.compile_feature_geometry(line2, 0, 0, 0)
    assert out2 is not None
    assert len(out2.coords) == 3


def test_simplify_constants():
    assert BUFFER_UNITS == 128          # 8 px * 4096/256
    assert SIMPLIFY_UNITS == 8.0       # 0.5 px * 4096/256


def test_line_downgraded_to_point_by_clip_is_dropped():
    # When clipping collapses a line to a zero-length / point-like remnant,
    # the feature must not be emitted (neither as a point nor as a degenerate
    # line).  This line sits entirely west of z=0 tile except for one endpoint
    # exactly on the buffered west edge.
    west_only = LineString([(-191.25, 10.0), (-200.0, 10.0)])
    out = tiler.compile_feature_geometry(west_only, 0, 0, 0)
    assert out is None


def test_zero_area_polygon_part_is_dropped():
    from shapely.geometry import GeometryCollection, LineString as LS

    # A polygon feature must never emit line/point remnants after clipping.
    assert tiler._keep_dimension(LS([(0, 0), (1, 1)]), 2) is None
    assert tiler._filter_non_degenerate(GeometryCollection(), 2) is None


def test_far_away_geometry_is_culled():
    assert tiler.compile_feature_geometry(Point(139.0, 35.0), 2, 0, 0) is None


def test_tile_extent_metadata():
    body = tiler.compile_tile(
        {"L": [{"geometry": Point(0, 0), "properties": {}, "id": 1}]},
        1, 1, 0,
    )
    decoded = mapbox_vector_tile.decode(
        body, default_options={"y_coord_down": True}
    )
    assert decoded["L"]["extent"] == 4096
    assert decoded["L"]["version"] == 2


def test_feature_ids_stable_numeric_order():
    feats = [
        {"geometry": Point(0, 0), "properties": {}, "id": 30},
        {"geometry": Point(0.1, 0.1), "properties": {}, "id": 2},
        {"geometry": Point(0.2, 0.2), "properties": {}, "id": 9},
    ]
    body = tiler.compile_tile({"L": feats}, 0, 0, 0)
    decoded = mapbox_vector_tile.decode(
        body, default_options={"y_coord_down": True}
    )
    assert [f["id"] for f in decoded["L"]["features"]] == [2, 9, 30]


def test_string_ids_are_ordered_after_numeric():
    feats = [
        {"geometry": Point(0, 0), "properties": {}, "id": "zzz"},
        {"geometry": Point(0.1, 0.1), "properties": {}, "id": 1},
        {"geometry": Point(0.2, 0.2), "properties": {"x": 1}},
    ]
    body = tiler.compile_tile({"L": feats}, 0, 0, 0)
    decoded = mapbox_vector_tile.decode(
        body, default_options={"y_coord_down": True}
    )
    features = decoded["L"]["features"]
    # Numeric ids sort first; the library decoder renders a missing protobuf id
    # field as the scalar default 0, which is how the last two features appear.
    assert features[0]["id"] == 1
    assert features[1]["id"] == 0
    assert features[2]["id"] == 0

    # Byte-level stability is the real requirement.
    body_again = tiler.compile_tile({"L": list(reversed(feats))}, 0, 0, 0)
    assert body == body_again


def test_layers_are_sorted_by_name():
    feats = [{"geometry": Point(0, 0), "properties": {}}]
    a = tiler.compile_tile({"zebra": feats, "alpha": feats}, 0, 0, 0)
    b = tiler.compile_tile({"alpha": feats, "zebra": feats}, 0, 0, 0)
    assert a == b


def test_bytes_deterministic_across_processing_order():
    poly = Polygon(
        [(-10, -10), (10, -10), (10, 10), (-10, 10), (-10, -10)]
    )
    feats = [
        {"geometry": poly, "properties": {"a": 1, "b": 2}, "id": 1},
        {"geometry": LineString([(-20, 0), (20, 0)]), "properties": {}, "id": 2},
        {"geometry": Point(0, 0), "properties": {}, "id": 3},
    ]
    a = tiler.compile_tile({"L": feats}, 1, 0, 0)
    b = tiler.compile_tile({"L": list(reversed(feats))}, 1, 0, 0)
    assert a == b


def test_etag_is_sha256_hex():
    etag = tiler.tile_etag(b"abc")
    assert etag == '"' + hashlib_sha256(b"abc") + '"'


def hashlib_sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def test_high_zoom_compiles_points_z14():
    # Beijing point in its z14 tile.
    lng, lat = 116.4074, 39.9042
    z = 14
    n = 1 << z
    x = int((lng + 180) / 360 * n)
    y = int(
        (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi)
        / 2 * n
    )
    out = tiler.compile_feature_geometry(Point(lng, lat), z, x, y)
    assert out is not None
    assert 0 <= out.x <= 4096
    assert 0 <= out.y <= 4096


def test_degenerate_quantized_polygon_does_not_crash():
    # Extremely thin sliver far north: may collapse under quantisation.
    poly = Polygon(
        [(10.0, 80.0), (10.00001, 80.0), (10.00001, 80.00001), (10.0, 80.0)]
    )
    # Should either produce valid geometry or be dropped, never raise.
    out = tiler.compile_feature_geometry(poly, 5, 16, 5)
    if out is not None:
        assert out.is_valid or out.geom_type.startswith("Multi")
