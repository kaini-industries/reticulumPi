"""Tests for the info_bot plugin."""

import ast
import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from reticulumpi.builtin_plugins.info_bot import _safe_eval


@pytest.fixture
def info_plugin(mock_app, tmp_path):
    """Create an InfoBot plugin instance with mocked dependencies."""
    config = {
        "display_name": "Test Info",
        "storage_path": str(tmp_path / "info_lxmf"),
    }
    mock_bot_identity = MagicMock()

    with (
        patch("reticulumpi.builtin_plugins.info_bot.create_lxm_router") as mock_router_cls,
        patch("reticulumpi.builtin_plugins.info_bot.RNS.Transport.register_announce_handler"),
        patch("reticulumpi.builtin_plugins.info_bot.RNS.Transport.deregister_announce_handler"),
        patch(
            "reticulumpi.builtin_plugins.info_bot.RNS.Identity",
            return_value=mock_bot_identity,
        ),
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
        assert "offline" in result.lower() or "unavailable" in result.lower()

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
        mock_bot_identity = MagicMock()

        with (
            patch("reticulumpi.builtin_plugins.info_bot.create_lxm_router") as mock_router_cls,
            patch("reticulumpi.builtin_plugins.info_bot.RNS.Transport.register_announce_handler"),
            patch("reticulumpi.builtin_plugins.info_bot.RNS.Transport.deregister_announce_handler"),
            patch(
                "reticulumpi.builtin_plugins.info_bot.RNS.Identity",
                return_value=mock_bot_identity,
            ),
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
        mock_bot_identity = MagicMock()

        with (
            patch("reticulumpi.builtin_plugins.info_bot.create_lxm_router") as mock_router_cls,
            patch("reticulumpi.builtin_plugins.info_bot.RNS.Transport.register_announce_handler"),
            patch("reticulumpi.builtin_plugins.info_bot.RNS.Transport.deregister_announce_handler"),
            patch(
                "reticulumpi.builtin_plugins.info_bot.RNS.Identity",
                return_value=mock_bot_identity,
            ),
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


# ── gap-013: _safe_eval edge cases ──────────────────────────────────


class TestSafeEval:
    def test_simple_addition(self):
        tree = ast.parse("2+3", mode="eval")
        assert _safe_eval(tree) == 5

    def test_sqrt_function(self):
        tree = ast.parse("sqrt(16)", mode="eval")
        assert _safe_eval(tree) == 4.0

    def test_exponent_too_large_raises(self):
        tree = ast.parse("2**1001", mode="eval")
        with pytest.raises(ValueError, match="Exponent too large"):
            _safe_eval(tree)

    def test_import_raises(self):
        tree = ast.parse("__import__('os')", mode="eval")
        with pytest.raises(ValueError):
            _safe_eval(tree)

    def test_nested_expression(self):
        tree = ast.parse("(2+3)*4", mode="eval")
        assert _safe_eval(tree) == 20

    def test_constant_pi(self):
        import math

        tree = ast.parse("pi", mode="eval")
        assert _safe_eval(tree) == math.pi

    def test_negative_exponent_large_magnitude(self):
        tree = ast.parse("2**(-1001)", mode="eval")
        with pytest.raises(ValueError, match="Exponent too large"):
            _safe_eval(tree)

    def test_exponent_at_boundary(self):
        tree = ast.parse("2**1000", mode="eval")
        result = _safe_eval(tree)
        assert result == 2**1000


# ── gap-002: Untested info_bot commands ─────────────────────────────


class TestCmdCalc:
    def test_calc_basic(self, info_plugin):
        result = info_plugin._cmd_calc("2+2")
        assert "= 4" in result

    def test_calc_sqrt(self, info_plugin):
        result = info_plugin._cmd_calc("sqrt(144)")
        assert "= 12" in result

    def test_calc_no_args(self, info_plugin):
        result = info_plugin._cmd_calc("")
        assert "Usage" in result

    def test_calc_exponent_too_large(self, info_plugin):
        result = info_plugin._cmd_calc("2**9999")
        assert "Error" in result

    def test_calc_unsafe_expression(self, info_plugin):
        result = info_plugin._cmd_calc("__import__('os')")
        assert "Error" in result


class TestCmdDice:
    def test_dice_default(self, info_plugin):
        result = info_plugin._cmd_dice("")
        assert "Rolling 1d6" in result

    def test_dice_2d6(self, info_plugin):
        result = info_plugin._cmd_dice("2d6")
        assert "Rolling 2d6" in result
        assert "=" in result

    def test_dice_no_d(self, info_plugin):
        result = info_plugin._cmd_dice("123")
        assert "Usage" in result

    def test_dice_bounds(self, info_plugin):
        result = info_plugin._cmd_dice("200d6")
        assert "Limits" in result


class TestCmdFlip:
    def test_flip_returns_heads_or_tails(self, info_plugin):
        result = info_plugin._cmd_flip()
        assert "Heads" in result or "Tails" in result
        assert "Coin flip" in result


class TestCmdFortune:
    def test_fortune_returns_string(self, info_plugin):
        result = info_plugin._cmd_fortune()
        assert isinstance(result, str)
        assert len(result) > 0


class TestCmdGrid:
    def test_grid_to_latlon(self, info_plugin):
        result = info_plugin._cmd_grid("EM10")
        assert "EM10" in result
        assert "N" in result or "E" in result

    def test_latlon_to_grid(self, info_plugin):
        result = info_plugin._cmd_grid("30.27 -97.74")
        assert "->" in result

    def test_grid_no_args(self, info_plugin):
        result = info_plugin._cmd_grid("")
        assert "Usage" in result

    def test_grid_invalid(self, info_plugin):
        result = info_plugin._cmd_grid("123")
        assert "Invalid" in result or "grid" in result.lower()


class TestCmdReach:
    def test_reach_no_network_map(self, info_plugin):
        info_plugin.app.get_plugin.return_value = None
        result = info_plugin._cmd_reach()
        assert "not available" in result.lower()

    def test_reach_no_nodes(self, info_plugin):
        network_map = MagicMock()
        network_map.get_known_nodes.return_value = []
        info_plugin.app.get_plugin.return_value = network_map
        result = info_plugin._cmd_reach()
        assert "No known nodes" in result

    def test_reach_with_nodes(self, info_plugin):
        import time

        network_map = MagicMock()
        network_map.get_known_nodes.return_value = [
            {
                "destination_hash": "<aabbccdd00112233>",
                "app_data": "TestPeer",
                "app_name": "test",
                "last_seen": time.time() - 60,
                "hops": 2,
            }
        ]
        conn_mon = MagicMock()
        conn_mon.get_routing_data.return_value = {"paths": []}
        th = MagicMock()
        th.get_transport_nodes.return_value = []

        def _get_plugin(name):
            return {
                "network_map": network_map,
                "connectivity_monitor": conn_mon,
                "transport_health": th,
            }.get(name)

        info_plugin.app.get_plugin.side_effect = _get_plugin
        with patch(
            "reticulumpi.reachability.score_all_nodes",
            return_value=[
                {
                    "destination_hash": "<aabbccdd00112233>",
                    "app_data": "TestPeer",
                    "score": 80,
                    "label": "High",
                    "hops": 2,
                    "last_seen": time.time() - 60,
                }
            ],
        ):
            result = info_plugin._cmd_reach()
        assert "Reachability" in result


class TestCmdPageauth:
    def test_pageauth_no_plugin(self, info_plugin):
        info_plugin.app.get_plugin.return_value = None
        result = info_plugin._cmd_pageauth()
        assert "not available" in result.lower()

    def test_pageauth_status(self, info_plugin):
        nn = MagicMock()
        nn.get_protected_pages.return_value = ["/protected"]
        nn.get_allowed_identities.return_value = ["aabb"]
        info_plugin.app.get_plugin.return_value = nn
        result = info_plugin._cmd_pageauth("status")
        assert "Page Auth" in result
        assert "1" in result  # 1 protected page

    def test_pageauth_list(self, info_plugin):
        nn = MagicMock()
        nn.get_allowed_identities.return_value = ["aabb", "ccdd"]
        info_plugin.app.get_plugin.return_value = nn
        result = info_plugin._cmd_pageauth("list")
        assert "aabb" in result
        assert "ccdd" in result

    def test_pageauth_add_no_admin(self, info_plugin):
        nn = MagicMock()
        nn.get_allowed_identities.return_value = []
        info_plugin.app.get_plugin.return_value = nn
        info_plugin.config["admin_identities"] = []
        result = info_plugin._cmd_pageauth("add aabbccdd", sender="someone")
        assert "admin" in result.lower()


class TestCmdCrypto:
    def test_crypto_no_args(self, info_plugin):
        result = info_plugin._cmd_crypto("")
        assert "Usage" in result

    def test_crypto_success(self, info_plugin):
        mock_data = {"bitcoin": {"usd": 67000.50, "usd_24h_change": 2.5, "usd_market_cap": 1.3e12}}
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(mock_data)):
            result = info_plugin._cmd_crypto("BTC")
        assert "BTC" in result
        assert "$67,000.50" in result
        assert "+2.50%" in result

    def test_crypto_unknown_symbol(self, info_plugin):
        with patch("urllib.request.urlopen", return_value=_mock_urlopen({})):
            result = info_plugin._cmd_crypto("ZZZZZ")
        assert "Unknown" in result


class TestCmdIss:
    def test_iss_success(self, info_plugin):
        iss_data = {
            "iss_position": {"latitude": "51.5", "longitude": "-0.1"},
            "message": "success",
        }
        crew_data = {"people": [{"name": "A", "craft": "ISS"}]}

        call_count = 0

        def mock_open(req, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_urlopen(iss_data)
            return _mock_urlopen(crew_data)

        with patch("urllib.request.urlopen", side_effect=mock_open):
            result = info_plugin._cmd_iss()
        assert "International Space Station" in result
        assert "51.5" in result
        assert "Crew aboard: 1" in result

    def test_iss_network_error(self, info_plugin):
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            result = info_plugin._cmd_iss()
        assert "unavailable" in result.lower()


class TestCmdSolar:
    def test_solar_success(self, info_plugin):
        kp_data = [
            ["time_tag", "Kp", "Kp_fraction", "a_running", "station_count"],
            ["2026-06-19 12:00:00", "3.33", "3.33", "15", "8"],
        ]
        wind_data = {"Bt": "5.2", "Bz": "-1.3"}

        call_count = 0

        def mock_open(req, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_urlopen(kp_data)
            return _mock_urlopen(wind_data)

        with patch("urllib.request.urlopen", side_effect=mock_open):
            result = info_plugin._cmd_solar()
        assert "Kp index: 3.33" in result
        assert "Unsettled" in result
        assert "Bt: 5.2" in result


class TestCmdDefine:
    def test_define_no_args(self, info_plugin):
        result = info_plugin._cmd_define("")
        assert "Usage" in result

    def test_define_success(self, info_plugin):
        api_data = [
            {
                "word": "test",
                "phonetic": "/tEst/",
                "meanings": [
                    {
                        "partOfSpeech": "noun",
                        "definitions": [{"definition": "A procedure to evaluate something."}],
                    }
                ],
            }
        ]
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(api_data)):
            result = info_plugin._cmd_define("test")
        assert "test" in result
        assert "noun" in result
        assert "procedure" in result

    def test_define_not_found(self, info_plugin):
        error = urllib.error.HTTPError("url", 404, "Not Found", {}, None)
        with patch("urllib.request.urlopen", side_effect=error):
            result = info_plugin._cmd_define("xyznotaword")
        assert "No definition found" in result
        assert error.closed


class TestCmdNews:
    def test_news_offline(self, info_plugin):
        info_plugin._internet_available = False
        with patch.object(
            type(info_plugin), "internet_available", new_callable=lambda: property(lambda s: False)
        ):
            result = info_plugin._cmd_news()
        assert "offline" in result.lower() or "unavailable" in result.lower()


class TestCmdJoke:
    def test_joke_offline_fallback(self, info_plugin):
        with patch.object(
            type(info_plugin), "internet_available", new_callable=lambda: property(lambda s: False)
        ):
            result = info_plugin._cmd_joke()
        assert isinstance(result, str)
        assert len(result) > 0
        assert "\n\n" in result  # setup + punchline format


# ── gap-008: _handle_message with !ping ─────────────────────────────


class TestHandleMessage:
    def test_ping_replies_pong(self, info_plugin):
        """Sending !ping should produce a Pong! reply via LXMF."""
        msg = MagicMock()
        msg.source_hash = b"\x03" * 16
        msg.content_as_string.return_value = "!ping"
        msg.source = MagicMock()

        mock_reply = MagicMock()
        with (
            patch(
                "reticulumpi.builtin_plugins.info_bot.RNS.prettyhexrep",
                return_value="<0303030303030303>",
            ),
            patch(
                "reticulumpi.builtin_plugins.info_bot.LXMF.LXMessage",
                return_value=mock_reply,
            ) as mock_lxm_cls,
        ):
            info_plugin._handle_message(msg)

        # The router's handle_outbound should have been called with the reply
        router = info_plugin.lxmf_router
        router.handle_outbound.assert_called_once_with(mock_reply)
        # The third positional arg to LXMessage is the content string
        mock_lxm_cls.assert_called_once()
        response_text = mock_lxm_cls.call_args[0][2]
        assert "Pong" in response_text
