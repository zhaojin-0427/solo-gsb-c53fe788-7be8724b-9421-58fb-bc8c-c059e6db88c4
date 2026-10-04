"""FastAPI application: publish immutable GeoJSON revisions and serve MVT tiles.

Routes
------
POST /datasets                                      create dataset (revision 1)
POST /datasets/{dataset_id}/revisions               append revision (409 on conflict)
GET  /datasets                                      list datasets
GET  /datasets/{dataset_id}                         dataset metadata + revisions
GET  /datasets/{dataset_id}/tiles/{z}/{x}/{y}.mvt   vector tile (ETag / 304)
GET  /datasets/{dataset_id}/tile-diff               incremental changed-tile manifest
GET  /healthz                                       liveness
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from shapely.geometry import shape as shapely_shape

from . import db
from . import tilediff
from .config import settings
from .geojson import GeoJSONValidationError, features_to_canonical_json, parse_geojson
from .tiler import compile_tile, tile_etag


@asynccontextmanager
async def lifespan(_application: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="Offline Vector Tile Compiler API",
    version="1.0.0",
    lifespan=lifespan,
    description=(
        "Publishes EPSG:4326 GeoJSON layers as immutable revisions and compiles "
        "deterministic Mapbox Vector Tiles (z=0-14, Web Mercator, 8 px buffer, "
        "0.5 px topology simplification, 4096 grid)."
    ),
)

_LAYER_RE = re.compile(r"^[A-Za-z0-9_-]{1,%d}$" % settings.max_layer_name_len)
_MVT_MEDIA_TYPE = "application/vnd.mapbox-vector-tile"


# ---------------------------------------------------------------------------
# Small bounded LRU caches (per worker process)
# ---------------------------------------------------------------------------


class _LRU:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self._data: "OrderedDict[Any, Any]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any:
        with self._lock:
            if key not in self._data:
                return None
            self._data.move_to_end(key)
            return self._data[key]

    def put(self, key: Any, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.capacity:
                self._data.popitem(last=False)


_tile_lru = _LRU(settings.tile_cache_size)
_features_lru = _LRU(256)
_compile_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------


def _error(status: int, message: str, **extra: Any) -> JSONResponse:
    payload: dict[str, Any] = {"error": message}
    payload.update(extra)
    return JSONResponse(status_code=status, content=payload)


@app.exception_handler(GeoJSONValidationError)
async def _handle_geojson_error(_request: Request, exc: GeoJSONValidationError):
    return _error(400, str(exc))


@app.exception_handler(db.NotFound)
async def _handle_not_found(_request: Request, exc: db.NotFound):
    return _error(404, "dataset not found", dataset_id=str(exc.args[0]))


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def _validate_layer_name(layer: str) -> str:
    layer = (layer or "default").strip() or "default"
    if not _LAYER_RE.match(layer):
        raise GeoJSONValidationError(
            f"invalid layer name {layer!r}: use 1-{settings.max_layer_name_len} "
            "letters, digits, '_' or '-'"
        )
    return layer


async def _read_publish_request(
    request: Request,
) -> tuple[bytes, dict[str, str]]:
    """Read either ``multipart/form-data`` (``file`` + fields) or a raw body."""
    content_length = request.headers.get("content-length")
    try:
        too_large = content_length is not None and int(content_length) > settings.max_upload_bytes
    except ValueError:
        too_large = False
    if too_large:
        raise _PayloadTooLarge(settings.max_upload_bytes)

    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise GeoJSONValidationError(
                "multipart request must contain a 'file' part with the GeoJSON document"
            )
        body = await upload.read(settings.max_upload_bytes + 1)
        fields = {k: str(v) for k, v in form.items() if k != "file"}
    else:
        body = await request.body()
        fields = {k: v for k, v in request.query_params.items()}

    if len(body) > settings.max_upload_bytes:
        raise _PayloadTooLarge(settings.max_upload_bytes)
    return body, fields


class _PayloadTooLarge(Exception):
    def __init__(self, limit: int):
        self.limit = limit


@app.exception_handler(_PayloadTooLarge)
async def _handle_too_large(_request: Request, exc: _PayloadTooLarge):
    return _error(413, f"upload exceeds maximum size of {exc.limit} bytes")


def _publish(body: bytes) -> tuple[bytes, str, int]:
    features = parse_geojson(body)
    canonical = features_to_canonical_json(features)
    content_hash = hashlib.sha256(canonical).hexdigest()
    return canonical, content_hash, len(features)


@app.post("/datasets", status_code=201, tags=["datasets"])
async def create_dataset(request: Request) -> JSONResponse:
    """Upload a GeoJSON FeatureCollection and publish it as revision 1."""
    body, fields = await _read_publish_request(request)
    layer = _validate_layer_name(fields.get("layer", "default"))
    canonical, content_hash, feature_count = _publish(body)

    dataset_id = uuid.uuid4().hex
    with db.connection() as conn:
        try:
            result = db.publish_initial_revision(
                conn,
                dataset_id=dataset_id,
                layer=layer,
                geojson_blob=canonical,
                content_hash=content_hash,
                feature_count=feature_count,
            )
        except db.RevisionConflict:
            # Extremely unlikely with a random UUID4 id; ask the client to retry.
            return _error(409, "generated dataset id collided, please retry")
    return JSONResponse(status_code=201, content=result)


@app.post("/datasets/{dataset_id}/revisions", status_code=201, tags=["datasets"])
async def create_revision(
    dataset_id: str, request: Request
) -> JSONResponse:
    """Append a new immutable revision to an existing dataset.

    Requires the form/query field ``expected_revision`` to equal the current
    head revision; otherwise ``409 Conflict`` is returned.
    """
    body, fields = await _read_publish_request(request)
    raw_expected = fields.get("expected_revision")
    if raw_expected is None:
        return _error(400, "missing required field 'expected_revision'")
    try:
        expected_revision = int(raw_expected)
    except (TypeError, ValueError):
        return _error(400, "'expected_revision' must be an integer")
    if expected_revision < 1:
        return _error(400, "'expected_revision' must reference an existing revision (>=1)")

    layer = fields.get("layer")

    canonical, content_hash, feature_count = _publish(body)

    with db.connection() as conn:
        existing = db.get_dataset(conn, dataset_id)
        if existing is None:
            return _error(404, "dataset not found", dataset_id=dataset_id)
        if layer is not None:
            layer = _validate_layer_name(layer)
            if existing["layer"] != layer:
                return _error(
                    400,
                    f"layer name is fixed to {existing['layer']!r} for this dataset",
                )
        # The MVT layer name is bound to the dataset; omitting it on revision
        # publishes is normal and has no effect.
        try:
            result = db.publish_next_revision(
                conn,
                dataset_id=dataset_id,
                geojson_blob=canonical,
                content_hash=content_hash,
                feature_count=feature_count,
                expected_revision=expected_revision,
            )
        except db.RevisionConflict as exc:
            return _error(
                409,
                "revision conflict: expected_revision does not match current head",
                dataset_id=dataset_id,
                expected_revision=expected_revision,
                current_revision=exc.current_revision,
            )
    return JSONResponse(status_code=201, content=result)


# ---------------------------------------------------------------------------
# Dataset metadata
# ---------------------------------------------------------------------------


@app.get("/datasets", tags=["datasets"])
def list_datasets() -> dict[str, Any]:
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT id, layer, created_at, latest_revision FROM datasets "
            "ORDER BY created_at, id"
        ).fetchall()
    return {"datasets": [dict(r) for r in rows]}


@app.get("/datasets/{dataset_id}", tags=["datasets"])
def get_dataset(dataset_id: str) -> JSONResponse:
    with db.connection() as conn:
        dataset = db.get_dataset(conn, dataset_id)
        if dataset is None:
            return _error(404, "dataset not found", dataset_id=dataset_id)
        revisions = db.list_revisions(conn, dataset_id)
    return JSONResponse(
        content={
            "id": dataset["id"],
            "layer": dataset["layer"],
            "created_at": dataset["created_at"],
            "latest_revision": dataset["latest_revision"],
            "revisions": [dict(r) for r in revisions],
        }
    )


# ---------------------------------------------------------------------------
# Tiles
# ---------------------------------------------------------------------------


def _parse_tile_coord(z_raw: str, x_raw: str, y_raw: str):
    """Validate z/x/y path parameters; raise ValueError with a clear message."""
    y_raw = y_raw[:-4] if y_raw.endswith(".mvt") else y_raw
    try:
        z, x, y = int(z_raw), int(x_raw), int(y_raw)
    except (TypeError, ValueError):
        raise ValueError("z, x and y must be non-negative integers")
    if not (settings.min_zoom <= z <= settings.max_zoom):
        raise ValueError(
            f"zoom z must be between {settings.min_zoom} and {settings.max_zoom}"
        )
    if x < 0 or y < 0:
        raise ValueError("tile x and y must be non-negative")
    n = 1 << z
    if x >= n or y >= n:
        raise ValueError(f"tile coordinates out of range for z={z}: 0 <= x,y < {n}")
    return z, x, y


def _load_revision_features(conn, dataset_id: str, revision: int):
    """Return parsed features for a revision (LRU cached)."""
    cache_key = (dataset_id, revision)
    cached = _features_lru.get(cache_key)
    if cached is not None:
        return cached
    row = db.get_revision(conn, dataset_id, revision)
    if row is None:
        return None
    doc = json.loads(row["geojson"].decode("utf-8"))
    features = [
        {
            "id": f.get("id"),
            "properties": f.get("properties") or {},
            "geometry": shapely_shape(f["geometry"]),
        }
        for f in doc["features"]
    ]
    _features_lru.put(cache_key, features)
    return features


def _if_none_match_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    for raw_tag in header.split(","):
        tag = raw_tag.strip()
        if tag == "*":
            return True
        if tag.startswith("W/"):
            tag = tag[2:].strip()
        if tag == etag:
            return True
    return False


def _get_tile_entry(
    conn,
    dataset_id: str,
    layer_name: str,
    revision: int,
    z: int,
    x: int,
    y: int,
) -> tuple[str, bytes]:
    """Return ``(etag, body)`` for one tile, byte-identical to the tile API.

    Lookup order is the in-process LRU, then the SQLite ``tile_cache`` row
    written by previous requests, then deterministic compilation.  Both the
    tile endpoint and the tile-diff endpoint resolve every tile through this
    single path, so the ``from_etag`` / ``to_etag`` values in a diff manifest
    are guaranteed to equal the ETags returned by the pinned tile endpoint.
    """
    cache_key = (dataset_id, revision, z, x, y)

    # 1) in-process cache
    entry = _tile_lru.get(cache_key)
    if entry is None:
        # 2) SQLite tile cache
        row = db.get_cached_tile(conn, *cache_key)
        if row is not None:
            entry = (row["etag"], bytes(row["body"]))
            _tile_lru.put(cache_key, entry)

    if entry is None:
        # 3) compile deterministically; identical inputs -> identical bytes.
        with _compile_lock:
            entry = _tile_lru.get(cache_key)
            if entry is None:
                features = _load_revision_features(conn, dataset_id, revision)
                body = compile_tile({layer_name: features}, z, x, y)
                etag = tile_etag(body)
                entry = (etag, body)
                db.store_cached_tile(
                    conn, dataset_id, revision, z, x, y, etag, body
                )
                _tile_lru.put(cache_key, entry)
    return entry


@app.api_route(
    "/datasets/{dataset_id}/tiles/{z}/{x}/{y}.mvt",
    methods=["GET", "HEAD"],
    tags=["tiles"],
    name="get_tile",
)
async def get_tile(
    request: Request,
    dataset_id: str,
    z: str,
    x: str,
    y: str,
    revision: int | None = Query(default=None, ge=1),
) -> Response:
    try:
        z_i, x_i, y_i = _parse_tile_coord(z, x, y)
    except ValueError as exc:
        return _error(400, str(exc))

    with db.connection() as conn:
        dataset = db.get_dataset(conn, dataset_id)
        if dataset is None:
            return _error(404, "dataset not found", dataset_id=dataset_id)
        layer_name = dataset["layer"]
        head_revision = dataset["latest_revision"]
        chosen_revision = revision if revision is not None else head_revision
        if db.get_revision(conn, dataset_id, chosen_revision) is None:
            return _error(
                404,
                f"revision {chosen_revision} does not exist",
                dataset_id=dataset_id,
                latest_revision=head_revision,
            )

        etag, body = _get_tile_entry(
            conn, dataset_id, layer_name, chosen_revision, z_i, x_i, y_i
        )

    if _if_none_match_matches(request.headers.get("if-none-match"), etag):
        return Response(
            status_code=304,
            headers={
                "ETag": etag,
                "X-Revision": str(chosen_revision),
                "Cache-Control": "no-cache",
                "Vary": "If-None-Match",
            },
        )

    pinned = revision is not None
    return Response(
        content=body,
        media_type=_MVT_MEDIA_TYPE,
        headers={
            "ETag": etag,
            "X-Revision": str(chosen_revision),
            "Content-Length": str(len(body)),
            "Vary": "If-None-Match",
            "Cache-Control": (
                "public, max-age=31536000, immutable"
                if pinned
                else "public, no-cache"
            ),
        },
    )


# ---------------------------------------------------------------------------
# Incremental tile diff
# ---------------------------------------------------------------------------


def _parse_revision_param(raw: str | None, name: str) -> int:
    """Parse ``from_revision`` / ``to_revision``; raise ValueError for 400s."""
    if raw is None:
        raise ValueError(f"missing required query parameter '{name}'")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"'{name}' must be an integer")
    if value < 1:
        raise ValueError(f"'{name}' must reference an existing revision (>=1)")
    return value


def _parse_diff_zoom(raw: str | None) -> int:
    """Parse the diff zoom parameter (integer within the compiled range)."""
    if raw is None:
        raise ValueError("missing required query parameter 'z'")
    try:
        z = int(raw)
    except (TypeError, ValueError):
        raise ValueError("'z' must be an integer")
    if not (settings.min_zoom <= z <= settings.max_zoom):
        raise ValueError(
            f"zoom z must be between {settings.min_zoom} and {settings.max_zoom}"
        )
    return z


@app.get("/datasets/{dataset_id}/tile-diff", tags=["tiles"])
def get_tile_diff(dataset_id: str, request: Request) -> JSONResponse:
    """List tiles whose encoded MVT bytes change between two revisions.

    Query parameters
    ----------------
    from_revision, to_revision : positive revision numbers (both must exist)
    z                          : zoom level, 0-14

    The candidate range is derived from the mercator bounds of every feature
    of both revisions, expanded by the same 8 px buffer used when clipping,
    so added / removed / moved / attribute-changed features and buffer-only
    neighbours are all covered (ID-less and duplicate-ID features included).
    Each candidate is then resolved through the exact projection / clip /
    simplify / quantise / MVT pipeline and adjudicated by *encoded bytes*:
    tiles that stay byte-identical even when the source GeoJSON differs are
    not reported.  ``from_etag`` / ``to_etag`` are the very ETags returned by
    the pinned tile endpoint.  Results are sorted by ``x`` then ``y``.
    """
    params = request.query_params
    try:
        from_revision = _parse_revision_param(
            params.get("from_revision"), "from_revision"
        )
        to_revision = _parse_revision_param(
            params.get("to_revision"), "to_revision"
        )
        z = _parse_diff_zoom(params.get("z"))
    except ValueError as exc:
        return _error(400, str(exc))

    with db.connection() as conn:
        dataset = db.get_dataset(conn, dataset_id)
        if dataset is None:
            return _error(404, "dataset not found", dataset_id=dataset_id)

        missing = [
            rev
            for rev in (from_revision, to_revision)
            if db.get_revision(conn, dataset_id, rev) is None
        ]
        if missing:
            rev = sorted(set(missing))[0]
            return _error(
                404,
                f"revision {rev} does not exist",
                dataset_id=dataset_id,
                latest_revision=dataset["latest_revision"],
            )

        # Same revision: the immutable blob and deterministic pipeline make
        # the byte diff empty by construction -- skip enumeration entirely.
        if from_revision == to_revision:
            changed: list[dict[str, Any]] = []
        else:
            layer_name = dataset["layer"]
            features_from = _load_revision_features(conn, dataset_id, from_revision)
            features_to = _load_revision_features(conn, dataset_id, to_revision)
            candidates = tilediff.candidate_tiles(features_from, features_to, z)

            changed = []
            for x, y in candidates:
                etag_from, body_from = _get_tile_entry(
                    conn, dataset_id, layer_name, from_revision, z, x, y
                )
                etag_to, body_to = _get_tile_entry(
                    conn, dataset_id, layer_name, to_revision, z, x, y
                )
                # Encoded bytes are the final arbiter: a different source
                # document that compiles to the same tile is no change at all.
                if body_from != body_to:
                    changed.append(
                        {
                            "z": z,
                            "x": x,
                            "y": y,
                            "from_etag": etag_from,
                            "to_etag": etag_to,
                        }
                    )

    return JSONResponse(
        content={
            "dataset_id": dataset_id,
            "z": z,
            "from_revision": from_revision,
            "to_revision": to_revision,
            "changed_tiles": changed,
        }
    )


@app.get("/healthz", tags=["meta"])
def healthz() -> dict[str, str]:
    return {"status": "ok"}
