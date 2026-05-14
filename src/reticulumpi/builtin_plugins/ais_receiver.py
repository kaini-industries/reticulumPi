"""AIS marine vessel receiver plugin.

Decodes Automatic Identification System signals on VHF channels 161.975
and 162.025 MHz using AIS-catcher or rtl_ais.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from typing import Any

from reticulumpi import events
from reticulumpi.builtin_plugins.signal_plugin_base import SignalPluginBase
from reticulumpi.sdr_scheduler import PRIORITY_BACKGROUND

_SHIP_TYPES: dict[int, str] = {
    0: "Not available",
    20: "Wing in ground", 30: "Fishing", 31: "Towing", 32: "Towing (large)",
    33: "Dredging", 34: "Diving ops", 35: "Military ops",
    36: "Sailing", 37: "Pleasure craft",
    40: "High speed craft", 50: "Pilot vessel", 51: "SAR vessel",
    52: "Tug", 53: "Port tender", 55: "Law enforcement",
    60: "Passenger", 70: "Cargo", 80: "Tanker", 90: "Other",
}

_NAV_STATUSES = {
    0: "Under way using engine", 1: "At anchor", 2: "Not under command",
    3: "Restricted manoeuvrability", 4: "Constrained by draught",
    5: "Moored", 6: "Aground", 7: "Engaged in fishing",
    8: "Under way sailing", 14: "AIS-SART", 15: "Not defined",
}


def _ship_type_desc(code: int) -> str:
    if code in _SHIP_TYPES:
        return _SHIP_TYPES[code]
    decade = (code // 10) * 10
    return _SHIP_TYPES.get(decade, f"Type {code}")


class AISReceiver(SignalPluginBase):
    """AIS vessel tracker via AIS-catcher or rtl_ais."""

    plugin_name = "ais_receiver"
    plugin_version = "0.1.0"
    plugin_description = "AIS marine vessel receiver"
    broadcast_keys = "ais"

    signal_priority = PRIORITY_BACKGROUND
    signal_continuous = True
    signal_label = "AIS Vessel Tracking"

    def validate_config(self) -> None:
        self._gain = self.config.get("gain", None)
        self._ppm = int(self.config.get("ppm", 0))
        self._decoder_bin = str(self.config.get("decoder_bin", "AIS-catcher"))
        self._stale_timeout = float(self.config.get("stale_timeout", 600))
        self._max_vessels = int(self.config.get("max_vessels", 200))
        self._max_restarts = int(self.config.get("max_restarts", 5))

    def _on_start(self) -> None:
        self._vessels: dict[str, dict[str, Any]] = {}
        self._vessels_lock = threading.Lock()
        self._stats = {
            "messages_total": 0,
            "messages_by_type": {},
            "vessels_seen_total": 0,
        }
        self._status = "idle"
        self._last_error: str | None = None
        self._restart_count = 0

    def _launch_subprocess(self, device_index: int) -> None:
        decoder = shutil.which(self._decoder_bin)
        if not decoder:
            self._status = "unavailable"
            self._last_error = f"{self._decoder_bin} not found on PATH"
            self.log.warning(self._last_error)
            return

        if "AIS-catcher" in self._decoder_bin or "ais-catcher" in self._decoder_bin.lower():
            cmd = [decoder, "-d", str(device_index), "-p", str(self._ppm), "-o", "5"]
            if self._gain is not None:
                cmd += ["-gr", "tuner", str(self._gain), "rtlagc", "on"]
        else:
            cmd = [decoder, "-d", str(device_index), "-p", str(self._ppm)]
            if self._gain is not None:
                cmd += ["-g", str(self._gain)]

        self.log.debug("Launching: %s", " ".join(cmd))
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._pid = self._process.pid
        self._status = "running"
        self._restart_count = 0

        self._start_log_reader(self._stderr_fake(), prefix="ais")
        self._start_thread(self._parser_loop, name="ais-parser")
        self._start_thread(self._maintenance_loop, name="ais-maintenance")

        self.log.info("AIS receiver started (PID %d)", self._pid)

    def _stderr_fake(self) -> Any:
        class _F:
            pass
        f = _F()
        f.stdout = self._process.stderr if self._process else None  # type: ignore[attr-defined]
        return f

    def _parser_loop(self) -> None:
        proc = self._process
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                if not self._active:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text or not text.startswith("{"):
                    continue
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    continue
                self._handle_message(msg)
        except (ValueError, OSError):
            pass
        except Exception:
            self.log.exception("AIS parser crashed")

    def _handle_message(self, msg: dict[str, Any]) -> None:
        mmsi = str(msg.get("mmsi", ""))
        if not mmsi:
            return

        msg_type = msg.get("type", 0)
        self._stats["messages_total"] += 1
        type_key = str(msg_type)
        counts = self._stats["messages_by_type"]
        counts[type_key] = counts.get(type_key, 0) + 1

        now = time.time()
        with self._vessels_lock:
            vessel = self._vessels.get(mmsi)
            if vessel is None:
                if len(self._vessels) >= self._max_vessels:
                    self._evict_oldest()
                vessel = {
                    "mmsi": mmsi,
                    "name": None,
                    "ship_type": 0,
                    "ship_type_desc": "Not available",
                    "destination": None,
                    "lat": None,
                    "lon": None,
                    "speed_kts": None,
                    "course": None,
                    "heading": None,
                    "nav_status": None,
                    "first_seen": now,
                    "last_seen": now,
                    "message_count": 0,
                }
                self._vessels[mmsi] = vessel
                self._stats["vessels_seen_total"] += 1

                try:
                    self.event_bus.publish(events.AIS_VESSEL_DETECTED, {"mmsi": mmsi})
                except Exception:
                    pass

            vessel["last_seen"] = now
            vessel["message_count"] += 1

            if msg_type in (1, 2, 3):
                if msg.get("lat") is not None:
                    vessel["lat"] = msg["lat"]
                if msg.get("lon") is not None:
                    vessel["lon"] = msg["lon"]
                if msg.get("speed") is not None:
                    vessel["speed_kts"] = msg["speed"]
                if msg.get("course") is not None:
                    vessel["course"] = msg["course"]
                if msg.get("heading") is not None and msg["heading"] != 511:
                    vessel["heading"] = msg["heading"]
                status_code = msg.get("status")
                if status_code is not None:
                    vessel["nav_status"] = _NAV_STATUSES.get(
                        status_code, f"Status {status_code}",
                    )

            elif msg_type == 5:
                if msg.get("shipname"):
                    vessel["name"] = msg["shipname"].strip()
                if msg.get("destination"):
                    vessel["destination"] = msg["destination"].strip()
                if msg.get("shiptype") is not None:
                    vessel["ship_type"] = msg["shiptype"]
                    vessel["ship_type_desc"] = _ship_type_desc(msg["shiptype"])

        self._update_snapshot_cache()

    def _evict_oldest(self) -> None:
        if not self._vessels:
            return
        oldest_mmsi = min(self._vessels, key=lambda k: self._vessels[k]["last_seen"])
        self._vessels.pop(oldest_mmsi, None)

    def _maintenance_loop(self) -> None:
        while self._active and self._dongle_active:
            self._sleep_while_active(60.0)
            if not self._active:
                break
            now = time.time()
            expired: list[str] = []
            with self._vessels_lock:
                for mmsi, v in list(self._vessels.items()):
                    if now - v["last_seen"] > self._stale_timeout:
                        expired.append(mmsi)
                for mmsi in expired:
                    self._vessels.pop(mmsi, None)
            for mmsi in expired:
                try:
                    self.event_bus.publish(events.AIS_VESSEL_LOST, {"mmsi": mmsi})
                except Exception:
                    pass
            if expired:
                self._update_snapshot_cache()

    def _update_snapshot_cache(self) -> None:
        with self._vessels_lock:
            vessel_list = sorted(
                self._vessels.values(),
                key=lambda v: v["last_seen"],
                reverse=True,
            )
        with self._cache_lock:
            self._snapshot_cache = {
                "status": self._status,
                "error": self._last_error,
                "vessels": vessel_list,
                "stats": {
                    "vessel_count": len(vessel_list),
                    **self._stats,
                },
            }

    def get_status(self) -> dict[str, Any]:
        with self._vessels_lock:
            count = len(self._vessels)
        return {
            "active": self._active,
            "status": self._status,
            "error": self._last_error,
            "vessel_count": count,
            "messages_total": self._stats["messages_total"],
        }
