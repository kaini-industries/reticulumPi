"""Tests for the tile prefetch module."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestLatLonToTile:
    def test_known_position(self):
        from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import _lat_lon_to_tile

        x, y = _lat_lon_to_tile(0.0, 0.0, 1)
        assert x == 1
        assert y == 1

    def test_zoom_zero(self):
        from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import _lat_lon_to_tile

        x, y = _lat_lon_to_tile(40.0, -74.0, 0)
        assert x == 0
        assert y == 0

    def test_high_zoom(self):
        from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import _lat_lon_to_tile

        x, y = _lat_lon_to_tile(51.5074, -0.1278, 10)
        assert 0 <= x < 1024
        assert 0 <= y < 1024

    def test_clamps_extreme_latitude(self):
        from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import _lat_lon_to_tile

        x, y = _lat_lon_to_tile(90.0, 0.0, 2)
        assert 0 <= x < 4
        assert 0 <= y < 4


class TestGridRadius:
    def test_low_zoom(self):
        from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import _grid_radius

        assert _grid_radius(5) == 1
        assert _grid_radius(10) == 1

    def test_mid_zoom(self):
        from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import _grid_radius

        assert _grid_radius(11) == 2
        assert _grid_radius(13) == 2

    def test_high_zoom(self):
        from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import _grid_radius

        assert _grid_radius(14) == 3
        assert _grid_radius(19) == 3


class TestTileList:
    def test_returns_tiles(self):
        from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import _tile_list

        tiles = _tile_list(40.0, -74.0, 10, 10)
        assert len(tiles) > 0
        for z, x, y in tiles:
            assert z == 10
            assert 0 <= x < 1024
            assert 0 <= y < 1024

    def test_multiple_zooms(self):
        from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import _tile_list

        tiles = _tile_list(40.0, -74.0, 8, 10)
        zooms = {z for z, x, y in tiles}
        assert zooms == {8, 9, 10}

    def test_no_duplicates(self):
        from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import _tile_list

        tiles = _tile_list(0.0, 0.0, 6, 12)
        assert len(tiles) == len(set(tiles))


class TestTileListBbox:
    def test_small_bbox(self):
        from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import _tile_list_bbox

        tiles = _tile_list_bbox(40.0, -74.5, 41.0, -73.5, 10, 10)
        assert len(tiles) > 0
        for z, x, y in tiles:
            assert z == 10
            assert 0 <= x < 1024
            assert 0 <= y < 1024

    def test_multiple_zooms(self):
        from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import _tile_list_bbox

        tiles = _tile_list_bbox(40.0, -74.5, 41.0, -73.5, 8, 10)
        zooms = {z for z, _, _ in tiles}
        assert zooms == {8, 9, 10}


class TestMaxPrefetchTiles:
    def test_constant_exists(self):
        from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import MAX_PREFETCH_TILES

        assert MAX_PREFETCH_TILES == 10_000


class TestRunPrefetch:
    def _make_plugin(self, tmp_path, *, bbox=None, lat=None, lon=None):
        plugin = MagicMock()
        plugin.config = {
            "tile_proxy": {
                "enabled": True,
                "prefetch": {
                    "enabled": True,
                    "min_zoom": 10,
                    "max_zoom": 10,
                },
            }
        }
        if bbox is not None:
            plugin.config["tile_proxy"]["prefetch"]["bbox"] = bbox
        if lat is not None:
            plugin.config["tile_proxy"]["prefetch"]["latitude"] = lat
            plugin.config["tile_proxy"]["prefetch"]["longitude"] = lon

        cache_dir = str(tmp_path / "tiles")
        os.makedirs(cache_dir, exist_ok=True)
        plugin._tile_cache_dir = cache_dir
        plugin._tile_upstream = "https://tile.example.com/{z}/{x}/{y}.png"
        plugin._tile_max_bytes = 500 * 1024 * 1024
        plugin._tile_cache_bytes = 0

        session = MagicMock()
        resp = AsyncMock()
        resp.status = 200
        resp.read = AsyncMock(return_value=b"\x89PNG fake tile data")
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session.get = MagicMock(return_value=ctx)
        plugin._prefetch_session = session
        plugin._tile_session = None

        return plugin

    @pytest.mark.asyncio
    async def test_skips_when_disabled(self):
        from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import run_prefetch

        plugin = MagicMock()
        plugin.config = {"tile_proxy": {"prefetch": {"enabled": False}}}
        await run_prefetch(plugin)

    @pytest.mark.asyncio
    async def test_fetches_tiles_with_lat_lon(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import run_prefetch

        plugin = self._make_plugin(tmp_path, lat=40.0, lon=-74.0)
        with patch("reticulumpi.builtin_plugins.web_dashboard.tile_prefetch.asyncio.sleep",
                    new_callable=AsyncMock):
            await run_prefetch(plugin)

        cached = list((tmp_path / "tiles").rglob("*.png"))
        assert len(cached) > 0

    @pytest.mark.asyncio
    async def test_fetches_tiles_with_bbox(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import run_prefetch

        plugin = self._make_plugin(tmp_path, bbox=[40.0, -74.5, 41.0, -73.5])
        with patch("reticulumpi.builtin_plugins.web_dashboard.tile_prefetch.asyncio.sleep",
                    new_callable=AsyncMock):
            await run_prefetch(plugin)

        cached = list((tmp_path / "tiles").rglob("*.png"))
        assert len(cached) > 0

    @pytest.mark.asyncio
    async def test_skips_existing_cached_tiles(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import run_prefetch

        plugin = self._make_plugin(tmp_path, lat=0.0, lon=0.0)

        # Pre-populate one tile so it gets skipped
        tile_dir = tmp_path / "tiles" / "10" / "512"
        tile_dir.mkdir(parents=True, exist_ok=True)
        (tile_dir / "512.png").write_bytes(b"existing")

        with patch("reticulumpi.builtin_plugins.web_dashboard.tile_prefetch.asyncio.sleep",
                    new_callable=AsyncMock):
            await run_prefetch(plugin)

        assert (tile_dir / "512.png").read_bytes() == b"existing"

    @pytest.mark.asyncio
    async def test_logs_fetch_errors(self, tmp_path, caplog):
        from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import run_prefetch

        plugin = self._make_plugin(tmp_path, lat=40.0, lon=-74.0)
        plugin._prefetch_session.get.side_effect = Exception("connection refused")

        import logging
        with caplog.at_level(logging.DEBUG):
            with patch(
                "reticulumpi.builtin_plugins.web_dashboard.tile_prefetch.asyncio.sleep",
                new_callable=AsyncMock,
            ):
                await run_prefetch(plugin)

        assert any("prefetch failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_atomic_write(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import run_prefetch

        plugin = self._make_plugin(tmp_path, lat=0.0, lon=0.0)
        with patch(
            "reticulumpi.builtin_plugins.web_dashboard.tile_prefetch.asyncio.sleep",
            new_callable=AsyncMock,
        ), patch(
            "reticulumpi.builtin_plugins.web_dashboard.tile_prefetch.tempfile.mkstemp"
        ) as mock_mkstemp, patch(
            "reticulumpi.builtin_plugins.web_dashboard.tile_prefetch.os.write"
        ), patch(
            "reticulumpi.builtin_plugins.web_dashboard.tile_prefetch.os.close"
        ), patch(
            "reticulumpi.builtin_plugins.web_dashboard.tile_prefetch.os.rename"
        ) as mock_rename:
            mock_mkstemp.return_value = (99, "/tmp/tile.tmp")
            await run_prefetch(plugin)

        assert mock_mkstemp.called
        assert mock_rename.called

    @pytest.mark.asyncio
    async def test_truncates_at_max_tiles(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import (
            MAX_PREFETCH_TILES,
            run_prefetch,
        )

        plugin = self._make_plugin(tmp_path, bbox=[-85, -180, 85, 180])
        plugin.config["tile_proxy"]["prefetch"]["min_zoom"] = 0
        plugin.config["tile_proxy"]["prefetch"]["max_zoom"] = 15

        call_count = 0
        original_get = plugin._prefetch_session.get

        def counting_get(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return original_get(*args, **kwargs)

        plugin._prefetch_session.get = counting_get

        with patch(
            "reticulumpi.builtin_plugins.web_dashboard.tile_prefetch.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            await run_prefetch(plugin)

        assert call_count <= MAX_PREFETCH_TILES


class TestPrefetchCacheBudget:
    def _make_plugin(self, tmp_path, *, lat=0.0, lon=0.0, max_bytes=200, cache_bytes=0):
        plugin = MagicMock()
        plugin.config = {
            "tile_proxy": {
                "enabled": True,
                "prefetch": {
                    "enabled": True,
                    "min_zoom": 10,
                    "max_zoom": 10,
                    "latitude": lat,
                    "longitude": lon,
                },
            }
        }

        cache_dir = str(tmp_path / "tiles")
        os.makedirs(cache_dir, exist_ok=True)
        plugin._tile_cache_dir = cache_dir
        plugin._tile_upstream = "https://tile.example.com/{z}/{x}/{y}.png"
        plugin._tile_max_bytes = max_bytes
        plugin._tile_cache_bytes = cache_bytes

        session = MagicMock()
        resp = AsyncMock()
        resp.status = 200
        resp.read = AsyncMock(return_value=b"\x89PNG fake tile data")
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session.get = MagicMock(return_value=ctx)
        plugin._prefetch_session = session
        plugin._tile_session = None

        return plugin

    @pytest.mark.asyncio
    async def test_stops_at_budget(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import run_prefetch

        plugin = self._make_plugin(tmp_path, max_bytes=50, cache_bytes=50)
        with patch("reticulumpi.builtin_plugins.web_dashboard.tile_prefetch.asyncio.sleep",
                    new_callable=AsyncMock):
            await run_prefetch(plugin)

        cached = list((tmp_path / "tiles").rglob("*.png"))
        assert len(cached) == 0

    @pytest.mark.asyncio
    async def test_increments_cache_bytes(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import run_prefetch

        plugin = self._make_plugin(tmp_path, max_bytes=500 * 1024 * 1024, cache_bytes=0)
        with patch("reticulumpi.builtin_plugins.web_dashboard.tile_prefetch.asyncio.sleep",
                    new_callable=AsyncMock):
            await run_prefetch(plugin)

        assert plugin._tile_cache_bytes > 0
        cached = list((tmp_path / "tiles").rglob("*.png"))
        assert len(cached) > 0


class TestDetectPosition:
    def test_uses_gps_first(self):
        from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import _detect_position

        plugin = MagicMock()
        gps = MagicMock()
        gps.last_fix = {"lat": 42.0, "lon": -71.0}
        plugin.app.get_plugin.return_value = gps
        lat, lon = _detect_position(plugin)
        assert lat == 42.0
        assert lon == -71.0

    def test_falls_back_to_meshtastic(self):
        from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import _detect_position

        plugin = MagicMock()
        gps = MagicMock()
        gps.last_fix = None

        msh = MagicMock()
        msh.get_meshtastic_nodes.return_value = [
            {"is_self": True, "latitude": 51.5, "longitude": -0.1}
        ]

        def get_plugin(name):
            if name == "gps_telemetry":
                return gps
            if name == "meshtastic_gateway":
                return msh
            return None

        plugin.app.get_plugin.side_effect = get_plugin
        lat, lon = _detect_position(plugin)
        assert lat == 51.5
        assert lon == -0.1

    def test_returns_none_when_no_position(self):
        from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import _detect_position

        plugin = MagicMock()
        gps = MagicMock()
        gps.last_fix = None
        msh = MagicMock()
        msh.get_meshtastic_nodes.return_value = []

        def get_plugin(name):
            if name == "gps_telemetry":
                return gps
            if name == "meshtastic_gateway":
                return msh
            return None

        plugin.app.get_plugin.side_effect = get_plugin
        lat, lon = _detect_position(plugin)
        assert lat is None
        assert lon is None
