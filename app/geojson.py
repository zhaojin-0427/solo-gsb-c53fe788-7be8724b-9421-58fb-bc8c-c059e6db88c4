"""GeoJSON ingestion and validation.

Only GeoJSON *FeatureCollection* documents in EPSG:4326 are accepted.  Every
geometry must be a valid, non-empty, in-range GeoJSON geometry parseable by
Shapely; otherwise the whole publish is rejected with HTTP 400.
"""
from __future__ import annotations

import json
from typing import Any

from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.errors import GEOSException

# GeoJSON spec geometry types supported by MVT.
_GEOMETRY_TYPES = {
    "Point",
    "MultiPoint",
    "LineString",
    "MultiLineString",
    "Polygon",
    "MultiPolygon",
}

# Property value types protobuf/MVT can represent (plus None, which is dropped).
_SCALAR_TYPES = (str, bool, int, float)


class GeoJSONValidationError(ValueError):
    """Raised with a human-readable message when uploaded GeoJSON is invalid."""


def _valid_ring_coords(coords: Any, ctx: str) -> None:
    if not isinstance(coords, list) or len(coords) < 4:
        raise GeoJSONValidationError(f"{ctx}: polygon ring must list >= 4 positions")
    for pos in coords:
        _valid_position(pos, ctx)
    if coords[0] != coords[-1]:
        raise GeoJSONValidationError(f"{ctx}: polygon ring must be closed")


def _valid_position(pos: Any, ctx: str) -> None:
    if (
        not isinstance(pos, list)
        or len(pos) < 2
        or not all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in pos[:2])
    ):
        raise GeoJSONValidationError(f"{ctx}: each position must be [lng, lat] numbers")
    lng, lat = pos[0], pos[1]
    if not (-180.0 <= lng <= 180.0) or not (-90.0 <= lat <= 90.0):
        raise GeoJSONValidationError(
            f"{ctx}: coordinate ({lng}, {lat}) is outside EPSG:4326 bounds"
        )


def _validate_coordinates(geom: dict[str, Any]) -> None:
    t = geom["type"]
    coords = geom.get("coordinates")
    ctx = f"geometry '{t}'"
    try:
        if t == "Point":
            _valid_position(coords, ctx)
        elif t == "MultiPoint":
            if not isinstance(coords, list) or not coords:
                raise GeoJSONValidationError(f"{ctx}: coordinates must be a non-empty list")
            for p in coords:
                _valid_position(p, ctx)
        elif t == "LineString":
            if not isinstance(coords, list) or len(coords) < 2:
                raise GeoJSONValidationError(f"{ctx}: line needs >= 2 positions")
            for p in coords:
                _valid_position(p, ctx)
        elif t == "MultiLineString":
            if not isinstance(coords, list) or not coords:
                raise GeoJSONValidationError(f"{ctx}: coordinates must be a non-empty list")
            for line in coords:
                if not isinstance(line, list) or len(line) < 2:
                    raise GeoJSONValidationError(f"{ctx}: line needs >= 2 positions")
                for p in line:
                    _valid_position(p, ctx)
        elif t == "Polygon":
            if not isinstance(coords, list) or not coords:
                raise GeoJSONValidationError(f"{ctx}: polygon must have an exterior ring")
            for ring in coords:
                _valid_ring_coords(ring, ctx)
        elif t == "MultiPolygon":
            if not isinstance(coords, list) or not coords:
                raise GeoJSONValidationError(f"{ctx}: coordinates must be a non-empty list")
            for poly in coords:
                if not isinstance(poly, list) or not poly:
                    raise GeoJSONValidationError(f"{ctx}: polygon must have an exterior ring")
                for ring in poly:
                    _valid_ring_coords(ring, ctx)
    except GeoJSONValidationError:
        raise
    except (TypeError, IndexError):
        raise GeoJSONValidationError(f"{ctx}: malformed coordinates structure")


def _normalise_properties(raw: Any, index: int) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise GeoJSONValidationError(f"feature[{index}]: 'properties' must be an object")
    out: dict[str, Any] = {}
    for key, value in raw.items():
        key = str(key)
        if value is None:
            # MVT cannot encode nulls; drop the attribute.
            continue
        if isinstance(value, _SCALAR_TYPES):
            out[key] = value
        elif isinstance(value, (list, dict)):
            # Keep structured attributes as canonical JSON strings.
            out[key] = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            raise GeoJSONValidationError(
                f"feature[{index}].properties.{key}: unsupported value type {type(value).__name__}"
            )
    return out


def _coerce_feature_id(raw: Any, index: int) -> str | int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise GeoJSONValidationError(f"feature[{index}]: boolean feature id is not allowed")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    if isinstance(raw, str):
        return raw
    raise GeoJSONValidationError(f"feature[{index}]: feature id must be integer or string")


def parse_geojson(payload: bytes) -> list[dict[str, Any]]:
    """Parse and validate an uploaded GeoJSON document.

    Returns a list of normalised feature dicts::

        {"id": int | str | None, "properties": {...}, "geometry": shapely}
    """
    if not payload or not payload.strip():
        raise GeoJSONValidationError("request body is empty")
    try:
        doc = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GeoJSONValidationError(f"invalid JSON document: {exc}") from exc

    if not isinstance(doc, dict):
        raise GeoJSONValidationError("GeoJSON document root must be an object")
    if doc.get("type") != "FeatureCollection":
        raise GeoJSONValidationError(
            "only GeoJSON FeatureCollection documents are accepted"
        )
    raw_features = doc.get("features")
    if not isinstance(raw_features, list):
        raise GeoJSONValidationError("FeatureCollection 'features' must be a list")
    if not raw_features:
        raise GeoJSONValidationError("FeatureCollection contains no features")

    features: list[dict[str, Any]] = []
    for index, feature in enumerate(raw_features):
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise GeoJSONValidationError(f"feature[{index}] is not a GeoJSON Feature")
        geom_json = feature.get("geometry")
        if geom_json is None:
            raise GeoJSONValidationError(f"feature[{index}] has null geometry")
        if not isinstance(geom_json, dict):
            raise GeoJSONValidationError(f"feature[{index}] geometry is invalid")
        gtype = geom_json.get("type")
        if gtype not in _GEOMETRY_TYPES:
            raise GeoJSONValidationError(
                f"feature[{index}]: unsupported geometry type {gtype!r}"
            )
        _validate_coordinates(geom_json)
        try:
            geom: BaseGeometry = shape(geom_json)
        except (GEOSException, ValueError, KeyError) as exc:
            raise GeoJSONValidationError(
                f"feature[{index}]: geometry could not be parsed ({exc})"
            ) from exc
        if geom.is_empty:
            raise GeoJSONValidationError(f"feature[{index}]: geometry is empty")
        if not geom.is_valid:
            from shapely import is_valid_reason

            reason = is_valid_reason(geom)
            raise GeoJSONValidationError(
                f"feature[{index}]: geometry is topologically invalid ({reason})"
            )

        features.append(
            {
                "id": _coerce_feature_id(feature.get("id"), index),
                "properties": _normalise_properties(feature.get("properties"), index),
                "geometry": geom,
            }
        )

    return features


def features_to_canonical_json(features: list[dict[str, Any]]) -> bytes:
    """Serialise validated features into a canonical, deterministic JSON blob."""
    serialisable = [
        {
            "type": "Feature",
            **({"id": f["id"]} if f.get("id") is not None else {}),
            "properties": f["properties"],
            "geometry": _geometry_to_geojson(f["geometry"]),
        }
        for f in features
    ]
    return json.dumps(
        {"type": "FeatureCollection", "features": serialisable},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _geometry_to_geojson(geom: BaseGeometry) -> dict[str, Any]:
    import shapely

    return json.loads(shapely.to_geojson(geom))
