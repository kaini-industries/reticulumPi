"""Remote Control plugin — manage nodes over Reticulum using RNS Links."""

from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Any

import RNS
import RNS.vendor.umsgpack as umsgpack

from reticulumpi.plugin_base import PluginBase

# Ring buffer for log capture
_LOG_BUFFER_SIZE = 500


class _LogRingBuffer(logging.Handler):
    """Captures log records into a bounded ring buffer."""

    def __init__(self, maxlen: int = _LOG_BUFFER_SIZE):
        super().__init__()
        self._buffer: collections.deque[dict[str, Any]] = collections.deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        self._buffer.append({
            "ts": record.created,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        })

    def get_lines(self, count: int = 100) -> list[dict[str, Any]]:
        return list(self._buffer)[-count:]


class RemoteControlPlugin(PluginBase):
    """Accepts authenticated RNS Link connections for remote node management.

    Only identities listed in ``allowed_identities`` config can connect.
    All communication is encrypted end-to-end via RNS Link.
    """

    plugin_name = "remote_control"
    plugin_version = "1.0.0"
    plugin_description = "Remote node management over Reticulum Links"

    def validate_config(self) -> None:
        allowed = self.config.get("allowed_identities", [])
        if not isinstance(allowed, list):
            raise ValueError("allowed_identities must be a list of hex identity hashes")
        for entry in allowed:
            if not isinstance(entry, str) or len(entry) < 8:
                raise ValueError(f"Invalid identity hash: {entry!r}")

    def start(self) -> None:
        self._active = True
        self._start_time = time.time()
        self._active_links: list[Any] = []
        self._links_lock = threading.Lock()

        # Parse allowed identity hashes
        self._allowed_hashes: set[bytes] = set()
        for hex_hash in self.config.get("allowed_identities", []):
            try:
                self._allowed_hashes.add(bytes.fromhex(hex_hash.replace("<", "").replace(">", "")))
            except ValueError:
                self.log.warning("Ignoring invalid identity hash: %s", hex_hash)

        if not self._allowed_hashes:
            self.log.warning(
                "No allowed_identities configured — remote control will reject all connections"
            )

        # Install log ring buffer
        buf_size = self.config.get("log_buffer_lines", _LOG_BUFFER_SIZE)
        self._log_buffer = _LogRingBuffer(maxlen=buf_size)
        self._log_buffer.setLevel(logging.DEBUG)
        logging.getLogger("reticulumpi").addHandler(self._log_buffer)

        # Create control destination
        self.destination = RNS.Destination(
            self.identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            "reticulumpi",
            "node",
            "control",
        )

        # Accept incoming links
        self.destination.set_link_established_callback(self._link_established)

        # Register request handlers
        self.destination.register_request_handler(
            "/ping", self._handle_ping, allow=RNS.Destination.ALLOW_ALL
        )
        self.destination.register_request_handler(
            "/status", self._handle_status, allow=RNS.Destination.ALLOW_ALL
        )
        self.destination.register_request_handler(
            "/metrics", self._handle_metrics, allow=RNS.Destination.ALLOW_ALL
        )
        self.destination.register_request_handler(
            "/plugins", self._handle_plugins, allow=RNS.Destination.ALLOW_ALL
        )
        self.destination.register_request_handler(
            "/interfaces", self._handle_interfaces, allow=RNS.Destination.ALLOW_ALL
        )
        self.destination.register_request_handler(
            "/config", self._handle_config, allow=RNS.Destination.ALLOW_ALL
        )
        self.destination.register_request_handler(
            "/logs", self._handle_logs, allow=RNS.Destination.ALLOW_ALL
        )
        self.destination.register_request_handler(
            "/plugin/enable", self._handle_plugin_enable, allow=RNS.Destination.ALLOW_ALL
        )
        self.destination.register_request_handler(
            "/plugin/disable", self._handle_plugin_disable, allow=RNS.Destination.ALLOW_ALL
        )
        self.destination.register_request_handler(
            "/announce", self._handle_announce, allow=RNS.Destination.ALLOW_ALL
        )

        self.log.info(
            "Remote control active at %s (%d authorized identities)",
            RNS.prettyhexrep(self.destination.hash),
            len(self._allowed_hashes),
        )

    def stop(self) -> None:
        self._active = False
        # Close all active links
        with self._links_lock:
            links = list(self._active_links)
            self._active_links.clear()
        for link in links:
            try:
                link.teardown()
            except Exception:
                pass
        # Remove log handler
        try:
            logging.getLogger("reticulumpi").removeHandler(self._log_buffer)
        except Exception:
            pass
        self._join_threads()

    def get_status(self) -> dict[str, Any]:
        with self._links_lock:
            link_count = len(self._active_links)
        return {
            "active": self._active,
            "active_links": link_count,
            "allowed_identities": len(self._allowed_hashes),
            "address": RNS.prettyhexrep(self.destination.hash) if hasattr(self, "destination") and self.destination else None,
        }

    # --- Link lifecycle ---

    def _link_established(self, link: Any) -> None:
        """Called when a remote peer establishes a Link."""
        self.log.info("Incoming link from %s", link)
        link.set_remote_identified_callback(self._remote_identified)
        link.set_link_closed_callback(self._link_closed)

    def _remote_identified(self, link: Any, identity: Any) -> None:
        """Called when the remote peer identifies itself."""
        if identity.hash not in self._allowed_hashes:
            self.log.warning(
                "Rejecting unauthorized identity: %s",
                RNS.prettyhexrep(identity.hash),
            )
            link.teardown()
            return

        self.log.info(
            "Authorized remote control link from %s",
            RNS.prettyhexrep(identity.hash),
        )
        with self._links_lock:
            self._active_links.append(link)

    def _link_closed(self, link: Any) -> None:
        """Called when a link is closed."""
        with self._links_lock:
            if link in self._active_links:
                self._active_links.remove(link)
        self.log.debug("Remote control link closed")

    # --- Request handlers ---
    # Each receives (path, data, request_id, link_id, remote_identity, requested_at)
    # and returns a response that gets sent back over the link.

    def _handle_ping(self, path: str, data: Any, request_id: Any, link_id: Any, remote_identity: Any, requested_at: Any) -> Any:
        return umsgpack.packb({"ok": True, "time": time.time(), "node": self.app.node_name})

    def _handle_status(self, path: str, data: Any, request_id: Any, link_id: Any, remote_identity: Any, requested_at: Any) -> Any:
        status = self.app.get_status()
        status["node_name"] = self.app.node_name
        status["identity_hash"] = RNS.prettyhexrep(self.app.identity.hash) if self.app.identity else ""
        status["uptime"] = time.time() - self._start_time
        return umsgpack.packb({"ok": True, "data": status})

    def _handle_metrics(self, path: str, data: Any, request_id: Any, link_id: Any, remote_identity: Any, requested_at: Any) -> Any:
        monitor = self.app.get_plugin("system_monitor")
        if monitor and hasattr(monitor, "latest_metrics"):
            return umsgpack.packb({"ok": True, "data": monitor.latest_metrics})
        return umsgpack.packb({"ok": False, "error": "system_monitor not available"})

    def _handle_plugins(self, path: str, data: Any, request_id: Any, link_id: Any, remote_identity: Any, requested_at: Any) -> Any:
        plugins = {}
        for name, plugin in self.app.plugins.items():
            try:
                plugins[name] = {
                    "version": plugin.plugin_version,
                    "description": plugin.plugin_description,
                    "status": plugin.get_status(),
                }
            except Exception:
                plugins[name] = {"error": "status collection failed"}
        return umsgpack.packb({"ok": True, "data": plugins})

    def _handle_interfaces(self, path: str, data: Any, request_id: Any, link_id: Any, remote_identity: Any, requested_at: Any) -> Any:
        interfaces = []
        try:
            for iface in RNS.Transport.interfaces:
                info = {
                    "name": str(iface),
                    "type": type(iface).__name__,
                    "online": getattr(iface, "online", True),
                }
                for attr in ("rxb", "txb", "bitrate"):
                    val = getattr(iface, attr, None)
                    if val is not None:
                        info[attr] = val
                interfaces.append(info)
        except Exception:
            pass
        return umsgpack.packb({"ok": True, "data": interfaces})

    def _handle_config(self, path: str, data: Any, request_id: Any, link_id: Any, remote_identity: Any, requested_at: Any) -> Any:
        import copy
        config_data = copy.deepcopy(self.app.config._data)
        # Strip sensitive values
        plugins = config_data.get("plugins", {})
        for plugin_cfg in plugins.values():
            if isinstance(plugin_cfg, dict):
                for key in ("password", "password_hash", "secret"):
                    plugin_cfg.pop(key, None)
        return umsgpack.packb({"ok": True, "data": config_data})

    def _handle_logs(self, path: str, data: Any, request_id: Any, link_id: Any, remote_identity: Any, requested_at: Any) -> Any:
        count = 100
        if isinstance(data, bytes):
            try:
                req = umsgpack.unpackb(data)
                if isinstance(req, dict):
                    count = min(req.get("count", 100), 1000)
            except Exception:
                pass
        lines = self._log_buffer.get_lines(count)
        return umsgpack.packb({"ok": True, "data": lines})

    def _handle_plugin_enable(self, path: str, data: Any, request_id: Any, link_id: Any, remote_identity: Any, requested_at: Any) -> Any:
        if not isinstance(data, bytes):
            return umsgpack.packb({"ok": False, "error": "missing plugin name"})
        try:
            req = umsgpack.unpackb(data)
            name = req.get("name") if isinstance(req, dict) else str(req)
        except Exception:
            return umsgpack.packb({"ok": False, "error": "invalid request"})

        try:
            self.app.enable_plugin(name)
            return umsgpack.packb({"ok": True, "message": f"Plugin '{name}' enabled"})
        except Exception as e:
            return umsgpack.packb({"ok": False, "error": str(e)})

    def _handle_plugin_disable(self, path: str, data: Any, request_id: Any, link_id: Any, remote_identity: Any, requested_at: Any) -> Any:
        if not isinstance(data, bytes):
            return umsgpack.packb({"ok": False, "error": "missing plugin name"})
        try:
            req = umsgpack.unpackb(data)
            name = req.get("name") if isinstance(req, dict) else str(req)
        except Exception:
            return umsgpack.packb({"ok": False, "error": "invalid request"})

        try:
            self.app.disable_plugin(name)
            return umsgpack.packb({"ok": True, "message": f"Plugin '{name}' disabled"})
        except Exception as e:
            return umsgpack.packb({"ok": False, "error": str(e)})

    def _handle_announce(self, path: str, data: Any, request_id: Any, link_id: Any, remote_identity: Any, requested_at: Any) -> Any:
        """Trigger an immediate announce from heartbeat_announce."""
        heartbeat = self.app.get_plugin("heartbeat_announce")
        if heartbeat and hasattr(heartbeat, "destination"):
            try:
                app_data = heartbeat._build_app_data()
                heartbeat.destination.announce(
                    app_data=app_data.encode("utf-8") if app_data else None
                )
                return umsgpack.packb({"ok": True, "message": "Announce sent"})
            except Exception as e:
                return umsgpack.packb({"ok": False, "error": str(e)})
        return umsgpack.packb({"ok": False, "error": "heartbeat_announce not available"})
