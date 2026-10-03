"""Application configuration.

Everything is read from environment variables so the same image can run both
locally and inside Docker Compose without code changes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    db_path: str = os.environ.get("VT_DB_PATH", "/data/tiles.db")
    host: str = os.environ.get("VT_HOST", "0.0.0.0")
    port: int = _int("VT_PORT", 8000)
    # Zoom range that the service is willing to compile.
    min_zoom: int = _int("VT_MIN_ZOOM", 0)
    max_zoom: int = _int("VT_MAX_ZOOM", 14)
    # MVT geometry pipeline constants (see README "瓦片规格").
    tile_extent: int = 4096          # quantisation grid per tile
    tile_size_px: int = 256         # nominal screen size of a tile
    buffer_px: int = 8              # clipping buffer in source pixels
    simplify_px: float = 0.5        # topology-preserving simplification tolerance
    # Reject absurdly large uploads (default 50 MB of JSON).
    max_upload_bytes: int = _int("VT_MAX_UPLOAD_BYTES", 50 * 1024 * 1024)
    max_layer_name_len: int = 32
    # In-process compiled-tile cache (per worker).
    tile_cache_size: int = _int("VT_TILE_CACHE_SIZE", 4096)
    # SQLite connection busy handler: how long (ms) a statement waits on a
    # lock before the driver raises "database is locked".  Must be set before
    # the first ``PRAGMA journal_mode=WAL`` on a fresh volume, because the WAL
    # switch needs an exclusive lock and two workers racing there get
    # SQLITE_BUSY immediately when no busy timeout is active yet.
    sqlite_busy_timeout_ms: int = _int("VT_SQLITE_BUSY_TIMEOUT_MS", 30000)
    # Bounded retry/backoff for startup schema initialisation (see db.init_db).
    init_max_attempts: int = _int("VT_INIT_MAX_ATTEMPTS", 8)
    init_retry_delay_s: float = _float("VT_INIT_RETRY_DELAY_S", 0.1)


settings = Settings()

# The clipping buffer expressed on the 4096-unit local tile grid.
BUFFER_UNITS = settings.buffer_px * settings.tile_extent // settings.tile_size_px
# Simplification tolerance expressed on the same grid.
SIMPLIFY_UNITS = settings.simplify_px * settings.tile_extent / settings.tile_size_px
