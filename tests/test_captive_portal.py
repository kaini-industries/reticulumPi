"""Tests for the captive_portal plugin."""

from __future__ import annotations

import subprocess
import threading
from unittest.mock import MagicMock, patch

import pytest

from reticulumpi.builtin_plugins.captive_portal import CaptivePortalPlugin, _SPLASH_TEMPLATE


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def _mock_hostapd(tmp_path):
    """Write a temporary hostapd.conf and patch the default path."""
    conf = tmp_path / "hostapd.conf"
    conf.write_text(
        "interface=wlan0\nssid=TestNet\nwpa=2\nwpa_key_mgmt=WPA-PSK\n"
    )
    with patch(
        "reticulumpi.builtin_plugins.captive_portal.Path"
    ) as mock_path_cls:
        real_path = __import__("pathlib").Path

        def _side_effect(p):
            if p == "/etc/hostapd/hostapd.conf":
                return conf
            return real_path(p)

        mock_path_cls.side_effect = _side_effect
        mock_path_cls.return_value = real_path()
        yield conf


def _make_plugin(mock_app, config=None, internet=False):
    """Create a CaptivePortalPlugin with subprocess calls mocked out."""
    probe = MagicMock()
    probe.is_online = internet
    mock_app.internet_probe = probe

    hm = MagicMock()
    hm._iface = "wlan0"
    hm.broadcast_snapshot.return_value = {
        "ssid": "TestNet",
        "ip": "10.0.0.1",
    }

    wd = MagicMock()
    wd._port = 8080
    wd._ssl_context = None

    def _get_plugin(name):
        return {"hotspot_monitor": hm, "web_dashboard": wd}.get(name)

    mock_app.get_plugin = MagicMock(side_effect=_get_plugin)

    cfg = {"mode": "auto", "portal_port": 9999, **(config or {})}

    with (
        patch("reticulumpi.builtin_plugins.captive_portal.subprocess") as mock_sp,
        patch.object(CaptivePortalPlugin, "_start_http_server"),
    ):
        mock_sp.run.return_value = MagicMock(
            returncode=0,
            stdout="chain_active=false\ndns_active=false\nstate_file=false\n",
            stderr="",
        )
        plugin = CaptivePortalPlugin(mock_app, cfg)
        plugin._helper = "/fake/helper.sh"
        plugin.start()
    return plugin, mock_sp


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

class TestValidateConfig:
    def test_default_config(self, mock_app):
        mock_app.internet_probe = None
        plugin = CaptivePortalPlugin(mock_app, {})
        assert plugin.config.get("mode", "auto") == "auto"

    def test_valid_modes(self, mock_app):
        mock_app.internet_probe = None
        for mode in ("auto", "always", "off"):
            CaptivePortalPlugin(mock_app, {"mode": mode})

    def test_invalid_mode_rejects(self, mock_app):
        mock_app.internet_probe = None
        with pytest.raises(ValueError, match="mode"):
            CaptivePortalPlugin(mock_app, {"mode": "bogus"})

    def test_portal_port_range(self, mock_app):
        mock_app.internet_probe = None
        with pytest.raises(ValueError, match="portal_port"):
            CaptivePortalPlugin(mock_app, {"portal_port": 80})
        with pytest.raises(ValueError, match="portal_port"):
            CaptivePortalPlugin(mock_app, {"portal_port": 99999})

    def test_valid_port(self, mock_app):
        mock_app.internet_probe = None
        CaptivePortalPlugin(mock_app, {"portal_port": 8081})


# ---------------------------------------------------------------------------
# Splash page template
# ---------------------------------------------------------------------------

class TestSplashPage:
    def test_contains_dashboard_link(self):
        html = _SPLASH_TEMPLATE.format(ssid="MyNet", dashboard_url="http://10.0.0.1:8080")
        assert 'href="http://10.0.0.1:8080"' in html

    def test_contains_ssid(self):
        html = _SPLASH_TEMPLATE.format(ssid="OffGrid Node", dashboard_url="http://10.0.0.1:8080")
        assert "OffGrid Node" in html

    def test_no_success_string(self):
        html = _SPLASH_TEMPLATE.format(ssid="Test", dashboard_url="http://x")
        assert "Success" not in html

    def test_escapes_html_in_ssid(self):
        import html as html_mod
        rendered = _SPLASH_TEMPLATE.format(
            ssid=html_mod.escape("<script>alert(1)</script>"),
            dashboard_url="http://x",
        )
        assert "<script>alert" not in rendered
        assert "&lt;script&gt;" in rendered


# ---------------------------------------------------------------------------
# HTTP handler responses
# ---------------------------------------------------------------------------

class TestHTTPHandler:
    """Test the HTTP handler responses using a real HTTPServer on localhost."""

    @pytest.fixture(autouse=True)
    def _setup_server(self, mock_app):
        self.plugin, _ = _make_plugin(mock_app, {"mode": "off"}, internet=True)
        self.plugin._splash_html = _SPLASH_TEMPLATE.format(
            ssid="Test", dashboard_url="http://10.0.0.1:8080",
        )
        self.plugin._ap_ip = "127.0.0.1"
        self.plugin._portal_port = 0

        import http.server

        plugin_ref = self.plugin

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                plugin_ref._requests_served += 1
                path = self.path.split("?")[0]
                if path == "/generate_204":
                    self._redirect()
                elif path == "/connecttest.txt":
                    self._redirect()
                elif path == "/check_network_status.txt":
                    self._redirect()
                else:
                    self._splash()

            def _splash(self):
                body = plugin_ref._splash_html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _redirect(self):
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://{plugin_ref._ap_ip}:{plugin_ref._portal_port}/",
                )
                self.end_headers()

            def log_message(self, fmt, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.plugin._portal_port = self.port
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        yield
        self.server.shutdown()

    def _get(self, path):
        import urllib.request
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, method="GET")
        try:
            resp = urllib.request.urlopen(req, timeout=5)
            return resp.status, dict(resp.headers), resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read().decode()

    def _get_no_redirect(self, path):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        headers = dict(resp.getheaders())
        body = resp.read().decode()
        status = resp.status
        conn.close()
        return status, headers, body

    def test_apple_hotspot_detect_returns_200_no_success(self):
        status, _, body = self._get("/hotspot-detect.html")
        assert status == 200
        assert "Success" not in body
        assert "Dashboard" in body

    def test_android_generate_204_returns_302(self):
        status, headers, _ = self._get_no_redirect("/generate_204")
        assert status == 302
        assert "Location" in headers

    def test_windows_connecttest_returns_302(self):
        status, headers, _ = self._get_no_redirect("/connecttest.txt")
        assert status == 302
        assert "Location" in headers

    def test_gnome_check_network_returns_302(self):
        status, headers, _ = self._get_no_redirect("/check_network_status.txt")
        assert status == 302

    def test_root_returns_splash(self):
        status, _, body = self._get("/")
        assert status == 200
        assert "http://10.0.0.1:8080" in body

    def test_requests_counted(self):
        before = self.plugin._requests_served
        self._get("/")
        self._get_no_redirect("/connecttest.txt")
        assert self.plugin._requests_served == before + 2


# ---------------------------------------------------------------------------
# State machine — activation / deactivation
# ---------------------------------------------------------------------------

class TestActivationLifecycle:
    def test_auto_mode_activates_when_offline(self, mock_app):
        plugin, mock_sp = _make_plugin(mock_app, internet=False)
        assert plugin._portal_active is True
        calls = [c for c in mock_sp.run.call_args_list if "activate" in str(c)]
        assert len(calls) >= 1

    def test_auto_mode_inactive_when_online(self, mock_app):
        plugin, mock_sp = _make_plugin(mock_app, internet=True)
        assert plugin._portal_active is False
        activate_calls = [c for c in mock_sp.run.call_args_list if "activate" in str(c)]
        assert len(activate_calls) == 0

    def test_always_mode_activates_when_online(self, mock_app):
        plugin, _ = _make_plugin(mock_app, {"mode": "always"}, internet=True)
        assert plugin._portal_active is True

    def test_always_mode_activates_when_offline(self, mock_app):
        plugin, _ = _make_plugin(mock_app, {"mode": "always"}, internet=False)
        assert plugin._portal_active is True

    def test_off_mode_never_activates(self, mock_app):
        plugin, _ = _make_plugin(mock_app, {"mode": "off"}, internet=False)
        assert plugin._portal_active is False

    def test_stop_deactivates(self, mock_app):
        plugin, _ = _make_plugin(mock_app, internet=False)
        assert plugin._portal_active is True
        with patch("reticulumpi.builtin_plugins.captive_portal.subprocess") as mock_sp:
            mock_sp.run.return_value = MagicMock(returncode=0, stdout="deactivated\n", stderr="")
            plugin.stop()
        assert plugin._portal_active is False


# ---------------------------------------------------------------------------
# Internet event hooks
# ---------------------------------------------------------------------------

class TestInternetEvents:
    def test_on_internet_available_deactivates_auto(self, mock_app):
        plugin, _ = _make_plugin(mock_app, internet=False)
        assert plugin._portal_active is True
        with patch("reticulumpi.builtin_plugins.captive_portal.subprocess") as mock_sp:
            mock_sp.run.return_value = MagicMock(returncode=0, stdout="deactivated\n", stderr="")
            plugin.on_internet_available()
        assert plugin._portal_active is False

    def test_on_internet_lost_activates_auto(self, mock_app):
        plugin, _ = _make_plugin(mock_app, internet=True)
        assert plugin._portal_active is False
        with patch("reticulumpi.builtin_plugins.captive_portal.subprocess") as mock_sp:
            mock_sp.run.return_value = MagicMock(returncode=0, stdout="activated\n", stderr="")
            plugin.on_internet_lost()
        assert plugin._portal_active is True

    def test_on_internet_available_noop_always(self, mock_app):
        plugin, _ = _make_plugin(mock_app, {"mode": "always"}, internet=True)
        assert plugin._portal_active is True
        plugin.on_internet_available()
        assert plugin._portal_active is True

    def test_on_internet_lost_noop_off(self, mock_app):
        plugin, _ = _make_plugin(mock_app, {"mode": "off"}, internet=False)
        assert plugin._portal_active is False
        plugin.on_internet_lost()
        assert plugin._portal_active is False


# ---------------------------------------------------------------------------
# Helper script invocations
# ---------------------------------------------------------------------------

class TestHelperCommands:
    def test_activate_calls_helper_with_correct_args(self, mock_app):
        plugin, mock_sp = _make_plugin(mock_app, internet=False)
        calls = mock_sp.run.call_args_list
        activate_call = [c for c in calls if "activate" in str(c)]
        assert len(activate_call) >= 1
        args = activate_call[-1][0][0]
        assert args[0] == "sudo"
        assert args[1] == "-n"
        assert "activate" in args
        assert "wlan0" in args
        assert "9999" in args
        assert "10.0.0.1" in args

    def test_deactivate_calls_helper(self, mock_app):
        plugin, _ = _make_plugin(mock_app, internet=False)
        assert plugin._portal_active is True
        with patch("reticulumpi.builtin_plugins.captive_portal.subprocess") as mock_sp2:
            mock_sp2.run.return_value = MagicMock(returncode=0, stdout="deactivated\n", stderr="")
            plugin._deactivate()
            deactivate_calls = [c for c in mock_sp2.run.call_args_list if "deactivate" in str(c)]
            assert len(deactivate_calls) >= 1
        assert plugin._portal_active is False

    def test_cleanup_called_on_start(self, mock_app):
        """start() always runs cleanup first for crash recovery."""
        plugin, mock_sp = _make_plugin(mock_app, internet=True)
        status_calls = [c for c in mock_sp.run.call_args_list if "status" in str(c)]
        assert len(status_calls) >= 1

    def test_cleanup_removes_stale_state(self, mock_app):
        probe = MagicMock()
        probe.is_online = True
        mock_app.internet_probe = probe

        hm = MagicMock()
        hm._iface = "wlan0"
        hm.broadcast_snapshot.return_value = {"ssid": "TestNet", "ip": "10.0.0.1"}
        wd = MagicMock()
        wd._port = 8080
        mock_app.get_plugin = MagicMock(
            side_effect=lambda n: {"hotspot_monitor": hm, "web_dashboard": wd}.get(n),
        )

        with (
            patch("reticulumpi.builtin_plugins.captive_portal.subprocess") as mock_sp,
            patch.object(CaptivePortalPlugin, "_start_http_server"),
        ):
            mock_sp.run.return_value = MagicMock(
                returncode=0,
                stdout="chain_active=true\ndns_active=true\nstate_file=true\n",
                stderr="",
            )
            plugin = CaptivePortalPlugin(mock_app, {"mode": "off", "portal_port": 9999})
            plugin._helper = "/fake/helper.sh"
            plugin.start()

            cleanup_calls = [c for c in mock_sp.run.call_args_list if "cleanup" in str(c)]
            assert len(cleanup_calls) == 1


# ---------------------------------------------------------------------------
# Status / broadcast
# ---------------------------------------------------------------------------

class TestStatusBroadcast:
    def test_get_status_fields(self, mock_app):
        plugin, _ = _make_plugin(mock_app, internet=True)
        status = plugin.get_status()
        assert "mode" in status
        assert "portal_active" in status
        assert "portal_port" in status
        assert "dashboard_url" in status
        assert "requests_served" in status

    def test_broadcast_snapshot_fields(self, mock_app):
        plugin, _ = _make_plugin(mock_app, internet=True)
        snap = plugin.broadcast_snapshot()
        assert snap is not None
        assert "mode" in snap
        assert "portal_active" in snap
        assert "requests_served" in snap


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------

class TestResolution:
    def test_resolve_ap_interface_from_config(self, mock_app):
        plugin, _ = _make_plugin(mock_app, {"ap_interface": "wlan1"}, internet=True)
        assert plugin._ap_interface == "wlan1"

    def test_resolve_ap_interface_from_hotspot_monitor(self, mock_app):
        plugin, _ = _make_plugin(mock_app, internet=True)
        assert plugin._ap_interface == "wlan0"

    def test_resolve_dashboard_url_from_config(self, mock_app):
        plugin, _ = _make_plugin(
            mock_app, {"dashboard_url": "http://custom:9090"}, internet=True,
        )
        assert plugin._dashboard_url == "http://custom:9090"

    def test_resolve_dashboard_url_auto(self, mock_app):
        plugin, _ = _make_plugin(mock_app, internet=True)
        assert plugin._dashboard_url == "http://10.0.0.1:8080"

    def test_resolve_dashboard_url_https_when_ssl(self, mock_app):
        plugin, _ = _make_plugin(mock_app, internet=True)
        wd = mock_app.get_plugin("web_dashboard")
        wd._ssl_context = MagicMock()
        assert plugin._resolve_dashboard_url().startswith("https://")

    def test_resolve_ssid_from_hotspot_monitor(self, mock_app):
        plugin, _ = _make_plugin(mock_app, internet=True)
        assert plugin._ssid == "TestNet"

    def test_parse_hostapd_interface(self, mock_app, tmp_path):
        conf = tmp_path / "hostapd.conf"
        conf.write_text("interface=wlan1\nssid=Test\n")
        plugin, _ = _make_plugin(mock_app, internet=True)
        with patch(
            "reticulumpi.builtin_plugins.captive_portal.Path",
        ) as mock_path_cls:
            real_path = __import__("pathlib").Path
            mock_path_cls.side_effect = lambda p: conf if p == "/etc/hostapd/hostapd.conf" else real_path(p)
            assert plugin._parse_hostapd_interface() == "wlan1"

    def test_parse_hostapd_interface_missing(self, mock_app):
        plugin, _ = _make_plugin(mock_app, internet=True)
        assert plugin._parse_hostapd_interface() == "wlan0"

    def test_detect_interface_ip_parses_output(self, mock_app):
        plugin, _ = _make_plugin(mock_app, internet=True)
        with patch("reticulumpi.builtin_plugins.captive_portal.subprocess") as mock_sp:
            mock_sp.run.return_value = MagicMock(
                stdout="2: wlan0: <BROADCAST> mtu 1500\n    inet 192.168.4.1/24 brd 192.168.4.255\n",
            )
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired
            assert CaptivePortalPlugin._detect_interface_ip("wlan0") == "192.168.4.1"

    def test_detect_interface_ip_fallback(self, mock_app):
        plugin, _ = _make_plugin(mock_app, internet=True)
        with patch("reticulumpi.builtin_plugins.captive_portal.subprocess") as mock_sp:
            mock_sp.run.return_value = MagicMock(stdout="")
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired
            assert CaptivePortalPlugin._detect_interface_ip("wlan0") == "10.0.0.1"

    def test_resolve_helper_path_fallback(self, mock_app, tmp_path):
        plugin, _ = _make_plugin(mock_app, internet=True)
        with (
            patch("reticulumpi.builtin_plugins.captive_portal.Path") as mock_path_cls,
            patch("reticulumpi.builtin_plugins.captive_portal.shutil") as mock_shutil,
        ):
            mock_path_cls.return_value.is_file.return_value = False
            mock_path_cls.side_effect = None
            mock_path_cls.return_value.resolve.return_value.parent = tmp_path
            mock_shutil.which.return_value = None
            path = plugin._resolve_helper_path()
            assert path == "/opt/reticulumpi/scripts/captive_portal_helper.sh"


# ---------------------------------------------------------------------------
# Helper failure / error paths
# ---------------------------------------------------------------------------

class TestHelperErrors:
    def test_run_helper_failure_raises(self, mock_app):
        plugin, _ = _make_plugin(mock_app, internet=True)
        with patch("reticulumpi.builtin_plugins.captive_portal.subprocess") as mock_sp:
            mock_sp.run.return_value = MagicMock(returncode=1, stdout="", stderr="iptables failed")
            with pytest.raises(RuntimeError, match="captive_portal_helper"):
                plugin._run_helper("activate", "wlan0", "9999", "10.0.0.1")

    def test_run_helper_timeout(self, mock_app):
        plugin, _ = _make_plugin(mock_app, internet=True)
        with patch("reticulumpi.builtin_plugins.captive_portal.subprocess") as mock_sp:
            import subprocess as sp_mod
            mock_sp.run.side_effect = sp_mod.TimeoutExpired(cmd="helper", timeout=15)
            with pytest.raises(sp_mod.TimeoutExpired):
                plugin._run_helper("activate", "wlan0", "9999", "10.0.0.1")

    def test_activate_failure_logs_not_crashes(self, mock_app):
        plugin, _ = _make_plugin(mock_app, internet=True)
        assert plugin._portal_active is False
        with patch("reticulumpi.builtin_plugins.captive_portal.subprocess") as mock_sp:
            mock_sp.run.return_value = MagicMock(returncode=1, stdout="", stderr="denied")
            plugin._activate()
            assert plugin._portal_active is False


# ---------------------------------------------------------------------------
# Idempotency guards
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_double_activate_is_noop(self, mock_app):
        plugin, _ = _make_plugin(mock_app, internet=False)
        assert plugin._portal_active is True
        with patch("reticulumpi.builtin_plugins.captive_portal.subprocess") as mock_sp2:
            mock_sp2.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            plugin._activate()
            assert mock_sp2.run.call_count == 0
        assert plugin._portal_active is True

    def test_double_deactivate_is_noop(self, mock_app):
        plugin, _ = _make_plugin(mock_app, internet=True)
        assert plugin._portal_active is False
        with patch("reticulumpi.builtin_plugins.captive_portal.subprocess") as mock_sp2:
            mock_sp2.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            plugin._deactivate()
            assert mock_sp2.run.call_count == 0
        assert plugin._portal_active is False
