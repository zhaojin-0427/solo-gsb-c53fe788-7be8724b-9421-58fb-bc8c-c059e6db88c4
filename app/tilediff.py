"""Candidate tile enumeration for incremental tile diffs.

Given two immutable revisions of an EPSG:4326 GeoJSON layer and a zoom ``z``,
``candidate_tiles`` returns the *small* set of XYZ tile coordinates at that
zoom whose encoded MVT bytes could possibly differ between the revisions.

A feature is emitted into a tile only when its geometry intersects the tile
box expanded by the 8 px clipping buffer (``BUFFER_UNITS = 128`` of the 4096
tile units -- see :func:`app.tiler.compile_feature_geometry`).  Therefore one
feature can only affect tiles whose own (un-buffered) bounds cover its
geometry expanded by the very same 8 px.  For every feature of *both*
revisions we:

1. project it to spherical Web Mercator with the exact projection / latitude
   clamp used by the tile pipeline (:mod:`app.projection`),
2. expand its mercator bounding box by 8 px (128/4096 of one tile span),
3. map the box to the inclusive tile x/y index range at zoom ``z``,
4. union every range.

Coverage is purely geometry based, so features without an ID or with
duplicate IDs need no special handling and can never be missed.  A localised
z=14 update enumerates only a handful of neighbouring tiles -- never the
whole ``2**14`` grid (which would be 268 million tiles).
"""
from __future__ import annotations

import math
from typing import Iterable

import numpy as np
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shp_transform

from .config import BUFFER_UNITS
from .projection import ORIGIN_SHIFT, lnglat_to_merc
from .tiler import EXTENT

# Fraction of one tile span covered by the 8 px buffer (128 / 4096 == 1/32).
_BUFFER_FRACTION = BUFFER_UNITS / EXTENT
# Floating point slack when a buffered edge lands (almost) exactly on a tile
# boundary.  Candidate sets must be supersets, so erring towards the extra
# tile is always safe; a real margin of 1/32 of a tile span dwarfs the slack.
_EPS = 1e-9


def _to_mercator(geom: BaseGeometry) -> BaseGeometry:
    """Reproject one 4326 geometry to Web Mercator metres (tile projection)."""

    def project(xs, ys, zs=None):
        return lnglat_to_merc(np.asarray(xs, dtype=np.float64),
                              np.asarray(ys, dtype=np.float64))

    return shp_transform(project, geom)


def _tile_range_for_geometry(
    geom_4326: BaseGeometry, z: int
) -> tuple[int, int, int, int]:
    """Return the inclusive ``(xmin, xmax, ymin, ymax)`` tile range at ``z``.

    The geometry's mercator bounding box is grown by the 8 px buffer on every
    side, so every tile the compiled feature could be clipped into is covered.
    """
    minx, miny, maxx, maxy = _to_mercator(geom_4326).bounds

    n = 1 << z
    span = 2.0 * ORIGIN_SHIFT / n
    # 8 px buffer plus a microscopic slack (1e-9 of a tile span) so a buffered
    # edge that mathematically crosses a tile boundary can never be rounded
    # back inside it by floating point error.
    grow = (_BUFFER_FRACTION + _EPS) * span
    minx, miny = minx - grow, miny - grow
    maxx, maxy = maxx + grow, maxy + grow

    # Keep the box inside the world (latitude is clamped to the mercator
    # limit by the projection itself; the buffer could only overshoot here).
    minx = max(minx, -ORIGIN_SHIFT)
    maxx = min(maxx, ORIGIN_SHIFT)
    miny = max(miny, -ORIGIN_SHIFT)
    maxy = min(maxy, ORIGIN_SHIFT)

    def tile_x(mx: float) -> int:
        return int(math.floor((mx + ORIGIN_SHIFT) / span))

    def tile_y(my: float) -> int:
        # XYZ: y grows southward (mercator Y is largest at the north edge).
        return int(math.floor((ORIGIN_SHIFT - my) / span))

    xmin = max(0, tile_x(minx))
    xmax = min(n - 1, tile_x(maxx))
    ymin = max(0, tile_y(maxy))
    ymax = min(n - 1, tile_y(miny))

    return xmin, xmax, ymin, ymax


def candidate_tiles(
    features_a: Iterable[dict], features_b: Iterable[dict], z: int
) -> list[tuple[int, int]]:
    """Union of possibly-affected ``(x, y)`` tiles at zoom ``z``.

    Considers every feature geometry of both revisions (including features
    without IDs or with duplicated IDs).  The result is sorted by ``x`` then
    ``y`` and contains no duplicates.
    """
    coords: set[tuple[int, int]] = set()
    for features in (features_a, features_b):
        for feature in features:
            geom = feature["geometry"]
            if geom is None or geom.is_empty:
                continue
            xmin, xmax, ymin, ymax = _tile_range_for_geometry(geom, z)
            for x in range(xmin, xmax + 1):
                for y in range(ymin, ymax + 1):
                    coords.add((x, y))
    return sorted(coords)
