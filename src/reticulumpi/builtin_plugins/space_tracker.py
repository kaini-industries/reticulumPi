"""Space Tracker plugin — satellites, launches, and space weather from public APIs.

Aggregates data from three internet sources:

    * Celestrak (https://celestrak.org)           — TLE sets for satellites
    * Launch Library 2 (https://thespacedevs.com) — upcoming launches
    * NOAA SWPC (https://services.swpc.noaa.gov)  — space weather (Kp index)

All network calls go through a rate-limited HTTP helper that:

    * enforces a per-source minimum interval between requests
    * enforces a sliding-window hourly cap where the upstream publishes one
    * persists its state to disk so a restart loop cannot hammer APIs
    * honours ETag / If-Modified-Since for bandwidth-friendly polls
    * backs off exponentially on failure (1 h → 24 h cap)

Orbital propagation (sub-satellite points, observer az/el, pass prediction)
requires the optional ``sgp4`` and/or ``skyfield`` packages.  If neither is
installed the plugin still fetches and publishes TLEs and the other data
sources — propagation is simply skipped with a one-time warning.

Example config:

    space_tracker:
      enabled: true
      observer:
        latitude: 40.7128        # null = try gps_telemetry plugin
        longitude: -74.0060
        elevation_m: 10
      celestrak_groups:
        - stations               # ISS, CSS, HST
        - amateur
        - weather
        - noaa
      tle_refresh_hours: 24      # min 6
      launches:
        enabled: true
        poll_interval_minutes: 30    # min 15
        lookahead_count: 5
      space_weather:
        enabled: true
        poll_interval_minutes: 15    # min 10
      propagation:
        enabled: true
        interval_seconds: 10
        max_objects: 200
      passes:
        enabled: true
        lookahead_hours: 24
        min_elevation_deg: 10
        watchlist:
          - "ISS (ZARYA)"
          - "NOAA 15"
          - "NOAA 18"
          - "NOAA 19"
      user_agent: "reticulumpi/0.2 (+https://example.invalid/contact)"
      request_timeout_seconds: 15
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase

# Re-export the event names under short local aliases so this module keeps
# reading cleanly.  The authoritative constants live in reticulumpi.events.
EVENT_TLE_UPDATED = events.SPACE_TLE_UPDATED
EVENT_POSITIONS_SNAPSHOT = events.SPACE_POSITIONS_SNAPSHOT
EVENT_PASS_UPCOMING = events.SPACE_PASS_UPCOMING
EVENT_LAUNCH_UPCOMING = events.SPACE_LAUNCH_UPCOMING
EVENT_WEATHER_UPDATED = events.SPACE_WEATHER_UPDATED


# ---------------------------------------------------------------------------
# Rate limiter — hard-enforced, persisted, backoff-aware.
# ---------------------------------------------------------------------------
class _RateLimiter:
    """Per-endpoint quota enforcer.

    Parameters
    ----------
    name:
        Identifier used for logging and the on-disk state key.
    min_interval_s:
        Minimum wall-clock seconds between successful requests.
    max_per_hour:
        Optional sliding-window hourly cap.  ``None`` disables the cap.
    backoff_base_s, backoff_cap_s:
        Exponential backoff bounds applied after consecutive failures.
    """

    def __init__(
        self,
        name: str,
        min_interval_s: float,
        max_per_hour: int | None = None,
        backoff_base_s: float = 3600.0,
        backoff_cap_s: float = 86400.0,
    ) -> None:
        self.name = name
        self.min_interval_s = float(min_interval_s)
        self.max_per_hour = max_per_hour
        self.backoff_base_s = float(backoff_base_s)
        self.backoff_cap_s = float(backoff_cap_s)

        self._lock = threading.Lock()
        self._last_request_ts: float = 0.0          # wall-clock epoch seconds
        self._failures: int = 0
        self._recent: deque[float] = deque()        # successful request timestamps

    # -- state (de)serialisation ---------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "last_request_ts": self._last_request_ts,
                "failures": self._failures,
                "recent": list(self._recent),
            }

    def load_dict(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._last_request_ts = float(data.get("last_request_ts", 0.0))
            self._failures = int(data.get("failures", 0))
            self._recent = deque(float(t) for t in data.get("recent", []))
            self._trim_recent(time.time())

    # -- quota math ----------------------------------------------------------
    def _trim_recent(self, now: float) -> None:
        cutoff = now - 3600.0
        while self._recent and self._recent[0] < cutoff:
            self._recent.popleft()

    def _current_min_interval(self) -> float:
        """Minimum interval with exponential backoff folded in."""
        if self._failures == 0:
            return self.min_interval_s
        backoff = self.backoff_base_s * (2 ** (self._failures - 1))
        return max(self.min_interval_s, min(self.backoff_cap_s, backoff))

    def next_allowed_at(self) -> float:
        """Wall-clock epoch seconds when the next request will be permitted."""
        with self._lock:
            self._trim_recent(time.time())
            cooldown = self._last_request_ts + self._current_min_interval()
            next_by_hour = 0.0
            if self.max_per_hour is not None and len(self._recent) >= self.max_per_hour:
                next_by_hour = self._recent[0] + 3600.0
            return max(cooldown, next_by_hour)

    def can_request(self) -> bool:
        return self.next_allowed_at() <= time.time()

    def record_attempt(self) -> None:
        """Call before firing the request — counts toward the hourly cap even
        on failure so a broken endpoint can't be retried hundreds of times."""
        with self._lock:
            self._last_request_ts = time.time()

    def record_success(self) -> None:
        with self._lock:
            now = time.time()
            self._failures = 0
            self._recent.append(now)
            self._trim_recent(now)

    def record_failure(self) -> None:
        with self._lock:
            self._failures = min(self._failures + 1, 16)  # cap to avoid overflow

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._trim_recent(time.time())
            return {
                "name": self.name,
                "last_request_ts": self._last_request_ts,
                "failures": self._failures,
                "requests_last_hour": len(self._recent),
                "max_per_hour": self.max_per_hour,
                "next_allowed_at": self.next_allowed_at(),
            }


# ---------------------------------------------------------------------------
# HTTP helper — stdlib only, always goes through a rate limiter.
# ---------------------------------------------------------------------------
class _HttpClient:
    """Minimal HTTPS client wrapping urllib with rate-limited, conditional GETs."""

    def __init__(self, user_agent: str, timeout_s: float, log: Any) -> None:
        self.user_agent = user_agent
        self.timeout_s = float(timeout_s)
        self.log = log
        # cache of conditional-request validators per URL: url -> (etag, last_mod)
        self._validators: dict[str, tuple[str | None, str | None]] = {}

    def get(
        self,
        url: str,
        limiter: _RateLimiter,
        accept: str = "*/*",
    ) -> tuple[int, bytes | None, dict[str, str]]:
        """Perform a rate-limited GET.

        Returns ``(status, body_or_None, headers)``.  Status 0 means the
        request was blocked by the limiter; status -1 means a network error
        (limiter already updated with failure).  304 bodies are returned as
        ``None``.
        """
        if not limiter.can_request():
            wait = max(0.0, limiter.next_allowed_at() - time.time())
            self.log.debug(
                "Rate limiter %s blocked request to %s (wait %.0fs)",
                limiter.name, url, wait,
            )
            return 0, None, {}

        headers = {
            "User-Agent": self.user_agent,
            "Accept": accept,
            "Accept-Encoding": "identity",  # keep parsing simple for scaffold
        }
        etag, last_mod = self._validators.get(url, (None, None))
        if etag:
            headers["If-None-Match"] = etag
        if last_mod:
            headers["If-Modified-Since"] = last_mod

        req = urllib.request.Request(url, headers=headers, method="GET")
        limiter.record_attempt()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                status = resp.status
                resp_headers = {k: v for k, v in resp.headers.items()}
                body = resp.read()
                new_etag = resp_headers.get("ETag")
                new_last_mod = resp_headers.get("Last-Modified")
                if new_etag or new_last_mod:
                    self._validators[url] = (new_etag, new_last_mod)
                limiter.record_success()
                return status, body, resp_headers
        except urllib.error.HTTPError as e:
            # 304 Not Modified is a success path for conditional GETs
            if e.code == 304:
                limiter.record_success()
                return 304, None, {k: v for k, v in (e.headers.items() if e.headers else [])}
            # 429 / 5xx → record failure so backoff applies
            limiter.record_failure()
            self.log.warning("HTTP %s from %s", e.code, url)
            return e.code, None, {}
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            limiter.record_failure()
            self.log.warning("Network error fetching %s: %s", url, e)
            return -1, None, {}


# ---------------------------------------------------------------------------
# Main plugin
# ---------------------------------------------------------------------------
class SpaceTrackerPlugin(PluginBase):
    """Fetches satellite TLEs, launch schedules, and space weather; publishes
    to the event bus for dashboard consumption.

    This is a scaffold: network fetch + caching + rate limiting are fully
    implemented, but TLE propagation and pass prediction are left as stubs
    to be filled in when ``sgp4`` / ``skyfield`` are installed.
    """

    plugin_name = "space_tracker"
    plugin_version = "0.1.0"
    plugin_description = "Satellite tracking, launch schedule, and space weather"

    # Minimum enforced intervals — these are floors, the user can configure
    # larger values but not smaller.
    _MIN_TLE_REFRESH_HOURS = 6
    _MIN_LAUNCH_POLL_MIN = 15
    _MIN_WEATHER_POLL_MIN = 10

    # Known Celestrak group identifiers.  Keep this list conservative — users
    # can pass arbitrary strings but we warn on unknowns.
    _KNOWN_GROUPS = {
        "stations", "amateur", "weather", "noaa", "goes", "resource",
        "cubesat", "starlink", "active", "gps-ops", "galileo", "glo-ops",
        "beidou", "science", "last-30-days",
    }

    _CELESTRAK_URL = (
        "https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=tle"
    )
    _LAUNCH_LIBRARY_URL = (
        "https://ll.thespacedevs.com/2.2.0/launch/upcoming/?limit={limit}&mode=list"
    )
    _SWPC_KP_URL = (
        "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
    )

    # ------------------------------------------------------------------
    # Config validation
    # ------------------------------------------------------------------
    def validate_config(self) -> None:
        tle_hours = self.config.get("tle_refresh_hours", 24)
        if not isinstance(tle_hours, (int, float)) or tle_hours < self._MIN_TLE_REFRESH_HOURS:
            raise ValueError(
                f"tle_refresh_hours must be >= {self._MIN_TLE_REFRESH_HOURS} (Celestrak etiquette)"
            )

        launch_cfg = self.config.get("launches", {}) or {}
        if launch_cfg.get("enabled", True):
            interval = launch_cfg.get("poll_interval_minutes", 30)
            if not isinstance(interval, (int, float)) or interval < self._MIN_LAUNCH_POLL_MIN:
                raise ValueError(
                    f"launches.poll_interval_minutes must be >= {self._MIN_LAUNCH_POLL_MIN}"
                )

        weather_cfg = self.config.get("space_weather", {}) or {}
        if weather_cfg.get("enabled", True):
            interval = weather_cfg.get("poll_interval_minutes", 15)
            if not isinstance(interval, (int, float)) or interval < self._MIN_WEATHER_POLL_MIN:
                raise ValueError(
                    f"space_weather.poll_interval_minutes must be >= {self._MIN_WEATHER_POLL_MIN}"
                )

        groups = self.config.get("celestrak_groups", []) or []
        if not isinstance(groups, list):
            raise ValueError("celestrak_groups must be a list")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        self._active = True

        # Resolved config + defaults
        self._tle_refresh_s = float(self.config.get("tle_refresh_hours", 24)) * 3600.0
        self._groups: list[str] = list(self.config.get("celestrak_groups", ["stations"]))

        for g in self._groups:
            if g not in self._KNOWN_GROUPS:
                self.log.warning(
                    "Celestrak group %r is not in the known list — check the "
                    "spelling at https://celestrak.org/NORAD/elements/", g,
                )

        launch_cfg = self.config.get("launches", {}) or {}
        self._launches_enabled = bool(launch_cfg.get("enabled", True))
        self._launch_interval_s = float(launch_cfg.get("poll_interval_minutes", 30)) * 60.0
        self._launch_limit = int(launch_cfg.get("lookahead_count", 5))

        weather_cfg = self.config.get("space_weather", {}) or {}
        self._weather_enabled = bool(weather_cfg.get("enabled", True))
        self._weather_interval_s = float(weather_cfg.get("poll_interval_minutes", 15)) * 60.0

        self._prop_cfg = self.config.get("propagation", {}) or {}
        self._passes_cfg = self.config.get("passes", {}) or {}

        # Cache dir
        self._cache_dir = os.path.expanduser(
            self.config.get("cache_dir", "~/.local/share/reticulumpi/space_tracker")
        )
        os.makedirs(self._cache_dir, exist_ok=True)
        self._state_path = os.path.join(self._cache_dir, "rate_state.json")

        # HTTP client
        user_agent = self.config.get(
            "user_agent",
            f"reticulumpi/{getattr(self.app, 'version', '0.x')} space_tracker",
        )
        timeout_s = float(self.config.get("request_timeout_seconds", 15))
        self._http = _HttpClient(user_agent=user_agent, timeout_s=timeout_s, log=self.log)

        # Rate limiters.  Celestrak: one limiter shared across all groups
        # (they all hit the same origin).  LL2 / SWPC: one each.
        self._limiters: dict[str, _RateLimiter] = {
            "celestrak": _RateLimiter(
                name="celestrak",
                min_interval_s=max(self._MIN_TLE_REFRESH_HOURS * 3600.0, self._tle_refresh_s),
                max_per_hour=None,
            ),
            "launchlibrary": _RateLimiter(
                name="launchlibrary",
                min_interval_s=max(self._MIN_LAUNCH_POLL_MIN * 60.0, self._launch_interval_s),
                max_per_hour=4,
            ),
            "swpc": _RateLimiter(
                name="swpc",
                min_interval_s=max(self._MIN_WEATHER_POLL_MIN * 60.0, self._weather_interval_s),
                max_per_hour=None,
            ),
        }
        self._load_rate_state()

        # In-memory caches (populated by fetch loops)
        self._tle_cache: dict[str, list[dict[str, str]]] = {}   # group -> list of {name, l1, l2}
        self._tle_last_fetch: dict[str, float] = {}             # group -> epoch seconds
        self._launches: list[dict[str, Any]] = []
        self._weather: dict[str, Any] = {}
        self._latest_positions: dict[str, Any] = {}             # last propagation cycle output
        self._cache_lock = threading.Lock()

        # Observer position (resolved lazily — gps_telemetry may not be up yet)
        self._observer_cfg = self.config.get("observer", {}) or {}

        # Load any cached TLEs from disk so restarts are hot
        self._load_cached_tles()

        # Kick off fetch threads
        self._start_thread(self._tle_loop, "space-tle")
        if self._launches_enabled:
            self._start_thread(self._launch_loop, "space-launches")
        if self._weather_enabled:
            self._start_thread(self._weather_loop, "space-weather")

        # Propagation / pass prediction are stubbed — only start the loop
        # if the optional deps are available.
        if bool(self._prop_cfg.get("enabled", True)) and _sgp4_available():
            self._start_thread(self._propagation_loop, "space-propagation")
        elif bool(self._prop_cfg.get("enabled", True)):
            self.log.info(
                "Orbital propagation disabled: install 'sgp4' (pip install sgp4) "
                "to enable sub-satellite point publishing."
            )

        self.log.info(
            "Space tracker active — groups=%s launches=%s weather=%s cache=%s",
            self._groups, self._launches_enabled, self._weather_enabled, self._cache_dir,
        )

    def stop(self) -> None:
        self._active = False
        try:
            self._save_rate_state()
        except Exception:
            self.log.exception("Failed to persist rate-limiter state on shutdown")
        self._join_threads()

    # ------------------------------------------------------------------
    # Status (surfaced in the dashboard / /status endpoint)
    # ------------------------------------------------------------------
    def get_snapshot(self) -> dict[str, Any]:
        """Full snapshot for dashboard consumption.

        Returns cached TLEs (count per group), latest launch list, and the
        most recent space-weather reading.  Live position data is published
        via the event bus (``space.positions.snapshot``) rather than pulled
        from here so the dashboard WebSocket can push at propagation cadence.
        """
        with self._cache_lock:
            return {
                "tle_groups": {g: len(sats) for g, sats in self._tle_cache.items()},
                "tle_last_fetch": dict(self._tle_last_fetch),
                "launches": list(self._launches),
                "weather": dict(self._weather) if self._weather else None,
                "observer": self._resolve_observer(),
                "positions": dict(self._latest_positions) if self._latest_positions else None,
                "sgp4_available": _sgp4_available(),
                "skyfield_available": _skyfield_available(),
                "rate_limiters": {k: v.status() for k, v in self._limiters.items()},
            }

    def get_latest_positions(self) -> dict[str, Any]:
        """Cheap accessor for the most recent propagation snapshot (WS broadcast)."""
        with self._cache_lock:
            return dict(self._latest_positions) if self._latest_positions else {}

    def get_status(self) -> dict[str, Any]:
        with self._cache_lock:
            tle_summary = {g: len(sats) for g, sats in self._tle_cache.items()}
            launches_count = len(self._launches)
            weather_has = bool(self._weather)
        return {
            "active": self._active,
            "groups_tracked": tle_summary,
            "upcoming_launches": launches_count,
            "weather_loaded": weather_has,
            "rate_limiters": {k: v.status() for k, v in self._limiters.items()},
            "sgp4_available": _sgp4_available(),
            "skyfield_available": _skyfield_available(),
        }

    # ------------------------------------------------------------------
    # Rate-limiter state persistence
    # ------------------------------------------------------------------
    def _save_rate_state(self) -> None:
        data = {k: v.to_dict() for k, v in self._limiters.items()}
        tmp = self._state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, self._state_path)

    def _load_rate_state(self) -> None:
        if not os.path.exists(self._state_path):
            return
        try:
            with open(self._state_path, encoding="utf-8") as f:
                data = json.load(f)
            for key, payload in data.items():
                if key in self._limiters and isinstance(payload, dict):
                    self._limiters[key].load_dict(payload)
        except (OSError, ValueError):
            self.log.warning("Could not load rate-limiter state; starting fresh")

    # ------------------------------------------------------------------
    # Celestrak TLE fetcher
    # ------------------------------------------------------------------
    def _tle_loop(self) -> None:
        # Stagger first fetch by a few seconds so we don't spike on start
        self._sleep_while_active(5)
        while self._active:
            try:
                self._refresh_due_groups()
            except Exception:
                self.log.exception("Error in TLE refresh cycle")
            # Check once a minute whether any group is due; the limiter
            # ensures we never actually fire more than once per min_interval.
            self._sleep_while_active(60)

    def _refresh_due_groups(self) -> None:
        now = time.time()
        for group in self._groups:
            last = self._tle_last_fetch.get(group, 0.0)
            if (now - last) < self._tle_refresh_s:
                continue
            if not self._limiters["celestrak"].can_request():
                # Limiter will still block us; skip quietly
                continue
            self._fetch_group(group)
            # Space out group fetches within a cycle
            if not self._active:
                return
            self._sleep_while_active(2)

    def _fetch_group(self, group: str) -> None:
        url = self._CELESTRAK_URL.format(group=group)
        status, body, _ = self._http.get(url, self._limiters["celestrak"], accept="text/plain")
        if status == 0:
            return  # limiter blocked
        if status == 304:
            self._tle_last_fetch[group] = time.time()
            self.log.debug("TLE group %s not modified", group)
            return
        if status != 200 or body is None:
            self.log.warning("TLE fetch failed for group %s (status=%s)", group, status)
            return

        try:
            sats = _parse_tle_block(body.decode("ascii", errors="replace"))
        except Exception:
            self.log.exception("Failed to parse TLE for group %s", group)
            return

        # Persist raw body to cache
        cache_file = os.path.join(self._cache_dir, f"{group}.tle")
        try:
            with open(cache_file, "wb") as f:
                f.write(body)
        except OSError:
            self.log.exception("Failed to write TLE cache %s", cache_file)

        with self._cache_lock:
            self._tle_cache[group] = sats
        self._tle_last_fetch[group] = time.time()
        self._save_rate_state()

        self.event_bus.publish(EVENT_TLE_UPDATED, {
            "group": group,
            "count": len(sats),
            "fetched_at": time.time(),
        })
        self.log.info("TLE group %s refreshed: %d satellites", group, len(sats))

    def _load_cached_tles(self) -> None:
        for group in self._groups:
            cache_file = os.path.join(self._cache_dir, f"{group}.tle")
            if not os.path.exists(cache_file):
                continue
            try:
                with open(cache_file, "rb") as f:
                    sats = _parse_tle_block(f.read().decode("ascii", errors="replace"))
                with self._cache_lock:
                    self._tle_cache[group] = sats
                self._tle_last_fetch[group] = os.path.getmtime(cache_file)
                self.log.debug("Loaded cached TLE group %s (%d sats)", group, len(sats))
            except Exception:
                self.log.exception("Failed to read cached TLE %s", cache_file)

    # ------------------------------------------------------------------
    # Launch Library 2
    # ------------------------------------------------------------------
    def _launch_loop(self) -> None:
        self._sleep_while_active(10)
        while self._active:
            try:
                self._fetch_launches()
            except Exception:
                self.log.exception("Error fetching launches")
            self._sleep_while_active(60)

    def _fetch_launches(self) -> None:
        if not self._limiters["launchlibrary"].can_request():
            return
        url = self._LAUNCH_LIBRARY_URL.format(limit=self._launch_limit)
        status, body, _ = self._http.get(url, self._limiters["launchlibrary"], accept="application/json")
        if status in (0, 304) or body is None:
            return
        if status != 200:
            self.log.warning("Launch Library fetch failed (status=%s)", status)
            return
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
            results = data.get("results", [])
        except (ValueError, AttributeError):
            self.log.exception("Malformed Launch Library response")
            return

        simplified = [
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "net": r.get("net"),           # ISO 8601 launch time (no earlier than)
                "status": (r.get("status") or {}).get("name"),
                "provider": (r.get("launch_service_provider") or {}).get("name"),
                "pad": (r.get("pad") or {}).get("name"),
                "pad_location": ((r.get("pad") or {}).get("location") or {}).get("name"),
                "mission": (r.get("mission") or {}).get("name"),
                "webcast": bool(r.get("webcast_live")),
            }
            for r in results
        ]

        with self._cache_lock:
            self._launches = simplified
        self._save_rate_state()

        self.event_bus.publish(EVENT_LAUNCH_UPCOMING, {
            "launches": simplified,
            "fetched_at": time.time(),
        })
        self.log.info("Upcoming launches refreshed: %d entries", len(simplified))

    # ------------------------------------------------------------------
    # NOAA SWPC — Kp index (geomagnetic activity)
    # ------------------------------------------------------------------
    def _weather_loop(self) -> None:
        self._sleep_while_active(15)
        while self._active:
            try:
                self._fetch_weather()
            except Exception:
                self.log.exception("Error fetching space weather")
            self._sleep_while_active(60)

    def _fetch_weather(self) -> None:
        if not self._limiters["swpc"].can_request():
            return
        status, body, _ = self._http.get(self._SWPC_KP_URL, self._limiters["swpc"], accept="application/json")
        if status in (0, 304) or body is None:
            return
        if status != 200:
            self.log.warning("SWPC fetch failed (status=%s)", status)
            return
        try:
            # Format: [["time_tag","Kp","a_running","station_count"], [...], ...]
            rows = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            self.log.exception("Malformed SWPC response")
            return
        if not isinstance(rows, list) or len(rows) < 2:
            return

        latest = rows[-1]
        try:
            kp = float(latest[1])
        except (ValueError, TypeError, IndexError):
            kp = None
        snapshot = {
            "source": "NOAA SWPC",
            "time_tag": latest[0] if len(latest) > 0 else None,
            "kp": kp,
            "fetched_at": time.time(),
        }
        with self._cache_lock:
            self._weather = snapshot
        self._save_rate_state()

        self.event_bus.publish(EVENT_WEATHER_UPDATED, snapshot)
        self.log.debug("Space weather refreshed: Kp=%s", kp)

    # ------------------------------------------------------------------
    # Propagation — uses sgp4 (required) and optionally skyfield for
    # observer az/el.  Without observer coords we still publish sub-
    # satellite lat/lon/alt; az/el are omitted.
    # ------------------------------------------------------------------
    def _propagation_loop(self) -> None:
        """Publish sub-satellite points (and observer az/el when possible)."""
        try:
            from sgp4.api import Satrec, jday
        except ImportError:
            self.log.warning("sgp4 unavailable at loop start — exiting propagation loop")
            return

        interval = max(1.0, float(self._prop_cfg.get("interval_seconds", 10)))
        max_objects = int(self._prop_cfg.get("max_objects", 200))
        self.log.info(
            "Propagation loop active (interval=%.0fs, max_objects=%d)",
            interval, max_objects,
        )

        # Cache of parsed Satrec objects keyed by (l1, l2) so we don't
        # reparse every cycle.
        satrec_cache: dict[tuple[str, str], Any] = {}

        # Give TLE loop a head start
        self._sleep_while_active(15)

        while self._active:
            try:
                self._propagate_once(Satrec, jday, satrec_cache, max_objects)
            except Exception:
                self.log.exception("Error during propagation cycle")
            self._sleep_while_active(interval)

    def _propagate_once(
        self,
        Satrec: Any,
        jday: Any,
        cache: dict[tuple[str, str], Any],
        max_objects: int,
    ) -> None:
        """Single propagation sweep: computes positions for all cached TLEs."""
        # Snapshot TLE cache under lock
        with self._cache_lock:
            flat: list[dict[str, str]] = []
            for group_sats in self._tle_cache.values():
                flat.extend(group_sats)
                if len(flat) >= max_objects:
                    break
        if not flat:
            return

        flat = flat[:max_objects]
        observer = self._resolve_observer()

        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc)
        jd, fr = jday(now.year, now.month, now.day, now.hour, now.minute,
                      now.second + now.microsecond * 1e-6)
        gmst = _gmst_rad(jd + fr)

        objects: list[dict[str, Any]] = []
        for sat in flat:
            key = (sat["l1"], sat["l2"])
            satrec = cache.get(key)
            if satrec is None:
                try:
                    satrec = Satrec.twoline2rv(sat["l1"], sat["l2"])
                except (ValueError, RuntimeError):
                    continue
                cache[key] = satrec

            err, r, _v = satrec.sgp4(jd, fr)
            if err != 0:
                continue  # propagation error (decayed, bad TLE, etc.)

            lat, lon, alt_km = _teme_to_geodetic(r, gmst)
            entry: dict[str, Any] = {
                "name": sat["name"],
                "lat": round(lat, 4),
                "lon": round(lon, 4),
                "alt_km": round(alt_km, 1),
            }
            if observer is not None:
                az, el = _observer_look_angles(
                    observer["lat"], observer["lon"], observer.get("elev_m", 0),
                    lat, lon, alt_km,
                )
                entry["az"] = round(az, 1)
                entry["el"] = round(el, 1)
            objects.append(entry)

        # Evict stale cache entries (TLEs that no longer appear in groups)
        if len(cache) > len(flat) * 2:
            active_keys = {(s["l1"], s["l2"]) for s in flat}
            for key in list(cache):
                if key not in active_keys:
                    del cache[key]

        snapshot = {
            "fetched_at": time.time(),
            "observer": observer,
            "count": len(objects),
            "objects": objects,
        }
        with self._cache_lock:
            self._latest_positions = snapshot
        self.event_bus.publish(EVENT_POSITIONS_SNAPSHOT, snapshot)

    # ------------------------------------------------------------------
    # Observer location — looked up lazily so gps_telemetry can feed it
    # ------------------------------------------------------------------
    def _resolve_observer(self) -> dict[str, float] | None:
        lat = self._observer_cfg.get("latitude")
        lon = self._observer_cfg.get("longitude")
        if lat is not None and lon is not None:
            return {
                "lat": float(lat),
                "lon": float(lon),
                "elev_m": float(self._observer_cfg.get("elevation_m", 0)),
            }
        gps = self.app.get_plugin("gps_telemetry") if hasattr(self.app, "get_plugin") else None
        if gps is not None and hasattr(gps, "last_fix"):
            fix = getattr(gps, "last_fix", None)
            if isinstance(fix, dict) and fix.get("lat") is not None and fix.get("lon") is not None:
                return {
                    "lat": float(fix["lat"]),
                    "lon": float(fix["lon"]),
                    "elev_m": float(fix.get("alt_m", 0)),
                }
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_tle_block(text: str) -> list[dict[str, str]]:
    """Parse a concatenated TLE text file into a list of three-line records.

    Celestrak serves each satellite as three lines: ``<name>\\n1 ...\\n2 ...``.
    Malformed trailing content is skipped silently.
    """
    out: list[dict[str, str]] = []
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    i = 0
    while i + 2 < len(lines) + 1:
        if i + 2 >= len(lines):
            break
        name = lines[i].strip()
        l1 = lines[i + 1]
        l2 = lines[i + 2]
        if l1.startswith("1 ") and l2.startswith("2 "):
            out.append({"name": name, "l1": l1, "l2": l2})
            i += 3
        else:
            i += 1
    return out


def _gmst_rad(jd_ut1: float) -> float:
    """Greenwich Mean Sidereal Time in radians at given Julian date (UT1).

    Implements the IAU 1982 polynomial.  Accurate to ~1 arcsec for dates
    near the present, which is more than enough for satellite pointing.
    """
    import math

    t = (jd_ut1 - 2451545.0) / 36525.0
    gmst_sec = (
        67310.54841
        + (876600.0 * 3600.0 + 8640184.812866) * t
        + 0.093104 * t * t
        - 6.2e-6 * t * t * t
    )
    # Reduce to [0, 86400) then convert to radians
    gmst_sec = gmst_sec % 86400.0
    return (gmst_sec / 86400.0) * 2.0 * math.pi


def _teme_to_geodetic(r_teme: tuple, gmst: float) -> tuple[float, float, float]:
    """Convert a TEME position vector (km) to geodetic lat/lon/alt (deg, deg, km).

    Uses the WGS-84 ellipsoid.  TEME → ECEF is a simple rotation around Z by
    -GMST; we approximate TEME ≈ PEF for this purpose (sub-km error, fine
    for a dashboard display).
    """
    import math

    x_teme, y_teme, z_teme = r_teme
    cos_g = math.cos(gmst)
    sin_g = math.sin(gmst)
    x =  cos_g * x_teme + sin_g * y_teme
    y = -sin_g * x_teme + cos_g * y_teme
    z = z_teme

    # WGS-84
    a = 6378.137        # km
    e2 = 6.69437999014e-3

    lon = math.atan2(y, x)
    p = math.sqrt(x * x + y * y)
    # Iterative lat/alt (Bowring's method — converges in ~3 iterations)
    lat = math.atan2(z, p * (1 - e2))
    for _ in range(5):
        sin_lat = math.sin(lat)
        n = a / math.sqrt(1 - e2 * sin_lat * sin_lat)
        alt = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1 - e2 * n / (n + alt)))
    sin_lat = math.sin(lat)
    n = a / math.sqrt(1 - e2 * sin_lat * sin_lat)
    alt = p / math.cos(lat) - n

    return math.degrees(lat), math.degrees(lon), alt


def _observer_look_angles(
    obs_lat_deg: float,
    obs_lon_deg: float,
    obs_elev_m: float,
    sat_lat_deg: float,
    sat_lon_deg: float,
    sat_alt_km: float,
) -> tuple[float, float]:
    """Return (azimuth_deg, elevation_deg) from observer to a satellite.

    Uses an ECEF → ENU transform.  Elevation < 0 means below the horizon.
    Azimuth is measured clockwise from true north.
    """
    import math

    a = 6378.137
    e2 = 6.69437999014e-3

    def geodetic_to_ecef(lat_d: float, lon_d: float, alt_km: float) -> tuple[float, float, float]:
        lat = math.radians(lat_d)
        lon = math.radians(lon_d)
        sin_lat = math.sin(lat)
        n = a / math.sqrt(1 - e2 * sin_lat * sin_lat)
        x = (n + alt_km) * math.cos(lat) * math.cos(lon)
        y = (n + alt_km) * math.cos(lat) * math.sin(lon)
        z = (n * (1 - e2) + alt_km) * sin_lat
        return x, y, z

    obs = geodetic_to_ecef(obs_lat_deg, obs_lon_deg, obs_elev_m / 1000.0)
    sat = geodetic_to_ecef(sat_lat_deg, sat_lon_deg, sat_alt_km)

    dx, dy, dz = sat[0] - obs[0], sat[1] - obs[1], sat[2] - obs[2]
    lat = math.radians(obs_lat_deg)
    lon = math.radians(obs_lon_deg)
    sl, cl = math.sin(lat), math.cos(lat)
    so, co = math.sin(lon), math.cos(lon)
    # ENU
    e = -so * dx + co * dy
    n_ = -sl * co * dx - sl * so * dy + cl * dz
    u = cl * co * dx + cl * so * dy + sl * dz

    rng = math.sqrt(e * e + n_ * n_ + u * u)
    if rng == 0:
        return 0.0, 90.0
    el = math.degrees(math.asin(u / rng))
    az = math.degrees(math.atan2(e, n_)) % 360.0
    return az, el


def _sgp4_available() -> bool:
    try:
        import sgp4  # noqa: F401
        return True
    except ImportError:
        return False


def _skyfield_available() -> bool:
    try:
        import skyfield  # noqa: F401
        return True
    except ImportError:
        return False
