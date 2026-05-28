"""Background tile prefetch — pre-seed the disk cache around the node position."""

from __future__ import annotations

import asyncio
import logging
import math
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reticulumpi.builtin_plugins.web_dashboard.plugin import WebDashboardPlugin

log = logging.getLogger(__name__)


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


async def run_prefetch(plugin: WebDashboardPlugin) -> None:
    """Prefetch tiles around the node's position into the disk cache.

    Runs as a background asyncio task; errors are logged, not raised.
    """
    tp = plugin.config.get("tile_proxy", {})
    pf = tp.get("prefetch", {})
    if not pf.get("enabled"):
        return

    lat = pf.get("latitude")
    lon = pf.get("longitude")

    if lat is None or lon is None:
        lat, lon = _detect_position(plugin)
    if lat is None or lon is None:
        log.info("Tile prefetch: no position available, skipping")
        return

    min_zoom = pf.get("min_zoom", 6)
    max_zoom = pf.get("max_zoom", 15)
    cache_dir = getattr(plugin, "_tile_cache_dir", "")
    session = getattr(plugin, "_tile_session", None)
    upstream = getattr(plugin, "_tile_upstream", "")

    if not cache_dir or not session or not upstream:
        log.warning("Tile prefetch: proxy not initialised, skipping")
        return

    tiles = _tile_list(lat, lon, min_zoom, max_zoom)
    total = len(tiles)
    fetched = 0
    skipped = 0

    log.info("Tile prefetch: %d tiles for %.4f, %.4f (z%d–z%d)", total, lat, lon, min_zoom, max_zoom)

    for z, x, y in tiles:
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
            with open(tile_path, "wb") as f:
                f.write(data)
            fetched += 1
        except Exception:
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
                if n.get("is_self") and n.get("latitude") is not None and n.get("longitude") is not None:
                    log.info("Tile prefetch: using Meshtastic self-node position")
                    return n["latitude"], n["longitude"]
        except Exception:
            pass

    return None, None
