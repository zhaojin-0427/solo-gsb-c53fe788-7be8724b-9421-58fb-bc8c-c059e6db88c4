"""Web Mercator (EPSG:3857) projection and tile-math helpers.

Input geometries are always EPSG:4326 (longitude/latitude).  The vector tile
pipeline works in *tile local units*: a tile is 4096 x 4096 units, origin at
the upper-left corner, X to the right and Y downward (MVT screen convention).
"""
from __future__ import annotations

import math

import numpy as np

# Spherical Mercator parameters (EPSG:3857 / Google Maps convention).
EARTH_RADIUS = 6378137.0
ORIGIN_SHIFT = math.pi * EARTH_RADIUS          # 20037508.342789244
MERC_MAX_LAT = 85.0511287798


def lnglat_to_merc(lng: np.ndarray, lat: np.ndarray):
    """Project longitude/latitude arrays to Web Mercator metres."""
    lat = np.clip(lat, -MERC_MAX_LAT, MERC_MAX_LAT)
    mx = lng * ORIGIN_SHIFT / 180.0
    # R * ln(tan(pi/4 + phi/2)); phi must be in radians.
    phi = np.radians(lat)
    my = EARTH_RADIUS * np.log(np.tan(math.pi / 4.0 + phi / 2.0))
    return mx, my


def tile_bounds_m(z: int, x: int, y: int):
    """Return ``(minx, miny, maxx, maxy)`` metres for an XYZ tile.

    XYZ numbering: ``x`` grows eastward, ``y`` grows southward (y=0 on top).
    """
    n = 1 << z
    span = 2.0 * ORIGIN_SHIFT / n
    minx = -ORIGIN_SHIFT + x * span
    maxx = minx + span
    maxy = ORIGIN_SHIFT - y * span
    miny = maxy - span
    return minx, miny, maxx, maxy


def make_lnglat_to_units_transformer(z: int, x: int, y: int, extent: int = 4096):
    """Build a vectorised Shapely transform function lng/lat -> tile units.

    Output coordinates follow the MVT screen convention: origin at the
    tile's upper-left corner, X eastward, Y southward.
    """
    minx, miny, maxx, maxy = tile_bounds_m(z, x, y)
    span = maxx - minx
    scale = extent / span

    def transform(xs, ys, zs=None):
        xs = np.asarray(xs, dtype=np.float64)
        ys = np.asarray(ys, dtype=np.float64)
        mx, my = lnglat_to_merc(xs, ys)
        u = (mx - minx) * scale
        # Screen Y grows downward: mercator Y is largest at the north edge.
        v = (maxy - my) * scale
        return u, v

    return transform
