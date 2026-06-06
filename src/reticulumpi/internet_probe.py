"""Lightweight periodic internet connectivity probe.

Runs TCP connect checks against configurable targets (default: DNS anycast
IPs on port 53).  Publishes INTERNET_ONLINE / INTERNET_OFFLINE events on
state transitions.  Uses asymmetric hysteresis: multiple consecutive
failures required to go offline, single success to recover.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from reticulumpi import events
from reticulumpi.event_bus import EventBus

log = logging.getLogger(__name__)

_DEFAULT_TARGETS = [
    {"host": "1.1.1.1", "port": 53},
    {"host": "8.8.8.8", "port": 53},
    {"host": "9.9.9.9", "port": 53},
]


class InternetProbe:
    """Periodic internet connectivity probe with hysteresis.

    The probe runs lightweight TCP connect checks (SYN/ACK only, no data)
    against well-known anycast IPs.  Any single target succeeding counts
    as "online".

    Hysteresis prevents false transitions from brief network blips:
    - Online → Offline: requires ``offline_threshold`` consecutive failures
    - Offline → Online: requires a single success
    """

    def __init__(self, event_bus: EventBus, config: dict[str, Any]) -> None:
        self._event_bus = event_bus
        self._force_offline: bool = config.get("force_offline", False)
        self._interval: float = max(5.0, float(config.get("probe_interval", 30)))
        self._timeout: float = max(1.0, float(config.get("probe_timeout", 3)))
        self._offline_threshold: int = max(1, int(config.get("offline_threshold", 3)))
        self._targets: list[dict[str, Any]] = config.get("targets") or _DEFAULT_TARGETS

        self._lock = threading.Lock()
        self._is_online: bool | None = None
        self._consecutive_failures: int = 0
        self._wan_ip: str | None = None
        self._lan_ip: str | None = None
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def is_online(self) -> bool:
        with self._lock:
            return bool(self._is_online)

    @property
    def force_offline(self) -> bool:
        with self._lock:
            return self._force_offline

    @property
    def wan_ip(self) -> str | None:
        with self._lock:
            return self._wan_ip

    @property
    def lan_ip(self) -> str | None:
        with self._lock:
            return self._lan_ip

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "online": bool(self._is_online),
                "wan_ip": self._wan_ip,
                "lan_ip": self._lan_ip,
                "force_offline": self._force_offline,
            }

    def set_force_offline(self, enabled: bool) -> None:
        """Toggle forced-offline mode at runtime.

        When enabling, bypasses hysteresis for immediate offline transition.
        When disabling, wakes the monitor loop to run a real connectivity
        check rather than blocking the caller for up to 9s of TCP probes.
        """
        with self._lock:
            self._force_offline = enabled
            self._consecutive_failures = 0
        if enabled:
            self._set_state(False)
        else:
            self._wake_event.set()

    def probe_once(self) -> bool:
        """Run a single synchronous connectivity check.

        Returns True if any target is reachable.
        """
        if self._force_offline:
            return False

        for target in self._targets:
            host = target.get("host", "")
            port = int(target.get("port", 53))
            try:
                sock = socket.create_connection((host, port), timeout=self._timeout)
                sock.close()
                return True
            except (OSError, socket.timeout):
                continue
        return False

    @staticmethod
    def _detect_lan_ip() -> str | None:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1.0)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip if ip and ip != "0.0.0.0" else None
        except (OSError, socket.timeout):
            return None

    @staticmethod
    def _detect_wan_ip() -> str | None:
        try:
            req = urllib.request.Request(
                "https://api.ipify.org",
                headers={"User-Agent": "ReticulumPi"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                ip = resp.read().decode("ascii", errors="ignore").strip()
                return ip if ip else None
        except (urllib.error.URLError, OSError, TimeoutError):
            return None

    def start(self) -> None:
        """Set initial state and start the background monitoring thread."""
        initial = self.probe_once()
        self._set_state(initial)
        if initial:
            log.info("Internet probe: online (%d target(s))", len(self._targets))
        else:
            log.warning("Internet probe: offline (force=%s)", self._force_offline)

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="internet-probe",
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the monitoring thread."""
        self._stop_event.set()
        self._wake_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=self._interval)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            self._run_check()

    def _run_check(self) -> None:
        reachable = self.probe_once()

        # Hold lock through the entire state-transition decision so
        # concurrent set_force_offline() calls cannot slip between
        # reading was_online and deciding to transition.
        with self._lock:
            was_online = self._is_online
            force = self._force_offline
            if reachable:
                self._consecutive_failures = 0
                consecutive = 0
            else:
                self._consecutive_failures += 1
                consecutive = self._consecutive_failures

            should_go_online = reachable and not force and not was_online
            should_go_offline = (
                not reachable and was_online and consecutive >= self._offline_threshold
            )

        if should_go_online:
            self._set_state(True)
            log.info("Internet connectivity restored")
        elif should_go_offline:
            self._set_state(False)
            log.warning(
                "Internet connectivity lost (%d consecutive failures)",
                consecutive,
            )

    def _set_state(self, online: bool) -> None:
        lan_ip = self._detect_lan_ip()
        wan_ip = self._detect_wan_ip() if online else None

        with self._lock:
            if self._is_online == online:
                return
            self._is_online = online
            self._lan_ip = lan_ip
            self._wan_ip = wan_ip

        event_type = events.INTERNET_ONLINE if online else events.INTERNET_OFFLINE
        self._event_bus.publish(
            event_type,
            {
                "timestamp": time.time(),
                "wan_ip": wan_ip,
                "lan_ip": lan_ip,
            },
        )
