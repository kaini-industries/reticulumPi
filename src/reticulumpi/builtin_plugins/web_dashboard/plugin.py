"""Web Dashboard plugin — secure web UI for node monitoring."""

import asyncio
import shutil
import subprocess
import time
from typing import Any

from reticulumpi.plugin_base import PluginBase


class WebDashboardPlugin(PluginBase):
    """Serves a secure web dashboard for monitoring the ReticulumPi node.

    Runs an aiohttp server in a background thread, providing:
    - Authenticated REST API for node status, metrics, plugins, and interfaces
    - WebSocket endpoint for real-time metric streaming
    - Self-contained HTML/JS/CSS frontend (no build step)
    - Optional HTTPS with self-signed certificate generation
    """

    plugin_name = "web_dashboard"
    plugin_version = "1.0.0"
    plugin_description = "Secure web dashboard for node monitoring and management"

    def validate_config(self) -> None:
        host = self.config.get("host", "127.0.0.1")
        if not isinstance(host, str):
            raise ValueError("host must be a string")

        port = self.config.get("port", 8080)
        if not isinstance(port, int) or port < 1 or port > 65535:
            raise ValueError("port must be an integer between 1 and 65535")

        session_timeout = self.config.get("session_timeout", 86400)
        if not isinstance(session_timeout, (int, float)) or session_timeout < 60:
            raise ValueError("session_timeout must be a number >= 60")

        max_sessions = self.config.get("max_sessions", 5)
        if not isinstance(max_sessions, int) or max_sessions < 1:
            raise ValueError("max_sessions must be a positive integer")

        session_gc_interval = self.config.get("session_gc_interval", 300)
        if (
            not isinstance(session_gc_interval, (int, float))
            or session_gc_interval < 30
        ):
            raise ValueError("session_gc_interval must be a number >= 30")

        metrics_interval = self.config.get("metrics_interval", 5)
        if not isinstance(metrics_interval, (int, float)) or metrics_interval < 1:
            raise ValueError("metrics_interval must be a number >= 1")

        max_ws = self.config.get("max_websocket_clients", 10)
        if not isinstance(max_ws, int) or max_ws < 1:
            raise ValueError("max_websocket_clients must be a positive integer")

        ws_compress = self.config.get("ws_compress", True)
        if not isinstance(ws_compress, bool):
            raise ValueError("ws_compress must be a boolean")

        ssl_config = self.config.get("ssl", {})
        if not isinstance(ssl_config, dict):
            raise ValueError("ssl must be a dict")

        lora_region = self.config.get("lora_region", "US")
        if not isinstance(lora_region, str) or lora_region not in {
            "US", "EU_868", "EU_433", "CN", "JP", "ANZ",
        }:
            raise ValueError(
                "lora_region must be one of: US, EU_868, EU_433, CN, JP, ANZ"
            )

    def start(self) -> None:
        self._active = True
        self._host = self.config.get("host", "127.0.0.1")
        self._port = self.config.get("port", 8080)
        self._start_time = time.time()

        # Import here so aiohttp is only required when the plugin is enabled
        from reticulumpi.builtin_plugins.web_dashboard.auth import (
            AuthManager,
            load_or_create_password_hash,
        )
        from reticulumpi.builtin_plugins.web_dashboard.server import create_app

        import os

        # Resolve password: env var > config password_hash > config password > auto-generate
        password_hash = (
            os.environ.get("RETICULUMPI_DASHBOARD_PASSWORD_HASH")
            or self.config.get("password_hash")
        )
        plaintext_password = (
            os.environ.get("RETICULUMPI_DASHBOARD_PASSWORD")
            or self.config.get("password")
        )
        generated_password = None

        if password_hash:
            source = "environment" if os.environ.get("RETICULUMPI_DASHBOARD_PASSWORD_HASH") else "config"
            self.log.info("Using dashboard password hash from %s", source)
        elif plaintext_password:
            pass  # handled below

        if not password_hash and not plaintext_password:
            secret_dir = self.config.get(
                "secret_dir", "~/.config/reticulumpi"
            )
            password_hash, generated_password = load_or_create_password_hash(secret_dir)
            if generated_password:
                banner = (
                    "\n"
                    "============================================================\n"
                    f"  Web dashboard password (first run): {generated_password}\n"
                    "  Save this password! It will not be shown again.\n"
                    "  To reset: delete ~/.config/reticulumpi/dashboard_secret\n"
                    "  Or set RETICULUMPI_DASHBOARD_PASSWORD env var.\n"
                    "  Retrieve from journal: journalctl -u reticulumpi | grep password\n"
                    "============================================================"
                )
                self.log.warning(banner)
                # Also print to stdout for interactive use / systemd journal
                print(banner, flush=True)
        elif plaintext_password and not password_hash:
            self.log.warning(
                "Using plaintext password in config. Generate a hash with "
                "'reticulumpi --hash-password' and set password_hash instead."
            )

        # Session persistence — store sessions in SQLite so they survive restarts
        secret_dir_resolved = os.path.expanduser(
            self.config.get("secret_dir", "~/.config/reticulumpi")
        )
        session_db = os.path.join(secret_dir_resolved, "sessions.db")

        self._auth = AuthManager(
            password_hash=password_hash,
            plaintext_password=plaintext_password,
            session_timeout=self.config.get("session_timeout", 86400),
            max_sessions=self.config.get("max_sessions", 5),
            session_db_path=session_db,
        )

        ssl_ctx = self._setup_ssl()

        self._aiohttp_app = create_app(self)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runner = None
        self._ssl_ctx = ssl_ctx

        self._start_thread(self._run_server, "web-dashboard")

        scheme = "https" if ssl_ctx else "http"
        self.log.info(
            "Web dashboard starting on %s://%s:%d", scheme, self._host, self._port
        )

        if self._host != "127.0.0.1" and not ssl_ctx:
            self.log.warning(
                "Dashboard is accessible over the network without encryption. "
                "Consider enabling SSL in the web_dashboard config."
            )

        self._mdns_proc: subprocess.Popen | None = None
        if self._host != "127.0.0.1" and shutil.which("avahi-publish-service"):
            try:
                self._mdns_proc = subprocess.Popen(
                    [
                        "avahi-publish-service",
                        "ReticulumPi Dashboard",
                        "_http._tcp",
                        str(self._port),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                import socket
                self.log.info(
                    "mDNS: dashboard advertised at %s://%s.local:%d",
                    scheme, socket.gethostname(), self._port,
                )
            except OSError:
                self.log.warning("Failed to start mDNS advertisement")

    def stop(self) -> None:
        self._active = False
        if self._mdns_proc:
            self._mdns_proc.terminate()
            self._mdns_proc.wait(timeout=5)
            self._mdns_proc = None
        if self._loop and self._runner:
            future = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
            try:
                future.result(timeout=10)
            except Exception:
                self.log.exception("Error during web dashboard shutdown")
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._join_threads(timeout=10)
        if hasattr(self, "_auth") and hasattr(self._auth.sessions, "close"):
            self._auth.sessions.close()

    def get_status(self) -> dict[str, Any]:
        ssl_config = self.config.get("ssl", {})
        scheme = "https" if ssl_config.get("enabled") else "http"
        host = getattr(self, "_host", self.config.get("host", "127.0.0.1"))
        port = getattr(self, "_port", self.config.get("port", 8080))
        return {
            "active": self._active,
            "host": host,
            "port": port,
            "web_url": f"{scheme}://{host}:{port}",
            "uptime": time.time() - getattr(self, "_start_time", time.time()) if self._active else 0,
            "active_sessions": len(self._auth.sessions) if hasattr(self, "_auth") else 0,
        }

    def _run_server(self) -> None:
        """Run the aiohttp server in a dedicated asyncio event loop."""

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        try:
            self._loop.run_until_complete(self._start_server())
            self._loop.run_forever()
        except Exception:
            self.log.exception("Web dashboard server error")
        finally:
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    async def _start_server(self) -> None:
        import aiohttp.web

        self._runner = aiohttp.web.AppRunner(self._aiohttp_app)
        await self._runner.setup()
        site = aiohttp.web.TCPSite(
            self._runner, self._host, self._port, ssl_context=self._ssl_ctx
        )
        await site.start()
        self.log.info("Web dashboard listening on %s:%d", self._host, self._port)

        # Start periodic session garbage collection
        self._session_gc_task = asyncio.ensure_future(self._session_gc_loop())

    async def _session_gc_loop(self) -> None:
        """Periodically sweep expired sessions from memory."""
        interval = self.config.get("session_gc_interval", 300)
        while self._active:
            await asyncio.sleep(interval)
            try:
                removed = self._auth.cleanup_expired_sessions()
                if removed:
                    self.log.info(
                        "Session GC: removed %d expired session(s), %d active",
                        removed,
                        len(self._auth.sessions),
                    )
            except Exception:
                self.log.exception("Session GC error")

    async def _shutdown(self) -> None:
        if hasattr(self, "_session_gc_task") and self._session_gc_task:
            self._session_gc_task.cancel()
            try:
                await self._session_gc_task
            except asyncio.CancelledError:
                pass
        if self._runner:
            await self._runner.cleanup()
        # Drain lingering tasks (e.g. aiohttp's compressed-frame writer
        # offloaded to the executor) so asyncio doesn't log
        # "Task was destroyed but it is pending" when the loop stops.
        pending = [
            t for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        ]
        if pending:
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    def _setup_ssl(self):
        """Configure SSL context if enabled in config."""
        ssl_config = self.config.get("ssl", {})
        if not ssl_config.get("enabled", False):
            return None

        import ssl

        cert_file = ssl_config.get("cert_file")
        key_file = ssl_config.get("key_file")
        auto_generate = ssl_config.get("auto_generate", False)

        if auto_generate and (not cert_file or not key_file):
            from reticulumpi.builtin_plugins.web_dashboard.ssl_utils import (
                generate_self_signed_cert,
            )

            cert_dir = ssl_config.get(
                "cert_dir", "~/.config/reticulumpi/web_certs"
            )
            cert_file, key_file = generate_self_signed_cert(
                cert_dir, self.app.config.node_name, self.log
            )

        if not cert_file or not key_file:
            raise ValueError(
                "SSL enabled but no cert_file/key_file provided and auto_generate is false"
            )

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_file, key_file)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx
