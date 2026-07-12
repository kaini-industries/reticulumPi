"""Web Dashboard plugin — secure web UI for node monitoring."""

import asyncio
import concurrent.futures
import ipaddress
import os
import secrets
import shutil
import subprocess
import threading
import time
from typing import Any

from reticulumpi._paths import runtime_cache_path
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
    plugin_lifecycle_api = 2
    TLS_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
    TLS_MIN_CHECK_INTERVAL_SECONDS = 60
    TLS_OPERATOR_VALIDITY_GUARD_SECONDS = 24 * 60 * 60
    TLS_MANAGED_RENEWAL_GUARD_SECONDS = 30 * 24 * 60 * 60

    @staticmethod
    def _dashboard_readiness_path() -> str:
        runtime_dir = os.environ.get("RETICULUMPI_RUNTIME_DIR", "/run/reticulumpi")
        return os.path.join(runtime_dir, "dashboard-ready")

    def _set_dashboard_readiness(self, ready: bool) -> None:
        """Publish bind readiness atomically and remove it on every exit path."""

        path = self._dashboard_readiness_path()
        if not ready:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                self.log.warning("Could not remove dashboard readiness marker %s: %s", path, exc)
            return
        from reticulumpi.builtin_plugins.web_dashboard.auth import write_secret_file_atomic

        write_secret_file_atomic(path, "ready\n")

    def _publish_dashboard_ready(self) -> None:
        """Publish readiness only after the listening site is fully initialized."""

        self._set_dashboard_readiness(True)
        self.mark_ready()
        self._server_ready.set()

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

        max_sessions = self.config.get("max_sessions", 10)
        if not isinstance(max_sessions, int) or max_sessions < 1:
            raise ValueError("max_sessions must be a positive integer")

        session_gc_interval = self.config.get("session_gc_interval", 300)
        if not isinstance(session_gc_interval, (int, float)) or session_gc_interval < 30:
            raise ValueError("session_gc_interval must be a number >= 30")

        metrics_interval = self.config.get("metrics_interval", 5)
        if not isinstance(metrics_interval, (int, float)) or metrics_interval < 1:
            raise ValueError("metrics_interval must be a number >= 1")

        callback_timeout = self.config.get("broadcast_callback_timeout_ms", 500)
        if (
            not isinstance(callback_timeout, (int, float))
            or isinstance(callback_timeout, bool)
            or not 10 <= callback_timeout <= 5000
        ):
            raise ValueError("broadcast_callback_timeout_ms must be between 10 and 5000")
        warm_timeout = self.config.get("broadcast_warm_timeout", 2)
        if (
            not isinstance(warm_timeout, (int, float))
            or isinstance(warm_timeout, bool)
            or not 0.1 <= warm_timeout <= 10
        ):
            raise ValueError("broadcast_warm_timeout must be between 0.1 and 10 seconds")

        max_ws = self.config.get("max_websocket_clients", 10)
        if not isinstance(max_ws, int) or max_ws < 1:
            raise ValueError("max_websocket_clients must be a positive integer")

        ws_compress = self.config.get("ws_compress", True)
        if not isinstance(ws_compress, bool):
            raise ValueError("ws_compress must be a boolean")

        ws_revalidate = self.config.get("ws_session_revalidate_interval", 30)
        if not isinstance(ws_revalidate, (int, float)) or ws_revalidate < 5:
            raise ValueError("ws_session_revalidate_interval must be a number >= 5")

        startup_timeout = self.config.get("startup_timeout", 15)
        if not isinstance(startup_timeout, (int, float)) or startup_timeout < 1:
            raise ValueError("startup_timeout must be a number >= 1")

        local_api = self.config.get("local_api", {})
        if not isinstance(local_api, dict):
            raise ValueError("local_api must be a dict")
        if not isinstance(local_api.get("enabled", False), bool):
            raise ValueError("local_api.enabled must be a boolean")
        token_file = local_api.get("token_file")
        if token_file is not None and not isinstance(token_file, str):
            raise ValueError("local_api.token_file must be a string")

        ssl_config = self.config.get("ssl", {})
        if not isinstance(ssl_config, dict):
            raise ValueError("ssl must be a dict")
        if not isinstance(ssl_config.get("enabled", False), bool):
            raise ValueError("ssl.enabled must be a boolean")
        if not isinstance(ssl_config.get("auto_generate", False), bool):
            raise ValueError("ssl.auto_generate must be a boolean")
        cert_file = ssl_config.get("cert_file")
        key_file = ssl_config.get("key_file")
        if cert_file is not None and not isinstance(cert_file, str):
            raise ValueError("ssl.cert_file must be a string")
        if key_file is not None and not isinstance(key_file, str):
            raise ValueError("ssl.key_file must be a string")
        if bool(cert_file) != bool(key_file):
            raise ValueError("ssl.cert_file and ssl.key_file must be configured together")
        extra_hostnames = ssl_config.get("extra_hostnames", [])
        if not isinstance(extra_hostnames, list) or not all(
            isinstance(h, str) for h in extra_hostnames
        ):
            raise ValueError("ssl.extra_hostnames must be a list of strings")
        if any(not hostname.strip() for hostname in extra_hostnames):
            raise ValueError("ssl.extra_hostnames entries must be non-empty")
        if (cert_file or key_file) and not extra_hostnames:
            raise ValueError(
                "ssl.extra_hostnames must name at least one required SAN for operator TLS"
            )

        force_secure_cookie = self.config.get("force_secure_cookie", False)
        if not isinstance(force_secure_cookie, bool):
            raise ValueError("force_secure_cookie must be a boolean")

        reverse_proxy = self.config.get("reverse_proxy", {})
        if not isinstance(reverse_proxy, dict):
            raise ValueError("reverse_proxy must be a dict")
        unknown_proxy_keys = set(reverse_proxy) - {"enabled", "trusted_networks"}
        if unknown_proxy_keys:
            raise ValueError(
                "reverse_proxy contains unsupported keys: " + ", ".join(sorted(unknown_proxy_keys))
            )
        proxy_enabled = reverse_proxy.get("enabled", False)
        if not isinstance(proxy_enabled, bool):
            raise ValueError("reverse_proxy.enabled must be a boolean")
        trusted_proxy_networks = reverse_proxy.get("trusted_networks", [])
        if not isinstance(trusted_proxy_networks, list) or not all(
            isinstance(cidr, str) for cidr in trusted_proxy_networks
        ):
            raise ValueError("reverse_proxy.trusted_networks must be a list of CIDR strings")
        if proxy_enabled and not trusted_proxy_networks:
            raise ValueError("reverse_proxy.trusted_networks cannot be empty when enabled")
        for cidr in trusted_proxy_networks:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError:
                raise ValueError(
                    f"reverse_proxy.trusted_networks entry is not a valid CIDR: {cidr!r}"
                )

        rate_limit = self.config.get("rate_limit", {})
        if not isinstance(rate_limit, dict):
            raise ValueError("rate_limit must be a dict")
        max_attempts = rate_limit.get("max_attempts", 5)
        if not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("rate_limit.max_attempts must be an integer >= 1")
        window_seconds = rate_limit.get("window_seconds", 60)
        if not isinstance(window_seconds, int) or window_seconds < 1:
            raise ValueError("rate_limit.window_seconds must be an integer >= 1")

        allowed_networks = self.config.get("allowed_networks", [])
        if not isinstance(allowed_networks, list):
            raise ValueError("allowed_networks must be a list of CIDR strings")
        for cidr in allowed_networks:
            if not isinstance(cidr, str):
                raise ValueError("allowed_networks entries must be CIDR strings")
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError:
                raise ValueError(f"allowed_networks entry is not a valid CIDR: {cidr!r}")

        tile_cache_entries = self.config.get("tile_cache_entries", 5000)
        if not isinstance(tile_cache_entries, int) or tile_cache_entries < 100:
            raise ValueError("tile_cache_entries must be an integer >= 100")

        tile_proxy = self.config.get("tile_proxy", {})
        if not isinstance(tile_proxy, dict):
            raise ValueError("tile_proxy must be a dict")
        if tile_proxy.get("enabled"):
            max_mb = tile_proxy.get("max_cache_mb", 500)
            if not isinstance(max_mb, (int, float)) or max_mb < 10:
                raise ValueError("tile_proxy.max_cache_mb must be >= 10")
            timeout = tile_proxy.get("request_timeout", 10)
            if not isinstance(timeout, (int, float)) or timeout < 1:
                raise ValueError("tile_proxy.request_timeout must be >= 1")
            max_tile_kb = tile_proxy.get("max_tile_kb", 512)
            if not isinstance(max_tile_kb, int) or not 16 <= max_tile_kb <= 4096:
                raise ValueError("tile_proxy.max_tile_kb must be an integer between 16 and 4096")
            prefetch = tile_proxy.get("prefetch", {})
            if not isinstance(prefetch, dict):
                raise ValueError("tile_proxy.prefetch must be a dict")
            min_zoom = prefetch.get("min_zoom", 6)
            max_zoom = prefetch.get("max_zoom", 15)
            if (
                not isinstance(min_zoom, int)
                or not isinstance(max_zoom, int)
                or not 0 <= min_zoom <= max_zoom <= 19
            ):
                raise ValueError(
                    "tile_proxy.prefetch zooms must satisfy 0 <= min_zoom <= max_zoom <= 19"
                )
            bbox = prefetch.get("bbox")
            if bbox is not None:
                if not isinstance(bbox, list) or len(bbox) != 4:
                    raise ValueError("tile_proxy.prefetch.bbox must be [south, west, north, east]")
                if not all(isinstance(v, (int, float)) for v in bbox):
                    raise ValueError("tile_proxy.prefetch.bbox values must be numbers")
                south, west, north, east = bbox
                if not (-90 <= south < north <= 90):
                    raise ValueError("tile_proxy.prefetch.bbox: need -90 <= south < north <= 90")
                if not (-180 <= west < east <= 180):
                    raise ValueError("tile_proxy.prefetch.bbox: need -180 <= west < east <= 180")

        lora_region = self.config.get("lora_region", "US")
        if not isinstance(lora_region, str) or lora_region not in {
            "US",
            "EU_868",
            "EU_433",
            "CN",
            "JP",
            "ANZ",
        }:
            raise ValueError("lora_region must be one of: US, EU_868, EU_433, CN, JP, ANZ")

    def start(self) -> None:
        self._set_dashboard_readiness(False)
        self._active = True
        self._host = self.config.get("host", "127.0.0.1")
        self._port = self.config.get("port", 8080)
        self._start_monotonic = time.monotonic()

        # Import here so aiohttp is only required when the plugin is enabled
        from reticulumpi.builtin_plugins.web_dashboard.auth import (
            AuthManager,
            load_or_create_password_hash,
        )
        from reticulumpi.builtin_plugins.web_dashboard.server import create_app

        # Resolve password: env var > config password_hash > config password > auto-generate
        password_hash = os.environ.get("RETICULUMPI_DASHBOARD_PASSWORD_HASH") or self.config.get(
            "password_hash"
        )
        plaintext_password = os.environ.get("RETICULUMPI_DASHBOARD_PASSWORD") or self.config.get(
            "password"
        )
        generated_password = None
        managed_password_hash_file: str | None = None

        if password_hash:
            source = (
                "environment" if os.environ.get("RETICULUMPI_DASHBOARD_PASSWORD_HASH") else "config"
            )
            self.log.info("Using dashboard password hash from %s", source)
        elif plaintext_password:
            pass  # handled below

        generated_pw_file: str | None = None
        if not password_hash and not plaintext_password:
            secret_dir = self.config.get("secret_dir", "~/.config/reticulumpi")
            secret_dir_resolved = os.path.expanduser(secret_dir)
            managed_password_hash_file = os.path.join(secret_dir_resolved, "dashboard_secret")
            password_hash, generated_password = load_or_create_password_hash(secret_dir)
            pw_file = os.path.join(secret_dir_resolved, "dashboard_password.txt")
            if generated_password:
                self.log.warning(
                    "Generated a new dashboard password. Read it from %s; "
                    "change it in the dashboard before this file is removed.",
                    pw_file,
                )
            if os.path.isfile(pw_file):
                os.chmod(pw_file, 0o600)
                generated_pw_file = pw_file
        elif plaintext_password and not password_hash:
            self.log.critical(
                "INSECURE: Dashboard password is stored in plaintext "
                "(env var or config). Generate a hash with "
                "'reticulumpi --hash-password' and set password_hash "
                "instead."
            )

        # Session persistence — store sessions in SQLite so they survive restarts
        secret_dir_resolved = os.path.expanduser(
            self.config.get("secret_dir", "~/.config/reticulumpi")
        )
        session_db = os.path.join(secret_dir_resolved, "sessions.db")

        rate_limit = self.config.get("rate_limit", {})
        self._auth = AuthManager(
            password_hash=password_hash,
            plaintext_password=plaintext_password,
            session_timeout=self.config.get("session_timeout", 86400),
            max_sessions=self.config.get("max_sessions", 10),
            session_db_path=session_db,
            rate_limit_max_attempts=rate_limit.get("max_attempts", 5),
            rate_limit_window=rate_limit.get("window_seconds", 60),
            force_secure_cookie=self.config.get("force_secure_cookie", False),
            generated_pw_file=generated_pw_file,
            password_hash_file=managed_password_hash_file,
        )
        self._auth_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="dashboard-auth",
        )
        self._auth_slots = threading.BoundedSemaphore(4)

        # The legacy anonymous localhost bypass is intentionally migrated to
        # the scoped token model.  Existing configurations remain enabled but
        # no request is accepted without possession of the token file.
        local_api = self.config.get("local_api")
        if not isinstance(local_api, dict):
            local_api = {}
        if self.config.get("allow_localhost_api", False):
            self.log.warning(
                "allow_localhost_api is deprecated; using token-authenticated "
                "read-only local_api access"
            )
            local_api = {**local_api, "enabled": True}
            self.config["local_api"] = local_api
        if "allow_localhost_send" in self.config:
            self.log.warning(
                "allow_localhost_send is deprecated and ignored; message sends require "
                "an authenticated dashboard session"
            )
        self._local_api_token = self._load_or_create_local_api_token(local_api)

        ssl_ctx = self._setup_ssl()

        self._aiohttp_app = create_app(self)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runner = None
        self._site = None
        self._ssl_ctx = ssl_ctx
        self._tls_maintenance_task = None
        self._server_ready = threading.Event()
        self._server_error: BaseException | None = None

        self._start_thread(self._run_server, "web-dashboard")
        if not self._server_ready.wait(float(self.config.get("startup_timeout", 15))):
            self.stop()
            raise RuntimeError("Web dashboard did not become ready before startup timeout")
        if self._server_error is not None:
            server_error = self._server_error
            self.stop()
            raise RuntimeError("Web dashboard failed to bind") from server_error

        scheme = "https" if ssl_ctx else "http"
        self.log.info("Web dashboard starting on %s://%s:%d", scheme, self._host, self._port)

        if self._host != "127.0.0.1" and not ssl_ctx:
            self.log.warning(
                "Dashboard is accessible over the network without encryption. "
                "Consider enabling SSL in the web_dashboard config."
            )

        self._mdns_proc: subprocess.Popen | None = None
        if self._host != "127.0.0.1" and shutil.which("avahi-publish-service"):
            mdns_service_type = "_https._tcp" if self._ssl_ctx else "_http._tcp"
            try:
                self._mdns_proc = subprocess.Popen(
                    [
                        "avahi-publish-service",
                        "ReticulumPi Dashboard",
                        mdns_service_type,
                        str(self._port),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                import socket

                self.log.info(
                    "mDNS: dashboard advertised at %s://%s.local:%d",
                    scheme,
                    socket.gethostname(),
                    self._port,
                )
            except OSError:
                self.log.warning("Failed to start mDNS advertisement")

    def stop(self) -> None:
        self._set_dashboard_readiness(False)
        self._active = False
        mdns_proc = getattr(self, "_mdns_proc", None)
        if mdns_proc:
            mdns_proc.terminate()
            try:
                mdns_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                mdns_proc.kill()
                try:
                    mdns_proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.log.warning("mDNS publisher did not exit after SIGKILL")
            self._mdns_proc = None
        if getattr(self, "_loop", None) and getattr(self, "_runner", None):
            future = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
            try:
                future.result(timeout=10)
            except Exception:
                self.log.exception("Error during web dashboard shutdown")
        if getattr(self, "_loop", None):
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._join_threads(timeout=10)
        if hasattr(self, "_auth") and hasattr(self._auth.sessions, "close"):
            self._auth.sessions.close()
        if getattr(self, "_auth_executor", None) is not None:
            self._auth_executor.shutdown(wait=False, cancel_futures=True)
            self._auth_executor = None

    def get_status(self) -> dict[str, Any]:
        ssl_config = self.config.get("ssl", {})
        scheme = "https" if ssl_config.get("enabled") else "http"
        host = getattr(self, "_host", self.config.get("host", "127.0.0.1"))
        port = getattr(self, "_port", self.config.get("port", 8080))
        now = time.monotonic()
        started = getattr(self, "_start_monotonic", now)
        return {
            "active": self._active,
            "host": host,
            "port": port,
            "web_url": f"{scheme}://{host}:{port}",
            "uptime": max(0.0, now - started) if self._active else 0,
            "active_sessions": len(self._auth.sessions) if hasattr(self, "_auth") else 0,
            "tls": {
                "enabled": bool(ssl_config.get("enabled", False)),
                "managed": bool(getattr(self, "_tls_managed", False)),
                "state": getattr(self, "_tls_state", "disabled"),
                "last_check": getattr(self, "_tls_last_check", None),
                "last_renewal": getattr(self, "_tls_last_renewal", None),
                "reason": getattr(self, "_tls_last_error", None),
            },
            "_lifecycle": self.get_lifecycle_status(),
        }

    def _run_server(self) -> None:
        """Run the aiohttp server in a dedicated asyncio event loop."""

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        try:
            self._loop.run_until_complete(self._start_server())
            self._loop.run_forever()
        except Exception as exc:
            self._server_error = exc
            self._active = False
            self._set_dashboard_readiness(False)
            if hasattr(self, "_server_ready"):
                self._server_ready.set()
            self.log.exception("Web dashboard server error")
        finally:
            self._set_dashboard_readiness(False)
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    async def _start_server(self) -> None:
        import os

        import aiohttp.web

        self._runner = aiohttp.web.AppRunner(self._aiohttp_app)
        await self._runner.setup()
        self._site = aiohttp.web.TCPSite(
            self._runner,
            self._host,
            self._port,
            ssl_context=self._ssl_ctx,
        )
        await self._site.start()
        self.log.info("Web dashboard listening on %s:%d", self._host, self._port)

        # Tile proxy HTTP client session
        self._tile_session = None
        tp = self.config.get("tile_proxy", {})
        if tp.get("enabled"):
            import aiohttp

            cache_dir = os.path.expanduser(tp.get("cache_dir", runtime_cache_path("tile_cache")))
            os.makedirs(cache_dir, exist_ok=True)
            self._tile_cache_dir = cache_dir
            ua = tp.get("user_agent", "reticulumpi/0.2 tile-proxy")
            timeout = aiohttp.ClientTimeout(total=tp.get("request_timeout", 10))
            self._tile_session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=6),
                timeout=timeout,
                headers={"User-Agent": ua},
            )
            self._prefetch_session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=2),
                timeout=aiohttp.ClientTimeout(total=5),
                headers={"User-Agent": ua},
            )
            self._tile_upstream = tp.get(
                "upstream_url",
                "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
            )
            self._tile_max_bytes = int(tp.get("max_cache_mb", 500)) * 1024 * 1024
            self._tile_max_tile_bytes = int(tp.get("max_tile_kb", 512)) * 1024
            self._tile_cache_bytes = 0
            self._tile_cache_lock = asyncio.Lock()
            self._tile_locks: dict[str, object] = {}
            from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import (
                enforce_tile_cache_budget,
            )

            if not await enforce_tile_cache_budget(self):
                self.log.warning("Tile cache remains over budget after eviction failures")
            self.log.info(
                "Tile proxy enabled, cache: %s (%d MB / %d MB)",
                cache_dir,
                self._tile_cache_bytes // (1024 * 1024),
                tp.get("max_cache_mb", 500),
            )

        # Start periodic session garbage collection
        self._session_gc_task = asyncio.ensure_future(self._session_gc_loop())
        if self._ssl_ctx is not None:
            self._tls_maintenance_task = asyncio.create_task(
                self._tls_maintenance_loop(),
                name="dashboard-tls-maintenance",
            )

        # Tile prefetch (delayed to let GPS settle)
        if tp.get("enabled") and tp.get("prefetch", {}).get("enabled"):

            async def _delayed_prefetch():
                try:
                    await asyncio.sleep(30)
                    from reticulumpi.builtin_plugins.web_dashboard.tile_prefetch import run_prefetch

                    await run_prefetch(self)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.log.exception("Tile prefetch failed")

            self._prefetch_task = asyncio.ensure_future(_delayed_prefetch())

        self._publish_dashboard_ready()

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
            # Truncate the sessions WAL so it doesn't grow unbounded on a tiny
            # DB (a 12KB store had accreted a 4.1MB WAL). Best-effort.
            checkpoint = getattr(self._auth.sessions, "checkpoint", None)
            if checkpoint is not None:
                try:
                    checkpoint()
                except Exception:
                    self.log.debug("Session WAL checkpoint failed", exc_info=True)

    async def _shutdown(self) -> None:
        restart_tasks = list(getattr(self, "_restart_tasks", set()))
        for task in restart_tasks:
            task.cancel()
        if restart_tasks:
            await asyncio.gather(*restart_tasks, return_exceptions=True)
            self._restart_tasks.clear()
        if hasattr(self, "_session_gc_task") and self._session_gc_task:
            self._session_gc_task.cancel()
            try:
                await self._session_gc_task
            except asyncio.CancelledError:
                pass
        if getattr(self, "_tls_maintenance_task", None):
            self._tls_maintenance_task.cancel()
            try:
                await self._tls_maintenance_task
            except asyncio.CancelledError:
                pass
            self._tls_maintenance_task = None
        if getattr(self, "_prefetch_task", None) and not self._prefetch_task.done():
            self._prefetch_task.cancel()
            try:
                await self._prefetch_task
            except asyncio.CancelledError:
                pass
        if getattr(self, "_prefetch_session", None):
            await self._prefetch_session.close()
            self._prefetch_session = None
        if getattr(self, "_tile_session", None):
            await self._tile_session.close()
            self._tile_session = None
        if hasattr(self, "_tile_locks"):
            self._tile_locks.clear()
        if self._runner:
            await self._runner.cleanup()
            self._site = None
        # Drain lingering tasks (e.g. aiohttp's compressed-frame writer
        # offloaded to the executor) so asyncio doesn't log
        # "Task was destroyed but it is pending" when the loop stops.
        pending = [
            t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()
        ]
        if pending:
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    def _setup_ssl(self):
        """Configure validated managed or operator-supplied TLS material."""
        ssl_config = self.config.get("ssl", {})
        self._tls_managed = False
        self._tls_state = "disabled"
        self._tls_last_check = None
        self._tls_last_renewal = None
        self._tls_last_error = None
        self._tls_degraded = False
        self._tls_failed_closed = False
        self._tls_cert_file = None
        self._tls_key_file = None
        self._tls_required_sans: list[str] = []
        self._tls_expires_at = None
        if not ssl_config.get("enabled", False):
            return None

        cert_file = ssl_config.get("cert_file")
        key_file = ssl_config.get("key_file")
        auto_generate = ssl_config.get("auto_generate", False)
        extra_hostnames = ssl_config.get("extra_hostnames", [])
        common_name = self.app.config.node_name

        # Any explicit path makes the pair operator-owned.  Incomplete or
        # invalid operator material fails closed and is never replaced by the
        # managed generator, even when auto_generate is also true.
        if cert_file or key_file:
            if not cert_file or not key_file:
                raise ValueError("SSL operator cert_file/key_file must both be provided")
            required_sans = list(extra_hostnames)
        elif auto_generate:
            from reticulumpi.builtin_plugins.web_dashboard.ssl_utils import (
                _collect_san_strings,
                generate_self_signed_cert,
            )

            self._tls_managed = True
            self._tls_cert_dir = os.path.expanduser(
                ssl_config.get("cert_dir", "~/.config/reticulumpi/web_certs")
            )
            self._tls_common_name = common_name
            self._tls_extra_hostnames = list(extra_hostnames)
            required_sans = _collect_san_strings(extra_hostnames)
            cert_file, key_file = generate_self_signed_cert(
                self._tls_cert_dir,
                common_name,
                self.log,
                extra_sans=extra_hostnames,
            )
        else:
            raise ValueError(
                "SSL enabled but no cert_file/key_file provided and auto_generate is false"
            )

        from reticulumpi.builtin_plugins.web_dashboard.ssl_utils import validate_cert_pair

        cert_file = os.path.expanduser(cert_file)
        key_file = os.path.expanduser(key_file)
        self._tls_expires_at = validate_cert_pair(
            cert_file,
            key_file,
            required_sans=required_sans,
            min_valid_days=1,
        )
        self._tls_cert_file = cert_file
        self._tls_key_file = key_file
        self._tls_required_sans = required_sans
        self._tls_state = "valid"
        return self._build_ssl_context(cert_file, key_file)

    @staticmethod
    def _build_ssl_context(cert_file: str, key_file: str):
        import ssl

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_file, key_file)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx

    def _tls_check_delay(self, *, now=None) -> float:
        """Return a bounded delay that reaches the relevant expiry guard in time."""

        from reticulumpi.builtin_plugins.web_dashboard.ssl_utils import _normalise_now

        expiry = getattr(self, "_tls_expires_at", None)
        if expiry is None:
            return float(self.TLS_CHECK_INTERVAL_SECONDS)
        guard = (
            self.TLS_MANAGED_RENEWAL_GUARD_SECONDS
            if getattr(self, "_tls_managed", False)
            else self.TLS_OPERATOR_VALIDITY_GUARD_SECONDS
        )
        until_guard = (expiry - _normalise_now(now)).total_seconds() - guard
        return max(
            float(self.TLS_MIN_CHECK_INTERVAL_SECONDS),
            min(float(self.TLS_CHECK_INTERVAL_SECONDS), until_guard),
        )

    async def _tls_maintenance_loop(self) -> None:
        """Validate TLS material daily or sooner when an expiry guard approaches."""
        while self._active and not getattr(self, "_tls_failed_closed", False):
            await asyncio.sleep(self._tls_check_delay())
            if not self._active:
                break
            try:
                await self._check_tls_certificate()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("Unexpected dashboard TLS maintenance failure")
                self._record_tls_degraded("unexpected TLS maintenance failure")

    async def _check_tls_certificate(self, *, now=None) -> bool:
        """Validate current material and renew a managed bundle when needed.

        Returns true only when a managed certificate was renewed and loaded.
        ``now`` is injectable so expiry boundaries are deterministic in tests.
        """
        if not self._ssl_ctx or not self._tls_cert_file or not self._tls_key_file:
            return False
        if self._tls_failed_closed:
            return False

        from reticulumpi.builtin_plugins.web_dashboard.ssl_utils import (
            _normalise_now,
            validate_cert_pair,
        )

        current_time = _normalise_now(now)
        self._tls_last_check = current_time.isoformat()
        if not self._tls_managed:
            try:
                self._tls_expires_at = validate_cert_pair(
                    self._tls_cert_file,
                    self._tls_key_file,
                    required_sans=self._tls_required_sans,
                    min_valid_days=1,
                    now=current_time,
                )
            except ValueError as exc:
                await self._fail_closed_tls_listener(
                    f"operator-supplied TLS material is invalid: {exc}"
                )
            else:
                self._record_tls_valid("valid")
            return False

        from reticulumpi.builtin_plugins.web_dashboard.ssl_utils import _collect_san_strings

        # Hostname/address discovery can change while the Pi moves between
        # networks. Treat the current managed SAN set as part of daily validity.
        self._tls_required_sans = _collect_san_strings(self._tls_extra_hostnames)
        try:
            self._tls_expires_at = validate_cert_pair(
                self._tls_cert_file,
                self._tls_key_file,
                required_sans=self._tls_required_sans,
                min_valid_days=30,
                now=current_time,
            )
        except ValueError as exc:
            return await self._renew_managed_tls(current_time, str(exc))

        self._record_tls_valid("valid")
        return False

    async def _renew_managed_tls(self, now, renewal_reason: str) -> bool:
        """Atomically replace the managed bundle and reload future handshakes."""
        from reticulumpi.builtin_plugins.web_dashboard.ssl_utils import (
            _atomic_write,
            generate_self_signed_cert,
            validate_cert_pair,
        )

        if self._tls_cert_file != self._tls_key_file:
            await self._fail_closed_tls_listener(
                "managed TLS material is not using the atomic bundle layout"
            )
            return False

        old_bundle: bytes | None = None
        old_is_valid = False
        old_expiry = None
        try:
            with open(self._tls_cert_file, "rb") as bundle_file:
                old_bundle = bundle_file.read()
            old_expiry = validate_cert_pair(
                self._tls_cert_file,
                self._tls_key_file,
                required_sans=self._tls_required_sans,
                min_valid_days=0,
                now=now,
            )
            old_is_valid = True
        except (OSError, ValueError):
            pass

        active_reload_attempted = False
        try:
            cert_file, key_file = generate_self_signed_cert(
                self._tls_cert_dir,
                self._tls_common_name,
                self.log,
                extra_sans=self._tls_extra_hostnames,
                now=now,
            )
            fresh_expiry = validate_cert_pair(
                cert_file,
                key_file,
                required_sans=self._tls_required_sans,
                min_valid_days=30,
                now=now,
            )
            # Prove a fresh context can parse the newly published bundle before
            # mutating the live context used by accepted connections.
            self._build_ssl_context(cert_file, key_file)
            active_reload_attempted = True
            self._ssl_ctx.load_cert_chain(cert_file, key_file)
        except Exception as exc:
            if old_bundle is not None:
                try:
                    _atomic_write(self._tls_cert_file, old_bundle, 0o600)
                    if active_reload_attempted:
                        self._ssl_ctx.load_cert_chain(
                            self._tls_cert_file,
                            self._tls_key_file,
                        )
                except Exception:
                    self.log.exception("Could not restore previous managed TLS bundle")
                    old_is_valid = False
            reason = f"managed TLS renewal failed ({renewal_reason}): {exc}"
            if old_is_valid:
                self._tls_expires_at = old_expiry
                self.log.exception(reason)
                self._record_tls_degraded(reason)
            else:
                await self._fail_closed_tls_listener(reason)
            return False

        self._tls_cert_file = cert_file
        self._tls_key_file = key_file
        self._tls_expires_at = fresh_expiry
        self._tls_last_renewal = now.isoformat()
        self._record_tls_valid("renewed")
        self.log.info("Reloaded renewed dashboard TLS certificate for new connections")
        return True

    def _record_tls_valid(self, state: str) -> None:
        was_degraded = self._tls_degraded
        self._tls_state = state
        self._tls_last_error = None
        self._tls_degraded = False
        if was_degraded and self._active and self.plugin_state.value == "ready":
            self.mark_ready()

    def _record_tls_degraded(self, reason: str) -> None:
        self._tls_state = "degraded"
        self._tls_last_error = reason
        self._tls_degraded = True
        self.mark_degraded(reason)

    async def _fail_closed_tls_listener(self, reason: str) -> None:
        self._tls_state = "failed_closed"
        self._tls_last_error = reason
        self._tls_degraded = True
        self._tls_failed_closed = True
        self._set_dashboard_readiness(False)
        self.mark_degraded(reason)
        site = getattr(self, "_site", None)
        if site is not None:
            try:
                await site.stop()
            except Exception:
                self.log.exception("TLS listener stop failed")
            finally:
                self._site = None
        # AppRunner cleanup also closes already-accepted connections; stopping
        # only the listening socket would not be a complete fail-closed state.
        runner = getattr(self, "_runner", None)
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception:
                self.log.exception("Dashboard runner cleanup failed during TLS fail-close")
            finally:
                self._runner = None
        self.log.critical("Dashboard TLS listener failed closed: %s", reason)

    def _load_or_create_local_api_token(self, local_api: dict[str, Any]) -> str | None:
        """Rotate the scoped loopback API token in a mode-0600 runtime file."""
        if not local_api.get("enabled", False):
            return None

        from reticulumpi.builtin_plugins.web_dashboard.auth import write_secret_file_atomic

        configured_path = local_api.get("token_file")
        if configured_path:
            token_path = os.path.expanduser(configured_path)
        else:
            runtime_dir = os.environ.get("RETICULUMPI_RUNTIME_DIR", "/run/reticulumpi")
            if os.path.isdir(runtime_dir) and os.access(runtime_dir, os.W_OK):
                token_path = os.path.join(runtime_dir, "local_api.token")
            else:
                secret_dir = os.path.expanduser(
                    self.config.get("secret_dir", "~/.config/reticulumpi")
                )
                token_path = os.path.join(secret_dir, "local_api.token")
                self.log.warning(
                    "Runtime directory %s is unavailable; local API token is using %s",
                    runtime_dir,
                    token_path,
                )

        token = secrets.token_urlsafe(32)
        write_secret_file_atomic(token_path, token + "\n")
        self._local_api_token_path = token_path
        self.log.info("Rotated scoped local API token: %s", token_path)
        return token
