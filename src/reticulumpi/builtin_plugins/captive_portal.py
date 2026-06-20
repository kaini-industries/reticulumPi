"""Captive portal plugin — guides hotspot clients to the web dashboard.

Intercepts OS-level captive portal detection probes (Apple, Android,
Windows, GNOME) so that devices connecting to the Pi's Wi-Fi hotspot
are presented with a splash page linking to the dashboard.

Three OS-level resources are managed (all via a privileged helper script):
  1. dnsmasq address overrides for portal detection domains
  2. iptables NAT REDIRECT from port 80 to the portal HTTP server
  3. A lightweight HTTP server responding to detection probes
"""

from __future__ import annotations

import html
import http.server
import shutil
import subprocess
import threading
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase

if TYPE_CHECKING:
    pass

_VALID_MODES = ("auto", "always", "off")
_HELPER_SCRIPT = "/opt/reticulumpi/scripts/captive_portal_helper.sh"

_SPLASH_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reticulum Pi</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,system-ui,sans-serif;
text-align:center;padding:2em 1em;background:#1a1a2e;color:#e0e0e0}}
.card{{max-width:400px;margin:2em auto;padding:2em;
background:#16213e;border-radius:12px;box-shadow:0 4px 24px rgba(0,0,0,.3)}}
h1{{font-size:1.4em;margin-bottom:.5em}}
.ssid{{color:#53c2f0;font-weight:700}}
.btn{{display:inline-block;margin-top:1.5em;padding:.8em 2em;
background:#0f3460;color:#e0e0e0;text-decoration:none;
border-radius:8px;font-size:1.1em}}
.btn:hover{{background:#1a5276}}
.hint{{margin-top:1.5em;font-size:.85em;opacity:.7}}
</style>
</head>
<body>
<div class="card">
<h1>Welcome to <span class="ssid">{ssid}</span></h1>
<p>You are connected to a Reticulum mesh node.</p>
<a class="btn" href="{dashboard_url}">Open Dashboard</a>
<p class="hint">Bookmark <code>{dashboard_url}</code> for direct access.</p>
</div>
</body>
</html>
"""


class CaptivePortalPlugin(PluginBase):
    plugin_name = "captive_portal"
    plugin_version = "1.0.0"
    plugin_description = "Captive portal for hotspot client discovery"
    plugin_dependencies = ("hotspot_monitor",)

    broadcast_tier = 2
    broadcast_keys = "captive_portal"

    def validate_config(self) -> None:
        mode = self.config.get("mode", "auto")
        if mode not in _VALID_MODES:
            raise ValueError(f"captive_portal.mode must be one of {_VALID_MODES}, got '{mode}'")
        port = self.config.get("portal_port", 8081)
        if not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ValueError(f"captive_portal.portal_port must be 1024-65535, got {port}")

    def start(self) -> None:
        self._active = True
        self._mode: str = self.config.get("mode", "auto")
        self._portal_port: int = self.config.get("portal_port", 8081)
        self._portal_active = False
        self._requests_served: int = 0
        self._state_lock = threading.Lock()

        self._ap_interface = self._resolve_ap_interface()
        self._ap_ip = self._resolve_ap_ip()
        self._ssid = self._resolve_ssid()
        self._dashboard_url = self._resolve_dashboard_url()
        self._helper = self._resolve_helper_path()

        self._splash_html = _SPLASH_TEMPLATE.format(
            ssid=html.escape(self._ssid),
            dashboard_url=html.escape(self._dashboard_url),
        )

        self._httpd: http.server.HTTPServer | None = None
        self._cleanup_stale_rules()
        self._start_http_server()

        should_activate = self._mode == "always" or (
            self._mode == "auto" and not self.internet_available
        )
        if should_activate:
            self._activate()

        self.log.info(
            "Captive portal ready (mode=%s, port=%d, active=%s)",
            self._mode,
            self._portal_port,
            self._portal_active,
        )

    def stop(self) -> None:
        self._active = False
        with self._state_lock:
            if self._portal_active:
                self._do_deactivate()
        self._stop_http_server()
        self._join_threads()

    # ── internet hooks ────────────────────────────────────────────────

    def on_internet_available(self) -> None:
        if self._mode == "auto":
            self._deactivate()

    def on_internet_lost(self) -> None:
        if self._mode == "auto":
            self._activate()

    # ── broadcast / status ────────────────────────────────────────────

    def get_status(self) -> dict[str, Any]:
        return {
            "active": self._active,
            "mode": self._mode,
            "portal_active": self._portal_active,
            "portal_port": self._portal_port,
            "dashboard_url": self._dashboard_url,
            "requests_served": self._requests_served,
        }

    def broadcast_snapshot(self, cycle_count: int = 0) -> dict[str, Any] | None:
        return {
            "mode": self._mode,
            "portal_active": self._portal_active,
            "requests_served": self._requests_served,
        }

    # ── activate / deactivate ─────────────────────────────────────────

    def _activate(self) -> None:
        with self._state_lock:
            if self._portal_active:
                return
            self._do_activate()

    def _deactivate(self) -> None:
        with self._state_lock:
            if not self._portal_active:
                return
            self._do_deactivate()

    def _do_activate(self) -> None:
        try:
            self._run_helper(
                "activate",
                self._ap_interface,
                str(self._portal_port),
                self._ap_ip,
            )
            self._portal_active = True
            self.log.info("Captive portal activated (interface=%s)", self._ap_interface)
            self.event_bus.publish(
                events.CAPTIVE_PORTAL_ACTIVATED,
                {
                    "interface": self._ap_interface,
                    "portal_port": self._portal_port,
                },
            )
        except Exception:
            self.log.exception("Failed to activate captive portal")

    def _do_deactivate(self) -> None:
        try:
            self._run_helper("deactivate")
            self._portal_active = False
            self.log.info("Captive portal deactivated")
            self.event_bus.publish(events.CAPTIVE_PORTAL_DEACTIVATED, {})
        except Exception:
            self.log.exception("Failed to deactivate captive portal")

    def _cleanup_stale_rules(self) -> None:
        try:
            result = self._run_helper("status")
            if "chain_active=true" in result or "dns_active=true" in result:
                self.log.warning("Found stale captive portal state — cleaning up")
                self._run_helper("cleanup")
        except Exception:
            self.log.debug("Stale rule check skipped (helper unavailable)")

    def _run_helper(self, *args: str) -> str:
        result = subprocess.run(
            ["sudo", "-n", self._helper, *args],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            self.log.error(
                "Helper '%s' failed (rc=%d): %s",
                args[0] if args else "?",
                result.returncode,
                result.stderr.strip(),
            )
            raise RuntimeError(f"captive_portal_helper {args[0]} failed")
        return result.stdout

    # ── HTTP server ───────────────────────────────────────────────────

    def _start_http_server(self) -> None:
        plugin = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                # GIL guarantees atomicity for single-word integer increments
                # on CPython, so no explicit lock is needed here.
                plugin._requests_served += 1
                path = self.path.split("?")[0]

                if path == "/generate_204":
                    self._redirect_to_splash()
                elif path == "/connecttest.txt":
                    self._redirect_to_splash()
                elif path == "/check_network_status.txt":
                    self._redirect_to_splash()
                else:
                    self._serve_splash()

            def _serve_splash(self) -> None:
                body = plugin._splash_html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _redirect_to_splash(self) -> None:
                self.send_response(302)
                self.send_header("Location", f"http://{plugin._ap_ip}:{plugin._portal_port}/")
                self.end_headers()

            def log_message(self, fmt: str, *args: Any) -> None:
                plugin.log.debug("portal-http: %s", fmt % args)

        try:
            self._httpd = http.server.HTTPServer(
                (self._ap_ip, self._portal_port),
                Handler,
            )
            self._httpd.timeout = 1
            self._start_thread(self._http_serve_loop, "captive-http")
        except OSError as exc:
            self.log.error(
                "Cannot start portal HTTP server on %s:%d: %s", self._ap_ip, self._portal_port, exc
            )

    def _http_serve_loop(self) -> None:
        while self._active:
            httpd = self._httpd
            if httpd is None:
                break
            httpd.handle_request()

    def _stop_http_server(self) -> None:
        if self._httpd:
            self._httpd.server_close()
            self._httpd = None

    # ── resolution helpers ────────────────────────────────────────────

    def _resolve_ap_interface(self) -> str:
        configured = self.config.get("ap_interface")
        if configured:
            return configured
        hm = self.app.get_plugin("hotspot_monitor") if hasattr(self.app, "get_plugin") else None
        if hm and hasattr(hm, "get_interface"):
            iface = hm.get_interface()
            if isinstance(iface, str) and iface:
                return iface
        return self._parse_hostapd_interface()

    def _resolve_ap_ip(self) -> str:
        configured = self.config.get("ap_ip")
        if configured:
            return configured
        hm = self.app.get_plugin("hotspot_monitor") if hasattr(self.app, "get_plugin") else None
        if hm:
            snap = hm.broadcast_snapshot()
            if snap and snap.get("ip"):
                return snap["ip"]
        return self._detect_interface_ip(self._ap_interface)

    def _resolve_ssid(self) -> str:
        hm = self.app.get_plugin("hotspot_monitor") if hasattr(self.app, "get_plugin") else None
        if hm:
            snap = hm.broadcast_snapshot()
            if snap and snap.get("ssid"):
                return snap["ssid"]
        return self.config.get("ssid", "Reticulum Pi")

    def _resolve_dashboard_url(self) -> str:
        configured = self.config.get("dashboard_url")
        if configured:
            return configured
        wd = self.app.get_plugin("web_dashboard") if hasattr(self.app, "get_plugin") else None
        if wd and hasattr(wd, "get_status"):
            try:
                status = wd.get_status()
                url = status.get("web_url") if isinstance(status, dict) else None
                if isinstance(url, str) and url:
                    # Replace host with AP IP for captive-portal context
                    parsed = urllib.parse.urlparse(url)
                    port = parsed.port or 80
                    return parsed._replace(
                        netloc=f"{self._ap_ip}:{port}"
                    ).geturl()
            except Exception:
                pass
        return f"http://{self._ap_ip}:8080"

    def _resolve_helper_path(self) -> str:
        if Path(_HELPER_SCRIPT).is_file():
            return _HELPER_SCRIPT
        local = (
            Path(__file__).resolve().parent.parent.parent.parent
            / "scripts"
            / "captive_portal_helper.sh"
        )
        if local.is_file():
            return str(local)
        found = shutil.which("captive_portal_helper.sh")
        return found or _HELPER_SCRIPT

    def _parse_hostapd_interface(self) -> str:
        try:
            for line in Path("/etc/hostapd/hostapd.conf").read_text().splitlines():
                line = line.strip()
                if line.startswith("interface="):
                    return line.split("=", 1)[1].strip()
        except OSError:
            pass
        return "wlan0"

    @staticmethod
    def _detect_interface_ip(iface: str) -> str:
        try:
            import re

            out = subprocess.run(
                ["ip", "-4", "addr", "show", iface],
                capture_output=True,
                text=True,
                timeout=5,
            )
            m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out.stdout)
            if m:
                return m.group(1)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return "10.0.0.1"
