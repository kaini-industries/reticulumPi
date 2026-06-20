"""Tests for the tile proxy endpoint in server.py."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_tile_request(z="10", x="512", y="512", plugin=None):
    """Build a mock aiohttp request for the tile proxy handler."""
    request = MagicMock()
    request.match_info = {"z": z, "x": x, "y": y}
    if plugin is None:
        plugin = MagicMock()
        plugin._tile_session = MagicMock()
        plugin._tile_cache_dir = "/tmp/test_tiles"
        plugin._tile_upstream = "https://tile.example.com/{z}/{x}/{y}.png"
        plugin._tile_max_bytes = 500 * 1024 * 1024
        plugin._tile_cache_bytes = 0
    request.app = {"plugin": plugin}
    return request


class TestCoordinateValidation:
    def test_valid_coordinates(self):
        from reticulumpi.builtin_plugins.web_dashboard.server import _handle_tile_proxy

        plugin = MagicMock()
        plugin._tile_session = MagicMock()
        plugin._tile_cache_dir = "/tmp/test_tiles"
        plugin._tile_upstream = "https://tile.example.com/{z}/{x}/{y}.png"
        plugin._tile_max_bytes = 500 * 1024 * 1024
        plugin._tile_cache_bytes = 0

        resp_mock = AsyncMock()
        resp_mock.status = 200
        resp_mock.content = MagicMock()
        resp_mock.content.read = AsyncMock(return_value=b"\x89PNG data")
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=resp_mock)
        ctx.__aexit__ = AsyncMock(return_value=False)
        plugin._tile_session.get = MagicMock(return_value=ctx)

        req = _make_tile_request("10", "512", "512", plugin)

        with (
            patch(
                "reticulumpi.builtin_plugins.web_dashboard.server.os.path.isfile",
                return_value=False,
            ),
            patch("reticulumpi.builtin_plugins.web_dashboard.server.os.makedirs"),
            patch(
                "reticulumpi.builtin_plugins.web_dashboard.server.tempfile.mkstemp",
                return_value=(99, "/tmp/tile.tmp"),
            ),
            patch("reticulumpi.builtin_plugins.web_dashboard.server.os.write"),
            patch("reticulumpi.builtin_plugins.web_dashboard.server.os.close"),
            patch("reticulumpi.builtin_plugins.web_dashboard.server.os.rename"),
        ):
            resp = asyncio.run(_handle_tile_proxy(req))
            assert resp.status == 200

    def test_negative_z_rejected(self):
        import aiohttp.web
        from reticulumpi.builtin_plugins.web_dashboard.server import _handle_tile_proxy

        req = _make_tile_request("-1", "0", "0")
        with pytest.raises(aiohttp.web.HTTPBadRequest):
            asyncio.run(_handle_tile_proxy(req))

    def test_negative_x_rejected(self):
        import aiohttp.web
        from reticulumpi.builtin_plugins.web_dashboard.server import _handle_tile_proxy

        req = _make_tile_request("10", "-1", "0")
        with pytest.raises(aiohttp.web.HTTPBadRequest):
            asyncio.run(_handle_tile_proxy(req))

    def test_negative_y_rejected(self):
        import aiohttp.web
        from reticulumpi.builtin_plugins.web_dashboard.server import _handle_tile_proxy

        req = _make_tile_request("10", "0", "-1")
        with pytest.raises(aiohttp.web.HTTPBadRequest):
            asyncio.run(_handle_tile_proxy(req))

    def test_z_too_large_rejected(self):
        import aiohttp.web
        from reticulumpi.builtin_plugins.web_dashboard.server import _handle_tile_proxy

        req = _make_tile_request("20", "0", "0")
        with pytest.raises(aiohttp.web.HTTPBadRequest):
            asyncio.run(_handle_tile_proxy(req))

    def test_x_exceeds_max_for_zoom(self):
        import aiohttp.web
        from reticulumpi.builtin_plugins.web_dashboard.server import _handle_tile_proxy

        req = _make_tile_request("1", "2", "0")
        with pytest.raises(aiohttp.web.HTTPBadRequest):
            asyncio.run(_handle_tile_proxy(req))

    def test_y_exceeds_max_for_zoom(self):
        import aiohttp.web
        from reticulumpi.builtin_plugins.web_dashboard.server import _handle_tile_proxy

        req = _make_tile_request("1", "0", "2")
        with pytest.raises(aiohttp.web.HTTPBadRequest):
            asyncio.run(_handle_tile_proxy(req))

    def test_non_integer_rejected(self):
        import aiohttp.web
        from reticulumpi.builtin_plugins.web_dashboard.server import _handle_tile_proxy

        req = _make_tile_request("abc", "0", "0")
        with pytest.raises(aiohttp.web.HTTPBadRequest):
            asyncio.run(_handle_tile_proxy(req))

    def test_max_valid_coords_at_zoom_0(self):
        from reticulumpi.builtin_plugins.web_dashboard.server import _handle_tile_proxy

        req = _make_tile_request("0", "0", "0")
        plugin = req.app["plugin"]
        resp_mock = AsyncMock()
        resp_mock.status = 200
        resp_mock.content = MagicMock()
        resp_mock.content.read = AsyncMock(return_value=b"\x89PNG")
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=resp_mock)
        ctx.__aexit__ = AsyncMock(return_value=False)
        plugin._tile_session.get = MagicMock(return_value=ctx)
        plugin._tile_max_bytes = 500 * 1024 * 1024
        plugin._tile_cache_bytes = 0

        with (
            patch(
                "reticulumpi.builtin_plugins.web_dashboard.server.os.path.isfile",
                return_value=False,
            ),
            patch("reticulumpi.builtin_plugins.web_dashboard.server.os.makedirs"),
            patch(
                "reticulumpi.builtin_plugins.web_dashboard.server.tempfile.mkstemp",
                return_value=(99, "/tmp/t.tmp"),
            ),
            patch("reticulumpi.builtin_plugins.web_dashboard.server.os.write"),
            patch("reticulumpi.builtin_plugins.web_dashboard.server.os.close"),
            patch("reticulumpi.builtin_plugins.web_dashboard.server.os.rename"),
        ):
            resp = asyncio.run(_handle_tile_proxy(req))
            assert resp.status == 200

    def test_z0_x1_rejected(self):
        import aiohttp.web
        from reticulumpi.builtin_plugins.web_dashboard.server import _handle_tile_proxy

        req = _make_tile_request("0", "1", "0")
        with pytest.raises(aiohttp.web.HTTPBadRequest):
            asyncio.run(_handle_tile_proxy(req))


class TestCacheHit:
    def test_serves_from_cache(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.server import _handle_tile_proxy

        tile_dir = tmp_path / "10" / "512"
        tile_dir.mkdir(parents=True)
        tile_file = tile_dir / "512.png"
        tile_file.write_bytes(b"\x89PNG cached tile data")

        plugin = MagicMock()
        plugin._tile_session = MagicMock()
        plugin._tile_cache_dir = str(tmp_path)

        req = _make_tile_request("10", "512", "512", plugin)

        resp = asyncio.run(_handle_tile_proxy(req))
        assert resp.status == 200
        plugin._tile_session.get.assert_not_called()


class TestCacheMiss:
    def test_fetches_upstream_and_caches(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.server import _handle_tile_proxy

        tile_data = b"\x89PNG upstream tile"
        plugin = MagicMock()
        plugin._tile_session = MagicMock()
        plugin._tile_cache_dir = str(tmp_path)
        plugin._tile_upstream = "https://tile.example.com/{z}/{x}/{y}.png"
        plugin._tile_max_bytes = 500 * 1024 * 1024
        plugin._tile_cache_bytes = 0

        resp_mock = AsyncMock()
        resp_mock.status = 200
        resp_mock.content = MagicMock()
        resp_mock.content.read = AsyncMock(return_value=tile_data)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=resp_mock)
        ctx.__aexit__ = AsyncMock(return_value=False)
        plugin._tile_session.get = MagicMock(return_value=ctx)

        req = _make_tile_request("10", "512", "512", plugin)

        resp = asyncio.run(_handle_tile_proxy(req))
        assert resp.status == 200
        assert resp.body == tile_data

        cached_tile = tmp_path / "10" / "512" / "512.png"
        assert cached_tile.exists()
        assert cached_tile.read_bytes() == tile_data

    def test_upstream_error_returns_502(self):
        import aiohttp.web
        from reticulumpi.builtin_plugins.web_dashboard.server import _handle_tile_proxy

        plugin = MagicMock()
        plugin._tile_session = MagicMock()
        plugin._tile_cache_dir = "/tmp/test_tiles"
        plugin._tile_upstream = "https://tile.example.com/{z}/{x}/{y}.png"
        plugin._tile_max_bytes = 500 * 1024 * 1024
        plugin._tile_cache_bytes = 0

        resp_mock = AsyncMock()
        resp_mock.status = 404
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=resp_mock)
        ctx.__aexit__ = AsyncMock(return_value=False)
        plugin._tile_session.get = MagicMock(return_value=ctx)

        req = _make_tile_request("10", "512", "512", plugin)

        with patch(
            "reticulumpi.builtin_plugins.web_dashboard.server.os.path.isfile", return_value=False
        ):
            with pytest.raises(aiohttp.web.HTTPBadGateway):
                asyncio.run(_handle_tile_proxy(req))

    def test_upstream_timeout_returns_504(self):
        import aiohttp
        import aiohttp.web
        from reticulumpi.builtin_plugins.web_dashboard.server import _handle_tile_proxy

        plugin = MagicMock()
        plugin._tile_session = MagicMock()
        plugin._tile_cache_dir = "/tmp/test_tiles"
        plugin._tile_upstream = "https://tile.example.com/{z}/{x}/{y}.png"
        plugin._tile_max_bytes = 500 * 1024 * 1024
        plugin._tile_cache_bytes = 0
        plugin._tile_session.get = MagicMock(side_effect=asyncio.TimeoutError())

        req = _make_tile_request("10", "512", "512", plugin)

        with patch(
            "reticulumpi.builtin_plugins.web_dashboard.server.os.path.isfile", return_value=False
        ):
            with pytest.raises(aiohttp.web.HTTPGatewayTimeout):
                asyncio.run(_handle_tile_proxy(req))


class TestCacheSizeEnforcement:
    def test_skips_write_when_cache_full(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.server import _handle_tile_proxy

        tile_data = b"\x89PNG tile"
        plugin = MagicMock()
        plugin._tile_session = MagicMock()
        plugin._tile_cache_dir = str(tmp_path)
        plugin._tile_upstream = "https://tile.example.com/{z}/{x}/{y}.png"
        plugin._tile_max_bytes = 100
        plugin._tile_cache_bytes = 100  # Already at limit

        resp_mock = AsyncMock()
        resp_mock.status = 200
        resp_mock.content = MagicMock()
        resp_mock.content.read = AsyncMock(return_value=tile_data)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=resp_mock)
        ctx.__aexit__ = AsyncMock(return_value=False)
        plugin._tile_session.get = MagicMock(return_value=ctx)

        req = _make_tile_request("10", "512", "512", plugin)

        resp = asyncio.run(_handle_tile_proxy(req))
        assert resp.status == 200
        assert resp.body == tile_data

        cached_tile = tmp_path / "10" / "512" / "512.png"
        assert not cached_tile.exists()

    def test_writes_when_under_limit(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.server import _handle_tile_proxy

        tile_data = b"\x89PNG tile"
        plugin = MagicMock()
        plugin._tile_session = MagicMock()
        plugin._tile_cache_dir = str(tmp_path)
        plugin._tile_upstream = "https://tile.example.com/{z}/{x}/{y}.png"
        plugin._tile_max_bytes = 500 * 1024 * 1024
        plugin._tile_cache_bytes = 0

        resp_mock = AsyncMock()
        resp_mock.status = 200
        resp_mock.content = MagicMock()
        resp_mock.content.read = AsyncMock(return_value=tile_data)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=resp_mock)
        ctx.__aexit__ = AsyncMock(return_value=False)
        plugin._tile_session.get = MagicMock(return_value=ctx)

        req = _make_tile_request("10", "512", "512", plugin)

        resp = asyncio.run(_handle_tile_proxy(req))
        assert resp.status == 200

        cached_tile = tmp_path / "10" / "512" / "512.png"
        assert cached_tile.exists()

    def test_increments_cache_bytes(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.server import _handle_tile_proxy

        tile_data = b"\x89PNG tile data here"
        plugin = MagicMock()
        plugin._tile_session = MagicMock()
        plugin._tile_cache_dir = str(tmp_path)
        plugin._tile_upstream = "https://tile.example.com/{z}/{x}/{y}.png"
        plugin._tile_max_bytes = 500 * 1024 * 1024
        plugin._tile_cache_bytes = 0

        resp_mock = AsyncMock()
        resp_mock.status = 200
        resp_mock.content = MagicMock()
        resp_mock.content.read = AsyncMock(return_value=tile_data)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=resp_mock)
        ctx.__aexit__ = AsyncMock(return_value=False)
        plugin._tile_session.get = MagicMock(return_value=ctx)

        req = _make_tile_request("10", "512", "512", plugin)

        asyncio.run(_handle_tile_proxy(req))
        assert plugin._tile_cache_bytes == len(tile_data)


class TestNoSession:
    def test_returns_503_without_session(self):
        import aiohttp.web
        from reticulumpi.builtin_plugins.web_dashboard.server import _handle_tile_proxy

        plugin = MagicMock()
        plugin._tile_session = None
        req = _make_tile_request("10", "0", "0", plugin)

        with pytest.raises(aiohttp.web.HTTPServiceUnavailable):
            asyncio.run(_handle_tile_proxy(req))


class TestBboxConfigValidation:
    def _make_plugin(self, tile_proxy_config):
        from reticulumpi.builtin_plugins.web_dashboard.plugin import WebDashboardPlugin

        plugin = MagicMock(spec=WebDashboardPlugin)
        plugin.config = {
            "host": "127.0.0.1",
            "port": 8080,
            "tile_cache_entries": 5000,
            "tile_proxy": tile_proxy_config,
            "lora_region": "US",
        }
        plugin.validate_config = WebDashboardPlugin.validate_config.__get__(plugin)
        return plugin

    def test_valid_bbox(self):
        plugin = self._make_plugin(
            {
                "enabled": True,
                "prefetch": {"bbox": [40.0, -74.5, 41.0, -73.5]},
            }
        )
        plugin.validate_config()

    def test_south_greater_than_north(self):
        plugin = self._make_plugin(
            {
                "enabled": True,
                "prefetch": {"bbox": [41.0, -74.5, 40.0, -73.5]},
            }
        )
        with pytest.raises(ValueError, match="south < north"):
            plugin.validate_config()

    def test_west_greater_than_east(self):
        plugin = self._make_plugin(
            {
                "enabled": True,
                "prefetch": {"bbox": [40.0, 10.0, 41.0, -10.0]},
            }
        )
        with pytest.raises(ValueError, match="west < east"):
            plugin.validate_config()

    def test_latitude_out_of_range(self):
        plugin = self._make_plugin(
            {
                "enabled": True,
                "prefetch": {"bbox": [-91.0, -74.5, 41.0, -73.5]},
            }
        )
        with pytest.raises(ValueError, match="south < north"):
            plugin.validate_config()

    def test_longitude_out_of_range(self):
        plugin = self._make_plugin(
            {
                "enabled": True,
                "prefetch": {"bbox": [40.0, -181.0, 41.0, -73.5]},
            }
        )
        with pytest.raises(ValueError, match="west < east"):
            plugin.validate_config()

    def test_wrong_length(self):
        plugin = self._make_plugin(
            {
                "enabled": True,
                "prefetch": {"bbox": [40.0, -74.5, 41.0]},
            }
        )
        with pytest.raises(ValueError, match="south, west, north, east"):
            plugin.validate_config()

    def test_null_bbox_is_valid(self):
        plugin = self._make_plugin(
            {
                "enabled": True,
                "prefetch": {"bbox": None},
            }
        )
        plugin.validate_config()
