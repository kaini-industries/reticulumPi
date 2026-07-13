"""Regression tests for security-sensitive dashboard remediation paths.

These tests deliberately exercise failure, cleanup, and compatibility branches
that are easy to miss in end-to-end tests but are part of the public API 1.1
and dashboard lifecycle contracts.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import datetime
import json
import stat
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp.web
import pytest

from reticulumpi.builtin_plugins.web_dashboard import api as dashboard_api
from reticulumpi.builtin_plugins.web_dashboard import api_mesh, api_radio, api_services
from reticulumpi.builtin_plugins.web_dashboard import server as dashboard_server
from reticulumpi.builtin_plugins.web_dashboard.plugin import WebDashboardPlugin
from reticulumpi.builtin_plugins.web_dashboard.ssl_utils import generate_self_signed_cert
from reticulumpi.plugin_base import PluginState


_UNSET = object()


class FakeRequest(dict):
    """Small request double with predictable mapping and body semantics."""

    def __init__(
        self,
        plugin,
        *,
        query: dict[str, str] | None = None,
        match_info: dict[str, str] | None = None,
        token: object = "session-token",
        json_body: object = _UNSET,
        json_error: BaseException | None = None,
        post_body: object = _UNSET,
    ) -> None:
        super().__init__()
        if token is not _UNSET:
            self["token"] = token
        self.app = {"plugin": plugin}
        self.query = query or {}
        self.match_info = match_info or {}
        self.headers: dict[str, str] = {}
        self.cookies: dict[str, str] = {}
        self.remote = "127.0.0.1"
        self.scheme = "http"
        self._json_body = {} if json_body is _UNSET else json_body
        self._json_error = json_error
        self._post_body = {} if post_body is _UNSET else post_body

    async def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._json_body

    async def post(self):
        return self._post_body


class LegacyApp:
    """Compatibility host exposing the pre-lifecycle ``get_plugin`` API."""

    def __init__(self, plugins: dict[str, object] | None = None) -> None:
        self.plugins = plugins or {}

    def get_plugin(self, name: str):
        return self.plugins.get(name)


def _dashboard_owner(plugins: dict[str, object] | None = None, **attributes):
    owner = SimpleNamespace(app=LegacyApp(plugins), config={})
    for name, value in attributes.items():
        setattr(owner, name, value)
    return owner


def _unwrapped(handler):
    while hasattr(handler, "__wrapped__"):
        handler = handler.__wrapped__
    return handler


def _payload(response: aiohttp.web.Response) -> dict:
    return json.loads(response.text)


@pytest.mark.parametrize(
    ("handler_name", "expected_status"),
    [
        ("handle_lora_diagnostics", 200),
        ("handle_alerts", 200),
        ("handle_files", 200),
        ("handle_sensors", 200),
        ("handle_emergency", 200),
        ("handle_nomadnet_auth", 200),
        ("handle_meshtastic_status", 200),
        ("handle_meshtastic_nodes", 200),
        ("handle_meshtastic_device", 200),
        ("handle_meshtastic_lora_neighbors", 200),
        ("handle_meshtastic_channels", 200),
        ("handle_meshcore_status", 200),
        ("handle_meshcore_contacts", 200),
        ("handle_meshcore_device", 200),
        ("handle_meshcore_observer_status", 200),
        ("handle_mesh_bridge_status", 200),
        ("handle_mesh_bridge_running", 503),
        ("handle_messages", 200),
        ("handle_transports", 200),
        ("handle_contacts", 200),
        ("handle_message_stats", 200),
        ("handle_conversations", 200),
        ("handle_mark_read", 503),
        ("handle_unread_counts", 200),
        ("handle_space_snapshot", 200),
        ("handle_gps_snapshot", 200),
        ("handle_gps_status", 200),
        ("handle_gps_satellites", 200),
        ("handle_adsb_snapshot", 200),
        ("handle_ntp_snapshot", 200),
        ("handle_ntp_sources", 200),
        ("handle_link_tester_snapshot", 200),
        ("handle_link_tester_start", 503),
        ("handle_link_tester_stop", 503),
        ("handle_link_tester_clear", 503),
        ("handle_weather_alert", 503),
        ("handle_weather_alert_active", 503),
        ("handle_ais", 503),
        ("handle_acars", 503),
        ("handle_radiosonde", 503),
        ("handle_noaa", 503),
        ("handle_noaa_image", 503),
        ("handle_captive_portal", 200),
    ],
)
def test_service_read_endpoints_report_unready_dependencies(handler_name, expected_status):
    """Every dashboard service handler degrades safely when its provider is absent."""

    handler = _unwrapped(getattr(api_services, handler_name))
    response = asyncio.run(handler(FakeRequest(_dashboard_owner())))

    assert response.status == expected_status
    assert _payload(response)["ok"] is (expected_status < 400)


@pytest.mark.parametrize(
    ("handler_name", "plugin_name", "method_name", "query", "match_info"),
    [
        (
            "handle_sensor_history",
            "sensor_framework",
            "get_sensor_history",
            {"sensor": "temperature", "limit": "-1"},
            {},
        ),
        (
            "handle_messages",
            "messaging_hub",
            "get_messages",
            {"offset": "-1"},
            {},
        ),
        (
            "handle_conversation_messages",
            "messaging_hub",
            "get_conversation_messages",
            {"limit": "-1"},
            {"contact_id": "peer"},
        ),
        (
            "handle_message_search",
            "messaging_hub",
            "search_messages",
            {"q": "distress", "limit": "-1"},
            {},
        ),
    ],
)
def test_service_pagination_rejects_negative_bounds(
    handler_name,
    plugin_name,
    method_name,
    query,
    match_info,
):
    provider = SimpleNamespace(**{method_name: MagicMock()})
    owner = _dashboard_owner({plugin_name: provider})

    response = asyncio.run(
        _unwrapped(getattr(api_services, handler_name))(
            FakeRequest(owner, query=query, match_info=match_info)
        )
    )

    assert response.status == 400
    assert "must be" in _payload(response)["error"]
    getattr(provider, method_name).assert_not_called()


@pytest.mark.parametrize(
    "handler_name",
    [
        "handle_mesh_nodes",
        "handle_mesh_summary",
        "handle_mesh_telemetry",
        "handle_transport",
        "handle_connectivity",
        "handle_routing",
        "handle_path_warming",
        "handle_transport_health",
        "handle_reachability",
    ],
)
def test_mesh_endpoints_return_bounded_empty_payload_when_provider_is_unready(handler_name):
    response = asyncio.run(
        _unwrapped(getattr(api_mesh, handler_name))(FakeRequest(_dashboard_owner()))
    )

    assert response.status == 200
    assert _payload(response)["ok"] is True


def test_mesh_nodes_rejects_invalid_per_page_before_provider_query():
    network_map = SimpleNamespace(
        get_known_nodes=MagicMock(return_value=[]),
        get_known_nodes_paginated=MagicMock(return_value={}),
    )
    owner = _dashboard_owner({"network_map": network_map})

    response = asyncio.run(
        _unwrapped(api_mesh.handle_mesh_nodes)(
            FakeRequest(owner, query={"page": "1", "per_page": "-1"})
        )
    )

    assert response.status == 400
    network_map.get_known_nodes_paginated.assert_not_called()


@pytest.mark.parametrize("query", [{"page": "-1"}, {"max_hops": "-1"}])
def test_routing_rejects_invalid_pagination_and_hop_bounds(query):
    monitor = SimpleNamespace(get_routing_data=MagicMock())
    owner = _dashboard_owner({"connectivity_monitor": monitor})

    response = asyncio.run(api_mesh.handle_routing(FakeRequest(owner, query=query)))

    assert response.status == 400
    monitor.get_routing_data.assert_not_called()


def test_reachability_rejects_negative_limit_after_scoring(monkeypatch):
    network_map = SimpleNamespace(
        get_known_nodes=MagicMock(return_value=[{"destination_hash": "<aa>"}])
    )
    owner = _dashboard_owner({"network_map": network_map})
    monkeypatch.setattr(
        "reticulumpi.reachability.score_all_nodes",
        lambda *_args: [{"destination_hash": "<aa>", "score": 10, "label": "low"}],
    )

    response = asyncio.run(
        _unwrapped(api_mesh.handle_reachability)(FakeRequest(owner, query={"per_page": "-1"}))
    )

    assert response.status == 400


def test_reachability_rejects_negative_page_after_valid_limit(monkeypatch):
    network_map = SimpleNamespace(
        get_known_nodes=MagicMock(return_value=[{"destination_hash": "<aa>"}])
    )
    owner = _dashboard_owner({"network_map": network_map})
    monkeypatch.setattr(
        "reticulumpi.reachability.score_all_nodes",
        lambda *_args: [{"destination_hash": "<aa>", "score": 10, "label": "low"}],
    )

    response = asyncio.run(
        _unwrapped(api_mesh.handle_reachability)(
            FakeRequest(owner, query={"per_page": "50", "page": "-1"})
        )
    )

    assert response.status == 400


def test_reachability_uses_provider_hash_lookup(monkeypatch):
    network_map = SimpleNamespace(
        get_known_nodes=MagicMock(return_value=[]),
        get_nodes_by_hashes=MagicMock(return_value=[{"destination_hash": "<aa>"}]),
    )
    owner = _dashboard_owner({"network_map": network_map})
    monkeypatch.setattr(
        "reticulumpi.reachability.score_all_nodes",
        lambda nodes, *_args: [{**node, "score": 90, "label": "high"} for node in nodes],
    )

    response = asyncio.run(
        _unwrapped(api_mesh.handle_reachability)(FakeRequest(owner, query={"hashes": "aa"}))
    )

    assert response.status == 200
    network_map.get_nodes_by_hashes.assert_called_once_with([b"\xaa"])


def test_paths_enriches_cached_rows_with_ready_provider_data(monkeypatch):
    path = {"hash": "aa", "interface": "RNode", "hops": 1, "timestamp": 100.0}
    network_map = SimpleNamespace(
        get_node_by_hash=MagicMock(
            return_value={
                "app_name": "nomadnetwork",
                "app_data_str": "Field node",
                "aspects": "node",
                "announce_count": 2,
                "first_seen": 1.0,
            }
        )
    )
    connectivity = SimpleNamespace(get_routing_data=MagicMock(return_value={"paths": [dict(path)]}))
    transport = SimpleNamespace(get_transport_nodes=MagicMock(return_value=[]))
    owner = _dashboard_owner(
        {
            "network_map": network_map,
            "connectivity_monitor": connectivity,
            "transport_health": transport,
        }
    )
    monkeypatch.setattr(api_mesh, "_paths_cache", {"data": [path], "time": time.time()})
    monkeypatch.setattr(api_mesh, "_paths_lock", None)
    monkeypatch.setattr(
        "reticulumpi.reachability.score_all_nodes",
        lambda nodes, *_args: [
            {**node, "score": 88, "label": "high", "factors": {}} for node in nodes
        ],
    )

    response = asyncio.run(api_mesh.handle_paths(FakeRequest(owner)))
    result = _payload(response)["data"]

    assert result["paths"][0]["app_name"] == "nomadnetwork"
    assert result["paths"][0]["score"] == 88
    assert result["by_interface"] == {"RNode": 1}


class _AudioResponse:
    def __init__(self) -> None:
        self.prepared = False
        self.writes: list[bytes] = []
        self.chunked = False

    def enable_chunked_encoding(self) -> None:
        self.chunked = True

    async def prepare(self, _request) -> None:
        self.prepared = True

    async def write(self, data: bytes) -> None:
        self.writes.append(data)


def test_radio_audio_reserves_capacity_before_preparing_response(monkeypatch):
    fm = SimpleNamespace(
        is_playing=True,
        output_rate_hz=48_000,
        set_event_loop=MagicMock(),
        register_audio_client=MagicMock(return_value=True),
        unregister_audio_client=MagicMock(),
    )
    owner = _dashboard_owner({"fm_receiver": fm})
    stream = _AudioResponse()

    async def queue_terminator(awaitable, **_kwargs):
        awaitable.close()
        return None

    monkeypatch.setattr(api_radio.aiohttp.web, "StreamResponse", lambda **_kwargs: stream)
    monkeypatch.setattr(api_radio.asyncio, "wait_for", queue_terminator)

    response = asyncio.run(api_radio.handle_radio_audio(FakeRequest(owner)))

    assert response is stream
    assert stream.prepared and stream.chunked
    assert stream.writes[0].startswith(b"RIFF")
    fm.register_audio_client.assert_called_once()
    fm.unregister_audio_client.assert_called_once()


def test_radio_audio_capacity_rejection_does_not_prepare_response(monkeypatch):
    fm = SimpleNamespace(
        is_playing=True,
        output_rate_hz=48_000,
        set_event_loop=MagicMock(),
        register_audio_client=MagicMock(return_value=False),
        unregister_audio_client=MagicMock(),
    )
    owner = _dashboard_owner({"fm_receiver": fm})
    stream_factory = MagicMock()
    monkeypatch.setattr(api_radio.aiohttp.web, "StreamResponse", stream_factory)

    with pytest.raises(aiohttp.web.HTTPServiceUnavailable):
        asyncio.run(api_radio.handle_radio_audio(FakeRequest(owner)))

    stream_factory.assert_not_called()
    fm.unregister_audio_client.assert_not_called()


def test_radio_mutation_and_recording_download_require_session_token():
    owner = _dashboard_owner()
    request = FakeRequest(owner, token=_UNSET)

    assert api_radio._require_auth(request).status == 401
    response = asyncio.run(api_radio.handle_radio_recording_download(request))
    assert response.status == 401


def test_auth_executor_broken_during_work_fails_closed():
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    slots = SimpleNamespace(acquire=MagicMock(return_value=True), release=MagicMock())
    plugin = SimpleNamespace(_auth_executor=executor, _auth_slots=slots)

    def broken_work():
        raise concurrent.futures.BrokenExecutor("worker unavailable")

    try:
        admitted, result = asyncio.run(dashboard_api._run_auth_work(plugin, broken_work))
    finally:
        executor.shutdown(wait=True)

    assert admitted is False
    assert result is None
    slots.release.assert_called_once()


def test_form_login_redirects_when_auth_admission_is_saturated(monkeypatch):
    auth = SimpleNamespace(is_rate_limited=lambda _ip: False, login=MagicMock())
    owner = _dashboard_owner(_auth=auth)
    request = FakeRequest(owner, post_body={"password": "correct-password"})
    monkeypatch.setattr(
        dashboard_api,
        "_run_auth_work",
        AsyncMock(return_value=(False, None)),
    )

    with pytest.raises(aiohttp.web.HTTPFound) as raised:
        asyncio.run(dashboard_api.handle_form_login(request))

    assert raised.value.location == "/login.html?error=busy"


def test_logout_still_invalidates_session_when_socket_close_fails(monkeypatch):
    auth = SimpleNamespace(logout=MagicMock())
    owner = _dashboard_owner(_auth=auth)
    request = FakeRequest(owner)
    request.headers = {"Authorization": "Bearer session-token"}
    monkeypatch.setattr(
        "reticulumpi.builtin_plugins.web_dashboard.websocket_handler.close_websockets_for_token",
        AsyncMock(side_effect=RuntimeError("socket registry unavailable")),
    )

    response = asyncio.run(dashboard_api.handle_logout(request))

    assert response.status == 200
    auth.logout.assert_called_once_with("session-token")


@pytest.mark.parametrize(
    ("request_factory", "expected_error"),
    [
        (
            lambda owner: FakeRequest(owner, json_error=ValueError("invalid")),
            "Invalid request body",
        ),
        (lambda owner: FakeRequest(owner, json_body=[]), "Invalid request body"),
        (
            lambda owner: FakeRequest(owner, json_body={"current_password": 1}),
            "Current and new passwords are required",
        ),
        (
            lambda owner: FakeRequest(
                owner,
                json_body={"current_password": "x" * 257, "new_password": "y"},
            ),
            "Password too long",
        ),
    ],
)
def test_password_change_rejects_malformed_inputs(request_factory, expected_error):
    owner = _dashboard_owner(_auth=SimpleNamespace())

    response = asyncio.run(dashboard_api.handle_change_password(request_factory(owner)))

    assert response.status == 400
    assert _payload(response)["error"] == expected_error


def test_password_change_reports_saturation_and_unknown_persistence_failure(monkeypatch):
    owner = _dashboard_owner(_auth=SimpleNamespace(change_password=MagicMock()))
    request = FakeRequest(
        owner,
        json_body={"current_password": "old-password", "new_password": "new-password-long"},
    )
    work = AsyncMock(
        side_effect=[
            (False, None),
            (
                True,
                SimpleNamespace(
                    applied=False,
                    reason="unexpected_backend_failure",
                ),
            ),
        ]
    )
    monkeypatch.setattr(dashboard_api, "_run_auth_work", work)

    saturated = asyncio.run(dashboard_api.handle_change_password(request))
    failed = asyncio.run(dashboard_api.handle_change_password(request))

    assert saturated.status == 503
    assert failed.status == 500
    assert _payload(failed)["error"] == "Password could not be changed"


def test_plugin_detail_hides_unready_or_missing_plugin():
    response = asyncio.run(
        dashboard_api.handle_plugin_detail(
            FakeRequest(_dashboard_owner(), match_info={"name": "unready"})
        )
    )

    assert response.status == 404


def _restart_request(owner):
    request = FakeRequest(owner)
    request.headers = {"X-Confirm-Password": "confirmed"}
    return request


def test_restart_initializes_bounded_operation_state_and_task_set(monkeypatch):
    owner = _dashboard_owner(_auth=SimpleNamespace(_password_hash="hash"))
    monkeypatch.setattr(dashboard_api, "_last_restart_time", None)
    monkeypatch.setattr(
        dashboard_api,
        "_run_auth_work",
        AsyncMock(return_value=(True, True)),
    )
    monkeypatch.setattr(dashboard_api.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(
        "reticulumpi.control_client.request_control",
        MagicMock(return_value={"ok": True, "operation": "restart_services"}),
    )

    async def run():
        response = await dashboard_api.handle_services_restart(_restart_request(owner))
        await asyncio.gather(*list(owner._restart_tasks))
        return response

    response = asyncio.run(run())
    operation_id = _payload(response)["data"]["operation_id"]

    assert response.status == 202
    assert owner._restart_operations[operation_id]["state"] == "scheduled"
    assert owner._restart_tasks == set()


def test_restart_accepts_low_uptime_first_attempt_then_enforces_cooldown(monkeypatch):
    owner = _dashboard_owner(
        _auth=SimpleNamespace(_password_hash="hash"),
        _restart_operations={},
        _restart_tasks=set(),
    )
    monotonic_values = iter((0.25, 1.0))
    monkeypatch.setattr(dashboard_api, "_last_restart_time", None)
    monkeypatch.setattr(
        dashboard_api,
        "time",
        SimpleNamespace(monotonic=lambda: next(monotonic_values), time=time.time),
    )
    monkeypatch.setattr(
        dashboard_api,
        "_run_auth_work",
        AsyncMock(return_value=(True, True)),
    )
    monkeypatch.setattr(dashboard_api.asyncio, "sleep", AsyncMock())
    broker = MagicMock(return_value={"ok": True, "operation": "restart_services"})
    monkeypatch.setattr("reticulumpi.control_client.request_control", broker)

    async def run():
        first = await dashboard_api.handle_services_restart(_restart_request(owner))
        await asyncio.gather(*list(owner._restart_tasks))
        second = await dashboard_api.handle_services_restart(_restart_request(owner))
        return first, second

    first, second = asyncio.run(run())

    assert first.status == 202
    assert second.status == 429
    assert _payload(second)["error"] == "Service restart already in progress"
    assert dashboard_api._last_restart_time == 0.25
    broker.assert_called_once()


def test_restart_operation_history_evicts_oldest_record(monkeypatch):
    operations = {
        f"old-{index}": {"created_at": float(index), "state": "scheduled"} for index in range(20)
    }
    owner = _dashboard_owner(
        _auth=SimpleNamespace(_password_hash="hash"),
        _restart_operations=operations,
        _restart_tasks=set(),
    )
    monkeypatch.setattr(dashboard_api, "_last_restart_time", None)
    monkeypatch.setattr(dashboard_api.secrets, "token_hex", lambda _size: "new-operation")
    monkeypatch.setattr(
        dashboard_api,
        "_run_auth_work",
        AsyncMock(return_value=(True, True)),
    )
    monkeypatch.setattr(dashboard_api.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(
        "reticulumpi.control_client.request_control",
        MagicMock(return_value={"ok": True}),
    )

    async def run():
        response = await dashboard_api.handle_services_restart(_restart_request(owner))
        await asyncio.gather(*list(owner._restart_tasks))
        return response

    response = asyncio.run(run())

    assert response.status == 202
    assert len(owner._restart_operations) == 20
    assert "old-0" not in owner._restart_operations
    assert "new-operation" in owner._restart_operations


def test_restart_cancellation_is_exposed_in_operation_status(monkeypatch):
    owner = _dashboard_owner(
        _auth=SimpleNamespace(_password_hash="hash"),
        _restart_operations={},
        _restart_tasks=set(),
    )
    monkeypatch.setattr(dashboard_api, "_last_restart_time", None)
    monkeypatch.setattr(
        dashboard_api,
        "_run_auth_work",
        AsyncMock(return_value=(True, True)),
    )
    original_sleep = asyncio.sleep
    blocker: asyncio.Future[None]

    async def run():
        nonlocal blocker
        blocker = asyncio.get_running_loop().create_future()

        async def blocked_sleep(_delay):
            await blocker

        with patch.object(dashboard_api.asyncio, "sleep", blocked_sleep):
            response = await dashboard_api.handle_services_restart(_restart_request(owner))
            operation_id = _payload(response)["data"]["operation_id"]
            task = next(iter(owner._restart_tasks))
            await original_sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return operation_id

    blocker = None  # type: ignore[assignment]
    operation_id = asyncio.run(run())

    assert owner._restart_operations[operation_id]["state"] == "cancelled"


def test_restart_saturation_and_status_lookup(monkeypatch):
    owner = _dashboard_owner(
        _auth=SimpleNamespace(_password_hash="hash"),
        _restart_operations={"known": {"id": "known", "state": "scheduled"}},
    )
    monkeypatch.setattr(
        dashboard_api,
        "_run_auth_work",
        AsyncMock(return_value=(False, None)),
    )

    busy = asyncio.run(dashboard_api.handle_services_restart(_restart_request(owner)))
    found = asyncio.run(
        dashboard_api.handle_services_restart_status(
            FakeRequest(owner, match_info={"operation_id": "known"})
        )
    )
    missing = asyncio.run(
        dashboard_api.handle_services_restart_status(
            FakeRequest(owner, match_info={"operation_id": "missing"})
        )
    )

    assert busy.status == 503
    assert found.status == 200
    assert _payload(found)["data"]["state"] == "scheduled"
    assert missing.status == 404


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"ws_session_revalidate_interval": 4}, "ws_session_revalidate_interval"),
        ({"startup_timeout": 0}, "startup_timeout"),
        ({"local_api": []}, "local_api must be a dict"),
        ({"local_api": {"enabled": "yes"}}, "local_api.enabled"),
        ({"local_api": {"token_file": 5}}, "local_api.token_file"),
        ({"ssl": {"enabled": "yes"}}, "ssl.enabled"),
        ({"ssl": {"auto_generate": "yes"}}, "ssl.auto_generate"),
        ({"ssl": {"cert_file": 5}}, "ssl.cert_file"),
        ({"ssl": {"cert_file": "cert", "key_file": 5}}, "ssl.key_file"),
        ({"reverse_proxy": []}, "reverse_proxy must be a dict"),
        ({"reverse_proxy": {"enabled": "yes"}}, "reverse_proxy.enabled"),
        (
            {"reverse_proxy": {"enabled": True, "trusted_networks": []}},
            "trusted_networks cannot be empty",
        ),
        (
            {"reverse_proxy": {"enabled": True, "trusted_networks": ["invalid"]}},
            "not a valid CIDR",
        ),
        ({"tile_proxy": {"enabled": True, "max_tile_kb": 8}}, "max_tile_kb"),
        ({"tile_proxy": {"enabled": True, "prefetch": []}}, "prefetch must be a dict"),
        (
            {"tile_proxy": {"enabled": True, "prefetch": {"min_zoom": 10, "max_zoom": 5}}},
            "prefetch zooms",
        ),
    ],
)
def test_dashboard_rejects_invalid_security_and_capacity_config(mock_app, config, message):
    with pytest.raises(ValueError, match=message):
        WebDashboardPlugin(mock_app, {"enabled": True, "password": "test", **config})


def test_start_migrates_legacy_local_api_to_rotated_token(mock_app, tmp_path, monkeypatch):
    plugin = WebDashboardPlugin(
        mock_app,
        {
            "enabled": True,
            "password": "test",
            "secret_dir": str(tmp_path),
            "allow_localhost_api": True,
            "allow_localhost_send": True,
        },
    )
    monkeypatch.setattr(plugin, "_setup_ssl", lambda: None)
    monkeypatch.setattr(plugin, "_start_thread", lambda *_args: plugin._server_ready.set())
    monkeypatch.setattr(
        "reticulumpi.builtin_plugins.web_dashboard.server.create_app",
        lambda _plugin: MagicMock(),
    )
    monkeypatch.setattr("shutil.which", lambda _name: None)

    plugin.start()
    try:
        token_file = Path(plugin._local_api_token_path)
        assert plugin.config["local_api"]["enabled"] is True
        assert plugin._local_api_token == token_file.read_text().strip()
        assert token_file.stat().st_mode & 0o777 == 0o600
    finally:
        plugin.stop()


def test_start_timeout_aborts_and_releases_auth_executor(mock_app, tmp_path, monkeypatch):
    plugin = WebDashboardPlugin(
        mock_app,
        {
            "enabled": True,
            "password": "test",
            "secret_dir": str(tmp_path),
            "startup_timeout": 1,
        },
    )
    monkeypatch.setattr(plugin, "_setup_ssl", lambda: None)
    monkeypatch.setattr(plugin, "_start_thread", MagicMock())
    monkeypatch.setattr(
        "reticulumpi.builtin_plugins.web_dashboard.server.create_app",
        lambda _plugin: MagicMock(),
    )
    monkeypatch.setattr("threading.Event.wait", lambda *_args, **_kwargs: False)

    with pytest.raises(RuntimeError, match="did not become ready"):
        plugin.start()

    assert plugin._active is False
    assert plugin._auth_executor is None


def test_stop_escalates_stuck_mdns_publisher_to_sigkill(mock_app):
    plugin = WebDashboardPlugin(mock_app, {"enabled": True, "password": "test"})
    publisher = MagicMock()
    publisher.wait.side_effect = [
        subprocess.TimeoutExpired("avahi", 5),
        subprocess.TimeoutExpired("avahi", 2),
    ]
    plugin._mdns_proc = publisher
    plugin._loop = None
    plugin._auth_executor = None
    plugin._join_threads = MagicMock()

    plugin.stop()

    publisher.terminate.assert_called_once()
    publisher.kill.assert_called_once()
    assert publisher.wait.call_count == 2
    assert plugin._mdns_proc is None


def test_server_thread_reports_bind_failure_and_signals_waiter(mock_app, monkeypatch):
    plugin = WebDashboardPlugin(mock_app, {"enabled": True, "password": "test"})
    plugin._server_ready = MagicMock()
    plugin._active = True
    monkeypatch.setattr(plugin, "_set_dashboard_readiness", MagicMock())
    monkeypatch.setattr(
        plugin,
        "_start_server",
        AsyncMock(side_effect=OSError("address already in use")),
    )

    plugin._run_server()

    assert isinstance(plugin._server_error, OSError)
    assert plugin._active is False
    plugin._set_dashboard_readiness.assert_called_with(False)
    plugin._server_ready.set.assert_called_once()


def test_start_server_initializes_tile_budget_tls_task_and_readiness(
    mock_app,
    tmp_path,
    monkeypatch,
):
    cache_dir = tmp_path / "tiles"
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir()
    monkeypatch.setenv("RETICULUMPI_RUNTIME_DIR", str(runtime_dir))
    tile_dir = cache_dir / "1" / "2"
    tile_dir.mkdir(parents=True)
    (tile_dir / "3.png").write_bytes(b"png")
    (tile_dir / "stale.tmp").write_bytes(b"partial")
    plugin = WebDashboardPlugin(
        mock_app,
        {
            "enabled": True,
            "password": "test",
            "tile_proxy": {
                "enabled": True,
                "cache_dir": str(cache_dir),
                "max_cache_mb": 10,
                "max_tile_kb": 64,
            },
        },
    )
    plugin._aiohttp_app = MagicMock()
    plugin._host = "127.0.0.1"
    plugin._port = 8080
    plugin._ssl_ctx = object()
    plugin._server_ready = MagicMock()
    runner = SimpleNamespace(setup=AsyncMock())
    site = SimpleNamespace(start=AsyncMock())
    tile_session = SimpleNamespace(close=AsyncMock())
    prefetch_session = SimpleNamespace(close=AsyncMock())
    session_gc_task = MagicMock()
    tls_task = MagicMock()
    monkeypatch.setattr(aiohttp.web, "AppRunner", MagicMock(return_value=runner))
    monkeypatch.setattr(aiohttp.web, "TCPSite", MagicMock(return_value=site))
    monkeypatch.setattr(
        "aiohttp.ClientSession",
        MagicMock(side_effect=[tile_session, prefetch_session]),
    )

    def fake_ensure_future(coroutine):
        coroutine.close()
        return session_gc_task

    def fake_create_task(coroutine, **_kwargs):
        coroutine.close()
        return tls_task

    monkeypatch.setattr(asyncio, "ensure_future", fake_ensure_future)
    monkeypatch.setattr(asyncio, "create_task", fake_create_task)

    asyncio.run(plugin._start_server())

    runner.setup.assert_awaited_once()
    site.start.assert_awaited_once()
    assert plugin._tile_max_tile_bytes == 64 * 1024
    assert plugin._tile_cache_bytes == 3
    assert plugin._tile_locks == {}
    assert not (tile_dir / "stale.tmp").exists()
    assert plugin._session_gc_task is session_gc_task
    assert plugin._tls_maintenance_task is tls_task
    plugin._server_ready.set.assert_called_once()
    marker = runtime_dir / "dashboard-ready"
    assert marker.read_text(encoding="ascii") == "ready\n"
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600


def test_shutdown_cancels_managed_tasks_and_closes_clients(mock_app):
    plugin = WebDashboardPlugin(mock_app, {"enabled": True, "password": "test"})

    async def run():
        blocker = asyncio.Event()

        async def wait_forever():
            await blocker.wait()

        restart_task = asyncio.create_task(wait_forever())
        session_task = asyncio.create_task(wait_forever())
        tls_task = asyncio.create_task(wait_forever())
        prefetch_task = asyncio.create_task(wait_forever())
        plugin._restart_tasks = {restart_task}
        plugin._session_gc_task = session_task
        plugin._tls_maintenance_task = tls_task
        plugin._prefetch_task = prefetch_task
        plugin._prefetch_session = SimpleNamespace(close=AsyncMock())
        plugin._tile_session = SimpleNamespace(close=AsyncMock())
        plugin._tile_locks = {"tile": object()}
        plugin._runner = SimpleNamespace(cleanup=AsyncMock())
        plugin._site = object()

        await plugin._shutdown()

        assert plugin._restart_tasks == set()
        assert plugin._tls_maintenance_task is None
        assert plugin._prefetch_session is None
        assert plugin._tile_session is None
        assert plugin._tile_locks == {}
        assert plugin._site is None
        plugin._runner.cleanup.assert_awaited_once()

    asyncio.run(run())


def test_setup_ssl_builds_managed_and_operator_contexts(mock_app, tmp_path):
    pytest.importorskip("cryptography")
    mock_app.config.node_name = "TestNode"
    managed = WebDashboardPlugin(
        mock_app,
        {
            "enabled": True,
            "password": "test",
            "ssl": {
                "enabled": True,
                "auto_generate": True,
                "cert_dir": str(tmp_path / "managed"),
                "extra_hostnames": ["pi.test"],
            },
        },
    )

    managed_context = managed._setup_ssl()

    assert managed_context is not None
    assert managed._tls_managed is True
    assert managed._tls_cert_file == managed._tls_key_file
    assert managed._tls_state == "valid"

    operator = WebDashboardPlugin(
        mock_app,
        {
            "enabled": True,
            "password": "test",
            "ssl": {
                "enabled": True,
                "cert_file": managed._tls_cert_file,
                "key_file": managed._tls_key_file,
                "extra_hostnames": ["pi.test"],
            },
        },
    )

    operator_context = operator._setup_ssl()

    assert operator_context is not None
    assert operator._tls_managed is False
    assert operator._tls_required_sans == ["pi.test"]


def test_setup_ssl_rejects_incomplete_pair_if_config_changes_after_validation(mock_app):
    plugin = WebDashboardPlugin(mock_app, {"enabled": True, "password": "test"})
    plugin.config["ssl"] = {"enabled": True, "cert_file": "operator.pem"}

    with pytest.raises(ValueError, match="must both be provided"):
        plugin._setup_ssl()


def test_operator_tls_daily_check_records_valid_state(mock_app, tmp_path):
    pytest.importorskip("cryptography")
    mock_app.config.node_name = "TestNode"
    bundle, _ = generate_self_signed_cert(str(tmp_path), "TestNode", extra_sans=["localhost"])
    plugin = WebDashboardPlugin(
        mock_app,
        {
            "enabled": True,
            "password": "test",
            "ssl": {
                "enabled": True,
                "cert_file": bundle,
                "key_file": bundle,
                "extra_hostnames": ["localhost"],
            },
        },
    )
    plugin._ssl_ctx = plugin._setup_ssl()

    renewed = asyncio.run(plugin._check_tls_certificate())

    assert renewed is False
    assert plugin._tls_state == "valid"
    assert plugin._tls_last_check is not None


def test_tls_maintenance_records_unexpected_failure_then_stops(mock_app, monkeypatch):
    plugin = WebDashboardPlugin(mock_app, {"enabled": True, "password": "test"})
    plugin._active = True
    checks = AsyncMock(side_effect=RuntimeError("certificate backend failed"))
    monkeypatch.setattr(plugin, "_check_tls_certificate", checks)
    sleeps = 0

    async def fake_sleep(_delay):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            plugin._active = False

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    asyncio.run(plugin._tls_maintenance_loop())

    checks.assert_awaited_once()
    assert plugin._tls_state == "degraded"


def test_tls_maintenance_propagates_cancellation(mock_app, monkeypatch):
    plugin = WebDashboardPlugin(mock_app, {"enabled": True, "password": "test"})
    plugin._active = True
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(
        plugin,
        "_check_tls_certificate",
        AsyncMock(side_effect=asyncio.CancelledError()),
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(plugin._tls_maintenance_loop())


def test_tls_check_short_circuits_disabled_and_failed_closed_states(mock_app):
    plugin = WebDashboardPlugin(mock_app, {"enabled": True, "password": "test"})
    plugin._ssl_ctx = None
    plugin._tls_cert_file = None
    plugin._tls_key_file = None
    plugin._tls_failed_closed = False

    assert asyncio.run(plugin._check_tls_certificate()) is False

    plugin._ssl_ctx = object()
    plugin._tls_cert_file = "cert"
    plugin._tls_key_file = "key"
    plugin._tls_failed_closed = True
    assert asyncio.run(plugin._check_tls_certificate()) is False


def test_non_atomic_managed_tls_layout_fails_closed(mock_app):
    plugin = WebDashboardPlugin(mock_app, {"enabled": True, "password": "test"})
    plugin._tls_cert_file = "cert.pem"
    plugin._tls_key_file = "key.pem"
    plugin._site = SimpleNamespace(stop=AsyncMock())
    plugin._runner = None
    plugin._tls_degraded = False
    plugin._tls_failed_closed = False

    renewed = asyncio.run(
        plugin._renew_managed_tls(
            datetime.datetime.now(datetime.timezone.utc),
            "certificate expires soon",
        )
    )

    assert renewed is False
    assert plugin._tls_state == "failed_closed"
    assert plugin._site is None


def test_managed_tls_renewal_handles_missing_previous_bundle(mock_app, tmp_path, monkeypatch):
    plugin = WebDashboardPlugin(mock_app, {"enabled": True, "password": "test"})
    missing_bundle = str(tmp_path / "missing.pem")
    plugin._tls_cert_file = missing_bundle
    plugin._tls_key_file = missing_bundle
    plugin._tls_cert_dir = str(tmp_path / "generated")
    plugin._tls_common_name = "TestNode"
    plugin._tls_extra_hostnames = []
    plugin._tls_required_sans = []
    plugin._tls_degraded = False
    plugin._tls_last_error = None
    plugin._ssl_ctx = MagicMock()
    plugin._site = SimpleNamespace(stop=AsyncMock())
    plugin._runner = None
    monkeypatch.setattr(plugin, "_build_ssl_context", MagicMock(return_value=object()))

    renewed = asyncio.run(
        plugin._renew_managed_tls(
            datetime.datetime.now(datetime.timezone.utc),
            "missing previous bundle",
        )
    )

    assert renewed is True
    assert plugin._tls_state == "renewed"


def test_managed_tls_restore_failure_forces_listener_closed(mock_app, tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    issued_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    bundle, _ = generate_self_signed_cert(str(tmp_path / "managed"), "TestNode", now=issued_at)
    plugin = WebDashboardPlugin(mock_app, {"enabled": True, "password": "test"})
    plugin._tls_cert_file = bundle
    plugin._tls_key_file = bundle
    plugin._tls_cert_dir = str(tmp_path / "managed")
    plugin._tls_common_name = "TestNode"
    plugin._tls_extra_hostnames = []
    plugin._tls_required_sans = []
    plugin._ssl_ctx = MagicMock()
    plugin._ssl_ctx.load_cert_chain.side_effect = [
        OSError("live reload failed"),
        OSError("restore reload failed"),
    ]
    plugin._site = SimpleNamespace(stop=AsyncMock())
    plugin._runner = None
    monkeypatch.setattr(plugin, "_build_ssl_context", MagicMock(return_value=object()))

    renewed = asyncio.run(
        plugin._renew_managed_tls(
            issued_at + datetime.timedelta(days=335),
            "certificate expires soon",
        )
    )

    assert renewed is False
    assert plugin._tls_state == "failed_closed"
    assert plugin._site is None


def test_recovering_tls_marks_ready_again(mock_app, monkeypatch):
    plugin = WebDashboardPlugin(mock_app, {"enabled": True, "password": "test"})
    plugin._active = True
    plugin._plugin_state = PluginState.READY
    plugin._tls_degraded = True
    plugin._tls_last_error = "temporary failure"
    ready = MagicMock()
    monkeypatch.setattr(plugin, "mark_ready", ready)

    plugin._record_tls_valid("valid")

    assert plugin._tls_degraded is False
    assert plugin._tls_last_error is None
    ready.assert_called_once()


def test_fail_closed_tls_cleans_listener_even_when_cleanup_raises(mock_app):
    plugin = WebDashboardPlugin(mock_app, {"enabled": True, "password": "test"})
    plugin._site = SimpleNamespace(stop=AsyncMock(side_effect=RuntimeError("stop failed")))
    plugin._runner = SimpleNamespace(cleanup=AsyncMock(side_effect=RuntimeError("cleanup failed")))

    asyncio.run(plugin._fail_closed_tls_listener("invalid certificate"))

    assert plugin._site is None
    assert plugin._runner is None
    assert plugin._tls_failed_closed is True


def test_local_api_token_falls_back_to_secret_dir_when_runtime_missing(
    mock_app,
    tmp_path,
    monkeypatch,
):
    plugin = WebDashboardPlugin(
        mock_app,
        {"enabled": True, "password": "test", "secret_dir": str(tmp_path)},
    )
    missing_runtime = tmp_path / "missing-run"
    monkeypatch.setenv("RETICULUMPI_RUNTIME_DIR", str(missing_runtime))

    token = plugin._load_or_create_local_api_token({"enabled": True})

    token_path = tmp_path / "local_api.token"
    assert token == token_path.read_text().strip()
    assert token_path.stat().st_mode & 0o777 == 0o600


def test_tile_lock_acquire_cancellation_drops_registry_entry(monkeypatch):
    locks: dict[str, object] = {}
    lease = dashboard_server._TileLockLease(locks, "tile.png")
    monkeypatch.setattr(
        lease._entry.lock,
        "acquire",
        AsyncMock(side_effect=asyncio.CancelledError()),
    )

    async def run():
        async with lease:
            raise AssertionError("unreachable")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())

    assert locks == {}


def test_static_shell_handlers_fail_closed_for_missing_resources(monkeypatch):
    request = FakeRequest(_dashboard_owner(), match_info={"asset_path": "missing.js"})
    monkeypatch.setattr(
        dashboard_server,
        "read_static_bytes",
        MagicMock(side_effect=FileNotFoundError),
    )

    with pytest.raises(aiohttp.web.HTTPNotFound):
        asyncio.run(dashboard_server._serve_static(request))

    spectrum = asyncio.run(dashboard_server._serve_spectrum(FakeRequest(_dashboard_owner())))
    assert spectrum.status == 200
    assert spectrum.content_type == "text/html"


def test_service_worker_rejects_packaged_shell_without_build_marker(monkeypatch):
    owner = _dashboard_owner()
    owner.config = {"tile_cache_entries": 100}
    monkeypatch.setattr(dashboard_server, "read_static_text", lambda _path: "invalid shell")

    with pytest.raises(aiohttp.web.HTTPInternalServerError):
        asyncio.run(dashboard_server._serve_sw(FakeRequest(owner)))


def test_index_redirects_until_bootstrap_password_is_changed():
    owner = _dashboard_owner(
        _auth=SimpleNamespace(password_change_required=True),
    )

    with pytest.raises(aiohttp.web.HTTPFound) as raised:
        asyncio.run(dashboard_server._serve_index(FakeRequest(owner)))

    assert raised.value.location == "/?password_change=required"


def _tile_owner(tmp_path, response):
    response_context = MagicMock()
    response_context.__aenter__ = AsyncMock(return_value=response)
    response_context.__aexit__ = AsyncMock(return_value=False)
    session = SimpleNamespace(get=MagicMock(return_value=response_context))
    return _dashboard_owner(
        _tile_session=session,
        _tile_cache_dir=str(tmp_path),
        _tile_locks={},
        _tile_upstream="https://tiles/{z}/{x}/{y}.png",
        _tile_max_tile_bytes=16,
        _tile_max_bytes=0,
        _tile_cache_bytes=0,
    )


def test_tile_proxy_uses_file_created_while_waiting(tmp_path, monkeypatch):
    owner = _tile_owner(tmp_path, MagicMock())
    file_response = MagicMock()
    monkeypatch.setattr(dashboard_server.os.path, "isfile", MagicMock(side_effect=[False, True]))
    monkeypatch.setattr(
        dashboard_server.aiohttp.web,
        "FileResponse",
        MagicMock(return_value=file_response),
    )

    response = asyncio.run(
        dashboard_server._handle_tile_proxy(
            FakeRequest(owner, match_info={"z": "1", "x": "0", "y": "0"})
        )
    )

    assert response is file_response
    owner._tile_session.get.assert_not_called()
    assert owner._tile_locks == {}


@pytest.mark.parametrize(
    ("streamed", "fallback", "message"),
    [
        (object(), b"x" * 17, "exceeds configured size"),
        (object(), b"not-a-png", "invalid tile content"),
    ],
)
def test_tile_proxy_enforces_cap_and_signature_on_fallback_reads(
    tmp_path,
    streamed,
    fallback,
    message,
):
    upstream = SimpleNamespace(
        status=200,
        headers={"Content-Type": "image/png"},
        content=SimpleNamespace(read=AsyncMock(return_value=streamed)),
        read=AsyncMock(return_value=fallback),
    )
    owner = _tile_owner(tmp_path, upstream)

    with pytest.raises(aiohttp.web.HTTPBadGateway) as raised:
        asyncio.run(
            dashboard_server._handle_tile_proxy(
                FakeRequest(owner, match_info={"z": "1", "x": "0", "y": "0"})
            )
        )

    assert message in raised.value.text
    upstream.read.assert_awaited_once()
    assert owner._tile_locks == {}
