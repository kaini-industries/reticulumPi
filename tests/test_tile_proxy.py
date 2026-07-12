"""Tests for the tile proxy endpoint in server.py."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


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
        resp_mock.headers = {"Content-Type": "image/png"}
        resp_mock.content = MagicMock()
        resp_mock.content.read = AsyncMock(return_value=PNG_SIGNATURE + b"data")
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
            patch(
                "reticulumpi.builtin_plugins.web_dashboard.server.store_tile",
                new_callable=AsyncMock,
                return_value=True,
            ),
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
        resp_mock.headers = {"Content-Type": "image/png"}
        resp_mock.content = MagicMock()
        resp_mock.content.read = AsyncMock(return_value=PNG_SIGNATURE)
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
            patch(
                "reticulumpi.builtin_plugins.web_dashboard.server.store_tile",
                new_callable=AsyncMock,
                return_value=True,
            ),
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

        tile_data = PNG_SIGNATURE + b"upstream tile"
        plugin = MagicMock()
        plugin._tile_session = MagicMock()
        plugin._tile_cache_dir = str(tmp_path)
        plugin._tile_upstream = "https://tile.example.com/{z}/{x}/{y}.png"
        plugin._tile_max_bytes = 500 * 1024 * 1024
        plugin._tile_cache_bytes = 0

        resp_mock = AsyncMock()
        resp_mock.status = 200
        resp_mock.headers = {"Content-Type": "image/png"}
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
    def test_evicts_oldest_tile_when_cache_is_full(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.server import _handle_tile_proxy

        tile_data = PNG_SIGNATURE + b"tile"
        plugin = MagicMock()
        plugin._tile_session = MagicMock()
        plugin._tile_cache_dir = str(tmp_path)
        plugin._tile_upstream = "https://tile.example.com/{z}/{x}/{y}.png"
        plugin._tile_max_bytes = len(tile_data)
        old_tile = tmp_path / "1" / "0" / "0.png"
        old_tile.parent.mkdir(parents=True)
        old_tile.write_bytes(b"x" * len(tile_data))
        plugin._tile_cache_bytes = len(tile_data)

        resp_mock = AsyncMock()
        resp_mock.status = 200
        resp_mock.headers = {"Content-Type": "image/png"}
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
        assert not old_tile.exists()
        assert plugin._tile_cache_bytes == len(tile_data)

    def test_rejects_cache_write_when_one_tile_exceeds_total_budget(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.server import _handle_tile_proxy

        tile_data = PNG_SIGNATURE + b"tile"
        plugin = MagicMock()
        plugin._tile_session = MagicMock()
        plugin._tile_cache_dir = str(tmp_path)
        plugin._tile_upstream = "https://tile.example.com/{z}/{x}/{y}.png"
        plugin._tile_max_bytes = len(tile_data) - 1
        plugin._tile_cache_bytes = 0

        response = AsyncMock()
        response.status = 200
        response.headers = {"Content-Type": "image/png"}
        response.content.read = AsyncMock(return_value=tile_data)
        context = AsyncMock()
        context.__aenter__ = AsyncMock(return_value=response)
        context.__aexit__ = AsyncMock(return_value=False)
        plugin._tile_session.get = MagicMock(return_value=context)

        result = asyncio.run(_handle_tile_proxy(_make_tile_request("10", "512", "512", plugin)))

        assert result.status == 200
        assert not (tmp_path / "10" / "512" / "512.png").exists()
        assert plugin._tile_cache_bytes == 0

    def test_writes_when_under_limit(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.server import _handle_tile_proxy

        tile_data = PNG_SIGNATURE + b"tile"
        plugin = MagicMock()
        plugin._tile_session = MagicMock()
        plugin._tile_cache_dir = str(tmp_path)
        plugin._tile_upstream = "https://tile.example.com/{z}/{x}/{y}.png"
        plugin._tile_max_bytes = 500 * 1024 * 1024
        plugin._tile_cache_bytes = 0

        resp_mock = AsyncMock()
        resp_mock.status = 200
        resp_mock.headers = {"Content-Type": "image/png"}
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

        tile_data = PNG_SIGNATURE + b"tile data here"
        plugin = MagicMock()
        plugin._tile_session = MagicMock()
        plugin._tile_cache_dir = str(tmp_path)
        plugin._tile_upstream = "https://tile.example.com/{z}/{x}/{y}.png"
        plugin._tile_max_bytes = 500 * 1024 * 1024
        plugin._tile_cache_bytes = 0

        resp_mock = AsyncMock()
        resp_mock.status = 200
        resp_mock.headers = {"Content-Type": "image/png"}
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


@pytest.mark.asyncio
async def test_proxy_and_prefetch_share_one_concurrent_miss_lock(tmp_path):
    from reticulumpi.builtin_plugins.web_dashboard.server import _handle_tile_proxy
    from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import _prefetch_one_tile

    payload = PNG_SIGNATURE + b"shared"
    read_started = asyncio.Event()
    release_read = asyncio.Event()

    async def read(_limit):
        read_started.set()
        await release_read.wait()
        return payload

    response = SimpleNamespace(
        status=200,
        headers={"Content-Type": "image/png"},
        content=SimpleNamespace(read=read),
    )

    class ResponseContext:
        async def __aenter__(self):
            return response

        async def __aexit__(self, *_exc):
            return False

    session = SimpleNamespace(get=MagicMock(side_effect=lambda _url: ResponseContext()))
    plugin = SimpleNamespace(
        _tile_session=session,
        _tile_cache_dir=str(tmp_path),
        _tile_upstream="https://tile.example.com/{z}/{x}/{y}.png",
        _tile_max_tile_bytes=1024,
        _tile_max_bytes=1024,
        _tile_cache_bytes=0,
        _tile_locks={},
    )
    request = _make_tile_request("1", "0", "0", plugin)

    proxy = asyncio.create_task(_handle_tile_proxy(request))
    await asyncio.wait_for(read_started.wait(), timeout=1)
    prefetch = asyncio.create_task(
        _prefetch_one_tile(
            plugin,
            session,
            plugin._tile_upstream,
            1,
            0,
            0,
            1024,
        )
    )
    await asyncio.sleep(0)
    release_read.set()

    proxy_response, prefetch_outcome = await asyncio.gather(proxy, prefetch)

    assert proxy_response.status == 200
    assert prefetch_outcome == "cached"
    assert session.get.call_count == 1
    assert plugin._tile_locks == {}


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
