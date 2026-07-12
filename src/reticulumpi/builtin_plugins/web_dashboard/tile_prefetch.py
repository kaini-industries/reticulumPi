"""Background tile prefetch — pre-seed the disk cache around the node position."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import stat
import tempfile
from typing import TYPE_CHECKING

from reticulumpi.builtin_plugins.web_dashboard.operational_metrics import (
    record_tile_eviction,
    record_tile_hit,
    record_tile_miss,
    record_tile_reject,
    record_tile_stored,
)
from reticulumpi.plugin_base import resolve_ready_plugin

if TYPE_CHECKING:
    from reticulumpi.builtin_plugins.web_dashboard.plugin import WebDashboardPlugin

log = logging.getLogger(__name__)

MAX_PREFETCH_TILES = 10_000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class _TileLockEntry:
    """Reference-counted lock shared by proxy and prefetch for one tile."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0


class _TileLockLease:
    """Keep a tile lock registered only while a caller uses or waits for it."""

    def __init__(self, locks: dict[str, object], path: str) -> None:
        self._locks = locks
        self._path = path
        entry = locks.get(path)
        if not isinstance(entry, _TileLockEntry):
            entry = _TileLockEntry()
            locks[path] = entry
        self._entry = entry
        entry.users += 1

    async def __aenter__(self) -> None:
        try:
            await self._entry.lock.acquire()
        except BaseException:
            self._drop_user()
            raise

    async def __aexit__(self, *_exc_info: object) -> None:
        self._entry.lock.release()
        self._drop_user()

    def _drop_user(self) -> None:
        self._entry.users -= 1
        if self._entry.users == 0 and self._locks.get(self._path) is self._entry:
            self._locks.pop(self._path, None)


def _cache_lock(plugin: WebDashboardPlugin) -> asyncio.Lock:
    lock = getattr(plugin, "_tile_cache_lock", None)
    if not isinstance(lock, asyncio.Lock):
        lock = plugin._tile_cache_lock = asyncio.Lock()
    return lock


def _tile_locks(plugin: WebDashboardPlugin) -> dict[str, object]:
    locks = getattr(plugin, "_tile_locks", None)
    if not isinstance(locks, dict):
        locks = plugin._tile_locks = {}
    return locks


def _scan_cache_files(cache_dir: str) -> tuple[list[tuple[int, str, int]], int]:
    """Return regular PNGs by age and their actual bytes without following links."""

    files: list[tuple[int, str, int]] = []
    total = 0
    try:
        cache_stat = os.lstat(cache_dir)
    except OSError:
        return files, total
    if not stat.S_ISDIR(cache_stat.st_mode):
        return files, total
    for current, directories, names in os.walk(cache_dir, followlinks=False):
        directories[:] = [
            name for name in directories if not os.path.islink(os.path.join(current, name))
        ]
        for name in names:
            path = os.path.join(current, name)
            if name.endswith(".tmp"):
                try:
                    if not os.path.islink(path):
                        os.unlink(path)
                except OSError:
                    pass
                continue
            if not name.endswith(".png") or os.path.islink(path):
                continue
            try:
                file_stat = os.stat(path, follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(file_stat.st_mode):
                continue
            size = max(0, int(file_stat.st_size))
            total += size
            files.append((file_stat.st_mtime_ns, path, size))
    files.sort(key=lambda item: (item[0], item[1]))
    return files, total


def _evict_to_budget(
    plugin: WebDashboardPlugin,
    files: list[tuple[int, str, int]],
    total: int,
    incoming: int = 0,
) -> tuple[int, bool]:
    max_bytes = getattr(plugin, "_tile_max_bytes", 0)
    if not isinstance(max_bytes, int) or max_bytes <= 0:
        return total, True
    for _mtime, path, size in files:
        if total + incoming <= max_bytes:
            break
        try:
            os.unlink(path)
        except OSError:
            continue
        total = max(0, total - size)
        record_tile_eviction()
    return total, total + incoming <= max_bytes


async def enforce_tile_cache_budget(plugin: WebDashboardPlugin) -> bool:
    """Reconcile actual disk usage and evict oldest tiles under one lock."""

    async with _cache_lock(plugin):
        files, total = _scan_cache_files(getattr(plugin, "_tile_cache_dir", ""))
        total, within_budget = _evict_to_budget(plugin, files, total)
        plugin._tile_cache_bytes = total
        return within_budget


async def store_tile(plugin: WebDashboardPlugin, tile_path: str, data: bytes) -> bool:
    """Atomically store one tile after actual-usage reconciliation and eviction."""

    async with _cache_lock(plugin):
        files, total = _scan_cache_files(getattr(plugin, "_tile_cache_dir", ""))
        total, has_capacity = _evict_to_budget(plugin, files, total, len(data))
        if not has_capacity:
            plugin._tile_cache_bytes = total
            return False

        tile_dir = os.path.dirname(tile_path)
        os.makedirs(tile_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=tile_dir, suffix=".tmp")
        try:
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp_path, tile_path)
            directory_fd = os.open(tile_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        stored_size = os.path.getsize(tile_path)
        plugin._tile_cache_bytes = total + stored_size
        record_tile_stored()
        return True


async def _prefetch_one_tile(
    plugin: WebDashboardPlugin,
    session: object,
    upstream: str,
    z: int,
    x: int,
    y: int,
    max_tile_bytes: int,
) -> str:
    """Fetch at most once across prefetch/proxy and return a bounded outcome."""

    tile_path = os.path.join(plugin._tile_cache_dir, str(z), str(x), f"{y}.png")
    async with _TileLockLease(_tile_locks(plugin), tile_path):
        if os.path.isfile(tile_path):
            record_tile_hit()
            return "cached"

        url = upstream.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
        url = url.replace("{s}", "a")
        record_tile_miss()
        try:
            async with session.get(url) as response:
                if response.status != 200:
                    record_tile_reject("upstream")
                    return "failed"
                if not is_png_content_type(response.headers.get("Content-Type", "")):
                    record_tile_reject("invalid_content")
                    return "failed"
                data = await response.content.read(max_tile_bytes + 1)
                if not isinstance(data, (bytes, bytearray)):
                    data = await response.read()
        except Exception as exc:
            record_tile_reject("upstream")
            log.debug("Tile prefetch failed for z%d/%d/%d: %s", z, x, y, exc)
            return "failed"

        if len(data) > max_tile_bytes:
            record_tile_reject("oversize")
            return "failed"
        if not data.startswith(PNG_SIGNATURE):
            record_tile_reject("invalid_content")
            return "failed"
        try:
            if not await store_tile(plugin, tile_path, bytes(data)):
                record_tile_reject("capacity")
                return "capacity"
        except OSError as exc:
            record_tile_reject("write_error")
            log.debug("Tile prefetch write failed for z%d/%d/%d: %s", z, x, y, exc)
            return "failed"
        return "stored"


def is_png_content_type(value: object) -> bool:
    """Return whether an upstream media type is explicitly PNG."""
    if not isinstance(value, str):
        return False
    return value.split(";", 1)[0].strip().lower() == "image/png"


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
    limit: int | None = None,
) -> list[tuple[int, int, int]]:
    """Return bbox tiles without ever materialising more than ``limit``."""
    tiles = []
    for z in range(min_zoom, max_zoom + 1):
        x_min, y_min = _lat_lon_to_tile(north, west, z)
        x_max, y_max = _lat_lon_to_tile(south, east, z)
        n = 2**z
        for x in range(x_min, x_max + 1):
            for y in range(y_min, y_max + 1):
                if 0 <= y < n:
                    tiles.append((z, x % n, y))
                    if limit is not None and len(tiles) >= limit:
                        return tiles
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
        record_tile_reject("unavailable")
        log.warning("Tile prefetch: proxy not initialised, skipping")
        return

    bbox = pf.get("bbox")
    if bbox and len(bbox) == 4:
        south, west, north, east = bbox
        tiles = _tile_list_bbox(
            south,
            west,
            north,
            east,
            min_zoom,
            max_zoom,
            limit=MAX_PREFETCH_TILES + 1,
        )
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

    max_tile_bytes = getattr(plugin, "_tile_max_tile_bytes", 512_000)
    if not isinstance(max_tile_bytes, int) or max_tile_bytes <= 0:
        max_tile_bytes = 512_000
    if not await enforce_tile_cache_budget(plugin):
        record_tile_reject("capacity")
        log.warning("Tile prefetch: cache remains over budget after eviction failures")
        return

    for z, x, y in tiles:
        outcome = await _prefetch_one_tile(
            plugin,
            session,
            upstream,
            z,
            x,
            y,
            max_tile_bytes,
        )
        if outcome == "cached":
            skipped += 1
            continue
        if outcome == "capacity":
            log.info("Tile prefetch: cache budget reached, stopping")
            break
        if outcome == "stored":
            fetched += 1
            await asyncio.sleep(0.2)

    log.info("Tile prefetch complete: %d fetched, %d cached, %d total", fetched, skipped, total)


def _detect_position(plugin: WebDashboardPlugin) -> tuple[float | None, float | None]:
    """Try to get position from GPS plugin or Meshtastic self-node."""
    gps = resolve_ready_plugin(plugin, "gps_telemetry") if plugin.app else None
    if gps and hasattr(gps, "last_fix"):
        fix = gps.last_fix
        if fix and fix.get("lat") is not None and fix.get("lon") is not None:
            log.info("Tile prefetch: using GPS position")
            return fix["lat"], fix["lon"]

    msh = resolve_ready_plugin(plugin, "meshtastic_gateway") if plugin.app else None
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
