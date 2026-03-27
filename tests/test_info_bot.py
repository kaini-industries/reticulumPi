"""Tests for the info_bot plugin."""

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest
import RNS


@pytest.fixture
def info_plugin(mock_app, tmp_path):
    """Create an InfoBot plugin instance with mocked dependencies."""
    config = {
        "display_name": "Test Info",
        "storage_path": str(tmp_path / "info_lxmf"),
    }
    import RNS as _RNS

    mock_bot_identity = MagicMock()

    with (
        patch("LXMF.LXMRouter") as mock_router_cls,
        patch.object(_RNS.Transport, "register_announce_handler"),
        patch.object(_RNS.Transport, "deregister_announce_handler"),
        patch("RNS.Identity", return_value=mock_bot_identity),
    ):
        mock_router = MagicMock()
        mock_dest = MagicMock()
        mock_dest.hash = b"\x02" * 16
        mock_router.register_delivery_identity.return_value = mock_dest
        mock_router_cls.return_value = mock_router

        from reticulumpi.builtin_plugins.info_bot import InfoBot

        plugin = InfoBot(mock_app, config)
        plugin.start()
        yield plugin
        plugin.stop()


def _mock_urlopen(json_data):
    """Create a mock context manager that returns JSON data."""
    resp = io.BytesIO(json.dumps(json_data).encode("utf-8"))
    resp.read = resp.read  # Already has read()
    mock_cm = MagicMock()
    mock_cm.__enter__ = MagicMock(return_value=resp)
    mock_cm.__exit__ = MagicMock(return_value=False)
    return mock_cm


# ── Help command ─────────────────────────────────────────────────


class TestCmdHelp:
    def test_help_lists_weather(self, info_plugin):
        result = info_plugin._cmd_help()
        assert "!weather" in result
        assert "!help" in result

    def test_help_includes_node_name(self, info_plugin):
        result = info_plugin._cmd_help()
        assert "TestNode" in result


# ── Command routing ──────────────────────────────────────────────


class TestRouteCommand:
    def test_weather_command_routed(self, info_plugin):
        """!weather should route to weather handler, not help."""
        call_count = 0

        def mock_open(req, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_urlopen(_GEOCODE_RESPONSE)
            return _mock_urlopen(_WEATHER_RESPONSE)

        with patch("urllib.request.urlopen", side_effect=mock_open):
            result = info_plugin._route_command("!weather London")
        # Should contain weather data, not help text
        assert "59.0°F" in result
        assert "Available commands" not in result

    def test_help_command_routed(self, info_plugin):
        result = info_plugin._route_command("!help")
        assert "!weather" in result

    def test_no_prefix_returns_help(self, info_plugin):
        result = info_plugin._route_command("hello there")
        assert "!weather" in result

    def test_unknown_command_returns_help(self, info_plugin):
        result = info_plugin._route_command("!unknown")
        assert "Unknown command: !unknown" in result
        assert "!weather" in result

    def test_empty_after_prefix_returns_help(self, info_plugin):
        result = info_plugin._route_command("!")
        assert "!weather" in result

    def test_command_case_insensitive(self, info_plugin):
        """!WEATHER should route the same as !weather."""
        call_count = 0

        def mock_open(req, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_urlopen(_GEOCODE_RESPONSE)
            return _mock_urlopen(_WEATHER_RESPONSE)

        with patch("urllib.request.urlopen", side_effect=mock_open):
            result = info_plugin._route_command("!WEATHER London")
        assert "59.0°F" in result


# ── Weather command ──────────────────────────────────────────────


_GEOCODE_RESPONSE = {
    "results": [
        {
            "name": "London",
            "latitude": 51.5085,
            "longitude": -0.1257,
            "country": "United Kingdom",
            "admin1": "England",
        }
    ]
}

_WEATHER_RESPONSE = {
    "current": {
        "temperature_2m": 59.0,
        "relative_humidity_2m": 72,
        "wind_speed_10m": 8.5,
        "weather_code": 2,
    }
}


class TestCmdWeather:
    def test_weather_success(self, info_plugin):
        """Successful weather lookup returns formatted data."""
        call_count = 0

        def mock_open(req, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_urlopen(_GEOCODE_RESPONSE)
            return _mock_urlopen(_WEATHER_RESPONSE)

        with patch("urllib.request.urlopen", side_effect=mock_open):
            result = info_plugin._cmd_weather("London")

        assert "London" in result
        assert "England" in result
        assert "United Kingdom" in result
        assert "59.0°F" in result
        assert "72%" in result
        assert "8.5 mph" in result
        assert "Partly cloudy" in result

    def test_weather_city_state_filter(self, info_plugin):
        """'Madison, WI' should filter results to Wisconsin."""
        geo_multi = {
            "results": [
                {
                    "name": "Madison",
                    "latitude": 38.7,
                    "longitude": -85.4,
                    "country": "United States",
                    "country_code": "US",
                    "admin1": "Indiana",
                },
                {
                    "name": "Madison",
                    "latitude": 43.07,
                    "longitude": -89.40,
                    "country": "United States",
                    "country_code": "US",
                    "admin1": "Wisconsin",
                },
            ]
        }
        call_count = 0

        def mock_open(req, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_urlopen(geo_multi)
            return _mock_urlopen(_WEATHER_RESPONSE)

        with patch("urllib.request.urlopen", side_effect=mock_open):
            result = info_plugin._cmd_weather("Madison, WI")
        assert "Wisconsin" in result
        assert "Indiana" not in result

    def test_weather_no_args(self, info_plugin):
        result = info_plugin._cmd_weather("")
        assert "Usage" in result

    def test_weather_location_not_found(self, info_plugin):
        empty_geo = {"results": None}
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(empty_geo)):
            result = info_plugin._cmd_weather("xyznotaplace")
        assert "Location not found" in result
        assert "xyznotaplace" in result

    def test_weather_empty_results_list(self, info_plugin):
        empty_geo = {"results": []}
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(empty_geo)):
            result = info_plugin._cmd_weather("nowhere")
        assert "Location not found" in result

    def test_weather_network_error(self, info_plugin):
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            result = info_plugin._cmd_weather("London")
        assert "network error" in result.lower()

    def test_weather_malformed_response(self, info_plugin):
        """Malformed JSON should return a parse error, not crash."""
        call_count = 0

        def mock_open(req, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_urlopen(_GEOCODE_RESPONSE)
            # Return weather data missing expected keys
            return _mock_urlopen({"unexpected": "data"})

        with patch("urllib.request.urlopen", side_effect=mock_open):
            result = info_plugin._cmd_weather("London")
        # Should handle gracefully — either show partial data or error
        assert isinstance(result, str)


# ── Display name ─────────────────────────────────────────────────


class TestDisplayName:
    def test_uses_config_display_name(self, mock_app, tmp_path):
        config = {"display_name": "Custom Name", "storage_path": str(tmp_path / "lxmf")}
        import RNS as _RNS

        mock_bot_identity = MagicMock()

        with (
            patch("LXMF.LXMRouter") as mock_router_cls,
            patch.object(_RNS.Transport, "register_announce_handler"),
            patch.object(_RNS.Transport, "deregister_announce_handler"),
            patch("RNS.Identity", return_value=mock_bot_identity),
        ):
            mock_router = MagicMock()
            mock_dest = MagicMock()
            mock_dest.hash = b"\x02" * 16
            mock_router.register_delivery_identity.return_value = mock_dest
            mock_router_cls.return_value = mock_router

            from reticulumpi.builtin_plugins.info_bot import InfoBot

            plugin = InfoBot(mock_app, config)
            plugin.start()

            mock_router.register_delivery_identity.assert_called_once_with(
                mock_bot_identity, display_name="Custom Name"
            )
            plugin.stop()

    def test_defaults_to_node_name_info(self, mock_app, tmp_path):
        config = {"storage_path": str(tmp_path / "lxmf")}
        import RNS as _RNS

        mock_bot_identity = MagicMock()

        with (
            patch("LXMF.LXMRouter") as mock_router_cls,
            patch.object(_RNS.Transport, "register_announce_handler"),
            patch.object(_RNS.Transport, "deregister_announce_handler"),
            patch("RNS.Identity", return_value=mock_bot_identity),
        ):
            mock_router = MagicMock()
            mock_dest = MagicMock()
            mock_dest.hash = b"\x02" * 16
            mock_router.register_delivery_identity.return_value = mock_dest
            mock_router_cls.return_value = mock_router

            from reticulumpi.builtin_plugins.info_bot import InfoBot

            plugin = InfoBot(mock_app, config)
            plugin.start()

            mock_router.register_delivery_identity.assert_called_once_with(
                mock_bot_identity, display_name="TestNode Info"
            )
            plugin.stop()


# ── Fetch JSON helper ────────────────────────────────────────────


class TestFetchJson:
    def test_parses_json(self, info_plugin):
        data = {"key": "value"}
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(data)):
            result = info_plugin._fetch_json("https://example.com/api")
        assert result == data
