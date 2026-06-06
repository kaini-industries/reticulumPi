"""Background tile prefetch — pre-seed the disk cache around the node position."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import tempfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reticulumpi.builtin_plugins.web_dashboard.plugin import WebDashboardPlugin

log = logging.getLogger(__name__)

MAX_PREFETCH_TILES = 10_000


def _lat_lon_to_tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    """Convert lat/lon to slippy-map tile coordinates."""
    lat = max(-85.0511, min(85.0511, lat))
    n = 2**zoom
    x = int((lon + 180.0) / 360.0 * n) % n
    lat_rad = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def _grid_radius(zoom: int) -> int:
    """Grid half-size for a given zoom level (tiles from center)."""
    if zoom <= 10:
        return 1  # 3x3
    if zoom <= 13:
        return 2  # 5x5
    return 3  # 7x7


def _tile_list(lat: float, lon: float, min_zoom: int, max_zoom: int) -> list[tuple[int, int, int]]:
    """Return [(z, x, y), ...] for the prefetch grid around a position."""
    tiles = []
    for z in range(min_zoom, max_zoom + 1):
        cx, cy = _lat_lon_to_tile(lat, lon, z)
        r = _grid_radius(z)
        n = 2**z
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                tx = (cx + dx) % n
                ty = cy + dy
                if 0 <= ty < n:
                    tiles.append((z, tx, ty))
    return tiles


def _tile_list_bbox(
    south: float,
    west: float,
    north: float,
    east: float,
    min_zoom: int,
    max_zoom: int,
) -> list[tuple[int, int, int]]:
    """Return [(z, x, y), ...] covering a bounding box at each zoom level."""
    tiles = []
    for z in range(min_zoom, max_zoom + 1):
        x_min, y_min = _lat_lon_to_tile(north, west, z)
        x_max, y_max = _lat_lon_to_tile(south, east, z)
        n = 2**z
        for x in range(x_min, x_max + 1):
            for y in range(y_min, y_max + 1):
                if 0 <= y < n:
                    tiles.append((z, x % n, y))
    return tiles


async def run_prefetch(plugin: WebDashboardPlugin) -> None:
    """Prefetch tiles around the node's position into the disk cache.

    Runs as a background asyncio task; errors are logged, not raised.
    """
    tp = plugin.config.get("tile_proxy", {})
    pf = tp.get("prefetch", {})
    if not pf.get("enabled"):
        return

    min_zoom = pf.get("min_zoom", 6)
    max_zoom = pf.get("max_zoom", 15)
    cache_dir = getattr(plugin, "_tile_cache_dir", "")
    session = getattr(plugin, "_prefetch_session", None) or getattr(plugin, "_tile_session", None)
    upstream = getattr(plugin, "_tile_upstream", "")

    if not cache_dir or not session or not upstream:
        log.warning("Tile prefetch: proxy not initialised, skipping")
        return

    bbox = pf.get("bbox")
    if bbox and len(bbox) == 4:
        south, west, north, east = bbox
        tiles = _tile_list_bbox(south, west, north, east, min_zoom, max_zoom)
        log.info(
            "Tile prefetch: %d tiles for bbox [%.4f,%.4f,%.4f,%.4f] (z%d–z%d)",
            len(tiles),
            south,
            west,
            north,
            east,
            min_zoom,
            max_zoom,
        )
    else:
        lat = pf.get("latitude")
        lon = pf.get("longitude")
        if lat is None or lon is None:
            lat, lon = _detect_position(plugin)
        if lat is None or lon is None:
            log.info("Tile prefetch: no position available, skipping")
            return
        tiles = _tile_list(lat, lon, min_zoom, max_zoom)
        log.info(
            "Tile prefetch: %d tiles for %.4f, %.4f (z%d–z%d)",
            len(tiles),
            lat,
            lon,
            min_zoom,
            max_zoom,
        )

    if len(tiles) > MAX_PREFETCH_TILES:
        log.warning(
            "Tile prefetch: %d tiles exceeds limit of %d, truncating",
            len(tiles),
            MAX_PREFETCH_TILES,
        )
        tiles = tiles[:MAX_PREFETCH_TILES]

    total = len(tiles)
    fetched = 0
    skipped = 0

    max_bytes = getattr(plugin, "_tile_max_bytes", 0)

    for z, x, y in tiles:
        if max_bytes > 0 and getattr(plugin, "_tile_cache_bytes", 0) >= max_bytes:
            log.info("Tile prefetch: cache budget reached, stopping")
            break

        tile_path = os.path.join(cache_dir, str(z), str(x), f"{y}.png")
        if os.path.isfile(tile_path):
            skipped += 1
            continue

        url = upstream.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
        url = url.replace("{s}", "a")

        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    continue
                data = await resp.read()

            tile_dir = os.path.dirname(tile_path)
            os.makedirs(tile_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=tile_dir, suffix=".tmp")
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            os.rename(tmp_path, tile_path)
            plugin._tile_cache_bytes = getattr(plugin, "_tile_cache_bytes", 0) + len(data)
            fetched += 1
        except Exception as exc:
            log.debug("Tile prefetch failed for z%d/%d/%d: %s", z, x, y, exc)
            continue

        await asyncio.sleep(0.2)

    log.info("Tile prefetch complete: %d fetched, %d cached, %d total", fetched, skipped, total)


def _detect_position(plugin: WebDashboardPlugin) -> tuple[float | None, float | None]:
    """Try to get position from GPS plugin or Meshtastic self-node."""
    gps = plugin.app.get_plugin("gps_telemetry") if plugin.app else None
    if gps and hasattr(gps, "last_fix"):
        fix = gps.last_fix
        if fix and fix.get("lat") is not None and fix.get("lon") is not None:
            log.info("Tile prefetch: using GPS position")
            return fix["lat"], fix["lon"]

    msh = plugin.app.get_plugin("meshtastic_gateway") if plugin.app else None
    if msh and hasattr(msh, "get_meshtastic_nodes"):
        try:
            nodes = msh.get_meshtastic_nodes()
            for n in nodes:
                if (
                    n.get("is_self")
                    and n.get("latitude") is not None
                    and n.get("longitude") is not None
                ):
                    log.info("Tile prefetch: using Meshtastic self-node position")
                    return n["latitude"], n["longitude"]
        except Exception:
            log.debug("Meshtastic position lookup failed", exc_info=True)

    return None, None
