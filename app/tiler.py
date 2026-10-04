"""Offline vector-tile compilation pipeline.

Pipeline (applied per tile / per feature):

    EPSG:4326 GeoJSON
      -> project to Web Mercator and map onto the tile-local 4096 grid
      -> clip to the tile plus an 8-pixel buffer
      -> 0.5-pixel tolerance, topology-preserving simplification
      -> quantise to integer 4096 coordinates
      -> deterministic MVT encoding (stable layer / feature order)
"""
from __future__ import annotations

import hashlib
import math
from typing import Any, Iterator

import mapbox_vector_tile
import numpy as np
from shapely import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
    remove_repeated_points,
)
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shp_transform

from .config import BUFFER_UNITS, SIMPLIFY_UNITS, settings
from .projection import ORIGIN_SHIFT, lnglat_to_merc, make_lnglat_to_units_transformer

EXTENT = settings.tile_extent
# Integer coordinate window covered by the buffered clipping box.
MIN_BUF = -BUFFER_UNITS
MAX_BUF = EXTENT + BUFFER_UNITS

# The buffered window as an axis-aligned polygon, oriented exactly as GEOS
# emits an intersection result (screen-clockwise exterior in y-down space).
# A hole-free area feature that fully covers it keeps this exact polygon all
# the way through simplification / quantisation / cleaning, so it is returned
# directly for those interior tiles -- byte-identical, but without rebuilding
# the geometry at every pipeline stage.
CLIP_BOX_GEOM = Polygon(
    [
        (MIN_BUF, MAX_BUF),
        (MAX_BUF, MAX_BUF),
        (MAX_BUF, MIN_BUF),
        (MIN_BUF, MIN_BUF),
        (MIN_BUF, MAX_BUF),
    ]
)


def _geom_dimension(geom: BaseGeometry) -> int:
    """Topological dimension (point=0, line=1, area=2)."""
    if isinstance(geom, (Point, MultiPoint)):
        return 0
    if isinstance(geom, (LineString, MultiLineString)):
        return 1
    return 2  # Polygon / MultiPolygon (collections are exploded upstream)


def _explode(geom: BaseGeometry) -> list[BaseGeometry]:
    """Explode multi geometries / collections into atomic components."""
    if geom.is_empty:
        return []
    if isinstance(geom, (MultiPoint, MultiLineString, MultiPolygon, GeometryCollection)):
        parts: list[BaseGeometry] = []
        for g in geom.geoms:
            parts.extend(_explode(g))
        return parts
    return [geom]


def _keep_dimension(geom: BaseGeometry, dimension: int) -> BaseGeometry | None:
    """After a clip, retain only the components of the expected dimension.

    Intersection may turn a polygon boundary into a line, a line into a point,
    etc.  Such dimension drops are dropped from the feature.
    """
    kept = [
        g
        for g in _explode(geom)
        if not g.is_empty and _geom_dimension(g) == dimension
    ]
    if not kept:
        return None
    if dimension == 0:
        return MultiPoint(kept) if len(kept) > 1 else kept[0]
    if dimension == 1:
        return MultiLineString(kept) if len(kept) > 1 else kept[0]
    return MultiPolygon(kept) if len(kept) > 1 else kept[0]


def _clean_quantized(geom: BaseGeometry) -> BaseGeometry | None:
    """Tidy geometry after rounding so the encoder never sees degenerate rings.

    * collapse repeated consecutive vertices (a side effect of quantisation),
    * drop zero-length lines / points and too-small polygon rings,
    * make polygons valid once more.
    """
    geom = remove_repeated_points(geom, tolerance=0)
    if geom.is_empty:
        return None

    if isinstance(geom, Point):
        return geom
    if isinstance(geom, MultiPoint):
        pts = [p for p in geom.geoms if not p.is_empty]
        return MultiPoint(pts) if len(pts) > 1 else (pts[0] if pts else None)

    if isinstance(geom, LineString):
        coords = list(geom.coords)
        return LineString(coords) if len(set(coords)) >= 2 else None
    if isinstance(geom, MultiLineString):
        lines = [
            LineString(list(line.coords))
            for line in geom.geoms
            if len(set(line.coords)) >= 2
        ]
        if not lines:
            return None
        return MultiLineString(lines) if len(lines) > 1 else lines[0]

    if isinstance(geom, Polygon):
        rings: list[tuple] = []
        exterior = list(geom.exterior.coords)
        if len(exterior) >= 4 and Polygon(exterior).area > 0:
            rings.append(tuple(exterior))
        for interior in geom.interiors:
            ring = list(interior.coords)
            if len(ring) >= 4 and Polygon(ring).area > 0:
                rings.append(tuple(ring))
        if not rings:
            return None
        poly = Polygon(rings[0], rings[1:])
        if not poly.is_valid:
            poly = poly.buffer(0)
            if poly.is_empty:
                return None
            if not isinstance(poly, Polygon):
                poly = _keep_dimension(poly, 2)
        return poly if (poly is not None and not poly.is_empty) else None

    if isinstance(geom, MultiPolygon):
        polys: list[Polygon] = []
        for part in geom.geoms:
            cleaned = _clean_quantized(part)
            if cleaned is not None:
                polys.extend(_explode(cleaned))
        if not polys:
            return None
        return MultiPolygon(polys) if len(polys) > 1 else polys[0]

    # GeometryCollection should never survive this far; defensively explode.
    cleaned = [c for g in _explode(geom) if (c := _clean_quantized(g)) is not None]
    if not cleaned:
        return None
    return (
        MultiPolygon(cleaned)
        if isinstance(cleaned[0], Polygon)
        else (MultiLineString(cleaned) if isinstance(cleaned[0], LineString) else MultiPoint(cleaned))
    )


def _quantize(geom: BaseGeometry) -> BaseGeometry:
    """Round to the integer grid and clamp inside the buffered window."""

    def round_xy(xs, ys, zs=None):
        xi = np.rint(np.asarray(xs, dtype=np.float64)).astype(np.int64)
        yi = np.rint(np.asarray(ys, dtype=np.float64)).astype(np.int64)
        np.clip(xi, MIN_BUF, MAX_BUF, out=xi)
        np.clip(yi, MIN_BUF, MAX_BUF, out=yi)
        return xi.tolist(), yi.tolist()

    return shp_transform(round_xy, geom)


def _filter_non_degenerate(geom: BaseGeometry, dimension: int) -> BaseGeometry | None:
    """Drop zero-length line parts and zero-area polygon parts."""
    kept: list[BaseGeometry] = []
    for g in _explode(geom):
        if g.is_empty:
            continue
        if dimension == 1 and g.length == 0:
            continue
        if dimension == 2 and g.area == 0:
            continue
        kept.append(g)
    if not kept:
        return None
    if dimension == 0:
        return MultiPoint(kept) if len(kept) > 1 else kept[0]
    if dimension == 1:
        return MultiLineString(kept) if len(kept) > 1 else kept[0]
    return MultiPolygon(kept) if len(kept) > 1 else kept[0]


def compile_feature_geometry(
    geom_4326: BaseGeometry, z: int, x: int, y: int
) -> BaseGeometry | None:
    """Run one source geometry through the whole tile pipeline.

    Returns ``None`` when the feature does not survive to this tile.
    """
    transformer = make_lnglat_to_units_transformer(z, x, y, EXTENT)
    geom = shp_transform(transformer, geom_4326)

    # Dimensional family of the *source* geometry.
    dimension = _geom_dimension(geom_4326)

    # 1) Clip to the tile + 8 px (128 units) buffer.
    clip_box = Polygon(
        [
            (MIN_BUF, MIN_BUF),
            (MAX_BUF, MIN_BUF),
            (MAX_BUF, MAX_BUF),
            (MIN_BUF, MAX_BUF),
        ]
    )
    if not geom.intersects(clip_box):
        return None
    # Fast path for a hole-free area feature that fully contains the buffered
    # window (the many *interior* tiles of a large polygon at high zoom): the
    # intersection is exactly the clip box, so we skip the costly polygon
    # clipping.  The box is built in the same orientation ``intersection``
    # emits (screen-clockwise exterior in y-down space), which keeps the
    # encoded MVT bytes identical.  Polygons with holes or multipolygons keep
    # the exact intersection path.
    if (
        dimension == 2
        and geom.geom_type == "Polygon"
        and not geom.interiors
        and geom.covers(clip_box)
    ):
        # The buffered window survives the remaining stages unchanged.
        return CLIP_BOX_GEOM
    geom = geom.intersection(clip_box)

    geom = _keep_dimension(geom, dimension)
    if geom is None or geom.is_empty:
        return None
    geom = _filter_non_degenerate(geom, dimension)
    if geom is None:
        return None

    # 2) Topology-preserving simplification, 0.5 px (8 units) tolerance.
    #    preserve_topology avoids collapsing polygons into invalid forms.
    if dimension > 0:
        geom = geom.simplify(SIMPLIFY_UNITS, preserve_topology=True)
        if geom.is_empty:
            return None
        geom = _keep_dimension(geom, dimension)
        if geom is None or geom.is_empty:
            return None
        geom = _filter_non_degenerate(geom, dimension)
        if geom is None:
            return None

    # 3) Quantise to the integer 4096 grid and clean degeneracies.
    geom = _quantize(geom)
    geom = _clean_quantized(geom)
    return geom


# ---------------------------------------------------------------------------
# Stable feature ordering
# ---------------------------------------------------------------------------

_UINT64_MAX = (1 << 64) - 1


def _sort_key(feature: dict[str, Any]) -> tuple:
    """Deterministic ordering for features inside one layer.

    Positive integer IDs come first (numeric order), then string IDs
    (lexicographic), then features without an ID (by stable content hash).
    """
    fid = feature.get("id")
    if isinstance(fid, bool):  # bool is an int subclass; treat as no numeric id
        fid = None
    if isinstance(fid, int) and 0 <= fid <= _UINT64_MAX:
        return (0, fid, "")
    if isinstance(fid, (int, str)):
        return (1, 0, str(fid))
    digest = hashlib.sha256(
        (
            repr(feature["geometry"])
            + "|"
            + repr(sorted((feature.get("properties") or {}).items()))
        ).encode("utf-8")
    ).hexdigest()
    return (2, 0, digest)


def _normalise_properties(props: dict[str, Any]) -> dict[str, Any]:
    """Return a plain dict with sorted keys (encoding order must be stable)."""
    return {str(k): v for k, v in sorted(props.items(), key=lambda kv: str(kv[0]))}


# ---------------------------------------------------------------------------
# Tile encoding
# ---------------------------------------------------------------------------

from mapbox_vector_tile.encoder import on_invalid_geometry_raise

_ENCODE_OPTIONS = {
    "y_coord_down": True,    # our local Y already points downward
    "extents": EXTENT,
    "quantize_bounds": None,  # coordinates are pre-quantised
    "check_winding_order": True,
    # Never silently swallow invalid geometry: our pipeline validates input
    # and cleans after clipping, so an invalid shape here is a real error.
    "on_invalid_geometry": on_invalid_geometry_raise,
}


def encode_tile(layers: dict[str, list[dict[str, Any]]]) -> bytes:
    """Encode ``{layer_name: [feature, ...]}`` into deterministic MVT bytes."""
    encoded_layers = []
    for layer_name in sorted(layers):
        features = sorted(layers[layer_name], key=_sort_key)
        out_features = []
        for f in features:
            geom = f["geometry"]
            if geom is None or geom.is_empty:
                continue
            fid = f.get("id")
            mvt_feature: dict[str, Any] = {
                "geometry": geom,
                "properties": _normalise_properties(f.get("properties") or {}),
            }
            # MVT feature IDs must be non-negative uint64; anything else is
            # still ordered deterministically, just not emitted as an ID.
            if (
                not isinstance(fid, bool)
                and isinstance(fid, int)
                and 0 <= fid <= _UINT64_MAX
            ):
                mvt_feature["id"] = fid
            out_features.append(mvt_feature)
        encoded_layers.append({"name": layer_name, "features": out_features})

    return mapbox_vector_tile.encode(
        encoded_layers, default_options=_ENCODE_OPTIONS
    )


def compile_tile(
    layers: dict[str, list[dict[str, Any]]], z: int, x: int, y: int
) -> bytes:
    """Compile all layers for one tile and return encoded MVT bytes."""
    compiled: dict[str, list[dict[str, Any]]] = {}
    for layer_name, features in layers.items():
        layer_features = []
        for feature in features:
            geom = compile_feature_geometry(feature["geometry"], z, x, y)
            if geom is None:
                continue
            layer_features.append(
                {
                    "id": feature.get("id"),
                    "properties": feature.get("properties") or {},
                    "geometry": geom,
                }
            )
        # Always keep the layer slot so the layer/feature ordering used to
        # compute byte identity is independent of tile contents.
        compiled[layer_name] = layer_features
    return encode_tile(compiled)


def tile_etag(data: bytes) -> str:
    """Strong validator for the exact MVT byte payload."""
    return '"' + hashlib.sha256(data).hexdigest() + '"'


# ---------------------------------------------------------------------------
# Incremental diff: candidate tile enumeration
# ---------------------------------------------------------------------------


# The clipping buffer measured in *fractions of a tile* at a given zoom.
# BUFFER_UNITS / EXTENT == (8 px * 4096/256) / 4096 == 8/256 == 1/32.
_BUFFER_TILE_FRACTION = BUFFER_UNITS / EXTENT


def _project_to_merc(geom_4326: BaseGeometry) -> BaseGeometry:
    """Project an EPSG:4326 geometry to Web Mercator metres (same path as the
    tile pipeline, including the +/-85.05112878 degree latitude clip)."""

    def project(xs, ys, zs=None):
        mx, my = lnglat_to_merc(np.asarray(xs), np.asarray(ys))
        return mx.tolist(), my.tolist()

    return shp_transform(project, geom_4326)


def candidate_tiles_for_geometry(
    geom_4326: BaseGeometry, z: int
) -> tuple[int, int, int, int]:
    """Return the inclusive ``(xmin, ymin, xmax, ymax)`` XYZ tile range at zoom
    ``z`` whose 8-pixel-buffered clip box can touch ``geom_4326``.

    A feature only contributes geometry to a tile when the tile's buffered
    window (the tile square plus 8 px = 1/32 of a tile on every side)
    intersects the projected feature.  Working in *fractional tile* coordinates
    at zoom ``z`` lets us turn that test into a bounds comparison, so a local
    edit at z=14 enumerates a handful of tiles instead of all 2^28.

    The range is an over-approximation: the final decision is always made on
    the actual encoded MVT bytes (see ``tile-diff``), so candidates merely have
    to be complete.  The Mercator Y mapping is identical to
    :func:`projection.make_lnglat_to_units_transformer`, and vertices sitting
    exactly on a buffered boundary are included.
    """
    merc = _project_to_merc(geom_4326)
    minx, miny, maxx, maxy = merc.bounds

    n = 1 << z
    world = 2.0 * ORIGIN_SHIFT
    size = world / n
    b = _BUFFER_TILE_FRACTION
    eps = 1e-9

    # fx: fractional x column, grows eastward (fx = x + local_u/4096).
    fx0 = (minx + ORIGIN_SHIFT) / size
    fx1 = (maxx + ORIGIN_SHIFT) / size
    # fy: fractional y row in *screen* space, grows southward.
    fy0 = (ORIGIN_SHIFT - maxy) / size
    fy1 = (ORIGIN_SHIFT - miny) / size

    xmin = int(math.floor(fx0 - b + eps))
    xmax = int(math.floor(fx1 + b - eps))
    ymin = int(math.floor(fy0 - b + eps))
    ymax = int(math.floor(fy1 + b - eps))

    xmin = max(0, xmin)
    ymin = max(0, ymin)
    xmax = min(n - 1, xmax)
    ymax = min(n - 1, ymax)
    return xmin, ymin, xmax, ymax


def build_tile_feature_index(
    features: list[dict[str, Any]], z: int
) -> dict[tuple[int, int], list[dict[str, Any]]]:
    """Map every candidate tile at zoom ``z`` to the features that can reach it.

    A feature is listed for a tile only when its projected bounds (with the
    8 px buffer included) intersect that tile.  Membership is decided by
    geometry alone, so features without an ID and features sharing an ID are
    treated no differently.  The diff compiler feeds these per-tile subsets
    through the *same* per-feature pipeline used by the tile endpoint, so it
    never runs (or reprojects) far-away features for a tile; features absent
    from a tile's list compile exactly to the same nothing the full pipeline
    would have culled.
    """
    index: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for feature in features:
        geom = feature["geometry"]
        if geom is None or geom.is_empty:
            continue
        xmin, ymin, xmax, ymax = candidate_tiles_for_geometry(geom, z)
        for x in range(xmin, xmax + 1):
            for y in range(ymin, ymax + 1):
                index.setdefault((x, y), []).append(feature)
    return index


def candidate_tiles_for_features(
    features: list[dict[str, Any]], z: int
) -> Iterator[tuple[int, int]]:
    """Yield every candidate ``(x, y)`` tile at zoom ``z`` for the features."""
    yield from build_tile_feature_index(features, z)


def feature_content_key(feature: dict[str, Any]) -> str:
    """Stable content key for one feature: id + sorted properties + geometry.

    Two features with the same key are indistinguishable to the tile encoder
    (the encoder sorts by id / content and normalises property keys), so a
    tile whose covering features have the same multiset of keys on both
    revisions provably compiles to the same bytes without being recompiled.
    Geometry is hashed by WKB, which is canonical for a fixed Shapely/GEOS.
    """
    fid = feature.get("id")
    props = _normalise_properties(feature.get("properties") or {})
    geom = feature["geometry"]
    geom_repr = geom.wkb_hex if geom is not None else "none"
    payload = repr(fid) + "|" + repr(sorted(props.items())) + "|" + geom_repr
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_tile_diff_index(
    keyed_features: list[tuple[str, dict[str, Any]]], z: int
) -> tuple[
    dict[tuple[int, int], list[dict[str, Any]]],
    dict[tuple[int, int], dict[str, int]],
]:
    """Build per-tile feature subsets and feature-key multisets for a diff.

    ``keyed_features`` pairs each feature with its (revision-local, precomputed)
    :func:`feature_content_key`.  Returns:

    * ``index``  -- candidate tile -> features whose buffered bounds touch it;
    * ``counts`` -- candidate tile -> multiset (Counter) of feature keys.

    The same tile having equal key multisets in both revisions means identical
    covering features and therefore identical deterministic tile bytes.
    """
    index: dict[tuple[int, int], list[dict[str, Any]]] = {}
    counts: dict[tuple[int, int], dict[str, int]] = {}
    for key, feature in keyed_features:
        geom = feature["geometry"]
        if geom is None or geom.is_empty:
            continue
        xmin, ymin, xmax, ymax = candidate_tiles_for_geometry(geom, z)
        for x in range(xmin, xmax + 1):
            for y in range(ymin, ymax + 1):
                tile = (x, y)
                index.setdefault(tile, []).append(feature)
                counter = counts.setdefault(tile, {})
                counter[key] = counter.get(key, 0) + 1
    return index, counts
