"""ISM band decoder plugin wrapping rtl_433.

Passively decodes transmissions from IoT devices, weather stations,
tire pressure monitors, and other ISM-band devices using rtl_433.
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


class ISMDecoder(SignalPluginBase):
    """ISM band device decoder using rtl_433."""

    plugin_name = "ism_decoder"
    plugin_version = "0.1.0"
    plugin_description = "ISM band decoder (rtl_433)"
    broadcast_keys = "ism"

    signal_priority = PRIORITY_BACKGROUND
    signal_continuous = True
    signal_label = "ISM Band Decoder"

    def validate_config(self) -> None:
        self._devices_lock = threading.Lock()
        self._snapshot_dirty = True
        self._decoder_bin = str(self.config.get("decoder_bin", "rtl_433"))
        self._gain = self.config.get("gain", None)
        self._ppm = int(self.config.get("ppm", 0))
        self._protocols: list[int] = [int(p) for p in self.config.get("protocols", [])]
        self._protocol_blacklist: list[int] = [
            int(p) for p in self.config.get("protocol_blacklist", [])
        ]
        self._max_devices = int(self.config.get("max_devices", 100))
        self._stale_timeout = float(self.config.get("stale_timeout", 600))
        self._max_restarts = int(self.config.get("max_restarts", 5))

    def _on_start(self) -> None:
        self._devices: dict[str, dict[str, Any]] = {}
        self._stats = {
            "messages_total": 0,
            "devices_total": 0,
            "devices_active": 0,
            "last_message_at": None,
        }
        self._status = "idle"
        self._last_error: str | None = None
        self._restart_count = 0
        self._snapshot_dirty = True
        self._start_thread(self._maintenance_loop, name="ism-maintenance")

    def _on_stop(self) -> None:
        pass

    def _launch_subprocess(self, device_index: int) -> None:
        decoder = shutil.which(self._decoder_bin)
        if not decoder:
            self._status = "unavailable"
            self._last_error = f"{self._decoder_bin} not found on PATH"
            self.log.warning(self._last_error)
            raise RuntimeError(self._last_error)

        cmd = [
            decoder,
            "-d",
            str(device_index),
            "-F",
            "json",
            "-M",
            "utc",
            "-M",
            "protocol",
            "-M",
            "level",
            "-p",
            str(self._ppm),
        ]
        if self._gain is not None:
            cmd.extend(["-g", str(self._gain)])

        if self._protocols:
            for proto in self._protocols:
                cmd.extend(["-R", str(proto)])
        elif self._protocol_blacklist:
            for proto in self._protocol_blacklist:
                cmd.extend(["-R", f"-{proto}"])

        self.log.debug("Launching: %s", " ".join(cmd))

        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._pid = self._process.pid
        self._status = "scanning"
        self._restart_count = 0

        self._start_stderr_reader(self._process, prefix="rtl_433")
        self._start_thread(self._parser_loop, name="ism-parser")

        self.log.info("ISM decoder started (PID %d)", self._pid)

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
                self._handle_device(msg)
        except (ValueError, OSError):
            pass
        except Exception:
            self.log.exception("ISM parser crashed")

    def _handle_device(self, msg: dict[str, Any]) -> None:
        model = msg.get("model", "")
        dev_id = msg.get("id", "")
        if not model:
            return

        key = f"{model}:{dev_id}" if dev_id is not None and dev_id != "" else model
        now = time.time()
        self._stats["messages_total"] += 1
        self._stats["last_message_at"] = now

        publish_event = None
        with self._devices_lock:
            is_new = key not in self._devices
            if is_new and len(self._devices) >= self._max_devices:
                return

            if is_new:
                self._devices[key] = {
                    "key": key,
                    "model": model,
                    "id": dev_id,
                    "first_seen": now,
                    "message_count": 0,
                }
                self._stats["devices_total"] += 1
                self.log.info("New ISM device: %s", key)
                publish_event = {
                    "key": key,
                    "model": model,
                    "id": dev_id,
                }

            dev = self._devices[key]
            dev["last_seen"] = now
            dev["message_count"] += 1

            dev["channel"] = msg.get("channel")
            dev["battery_ok"] = msg.get("battery_ok")
            dev["temperature_C"] = msg.get("temperature_C")
            dev["humidity"] = msg.get("humidity")
            dev["wind_avg_km_h"] = msg.get("wind_avg_km_h")
            dev["wind_max_km_h"] = msg.get("wind_max_km_h")
            dev["wind_dir_deg"] = msg.get("wind_dir_deg")
            dev["rain_mm"] = msg.get("rain_mm")
            dev["pressure_hPa"] = msg.get("pressure_hPa")
            dev["rssi"] = msg.get("rssi")
            dev["snr"] = msg.get("snr")
            dev["noise"] = msg.get("noise")
            dev["freq_mhz"] = msg.get("freq")
            dev["protocol"] = msg.get("protocol")

        if publish_event:
            try:
                self.event_bus.publish(events.ISM_DEVICE_DETECTED, publish_event)
            except Exception:
                self.log.debug("event publish failed", exc_info=True)

        self._snapshot_dirty = True
        self._update_snapshot_cache()

    def _maintenance_loop(self) -> None:
        while self._active:
            self._sleep_while_active(60.0)
            if not self._active:
                break
            self._evict_stale()

    def _evict_stale(self) -> None:
        now = time.time()
        evicted: list[dict[str, Any]] = []
        with self._devices_lock:
            stale_keys = [
                k for k, v in self._devices.items() if now - v["last_seen"] > self._stale_timeout
            ]
            for key in stale_keys:
                dev = self._devices.pop(key)
                evicted.append(dev)
                self.log.debug("ISM device lost: %s", key)
        for dev in evicted:
            try:
                self.event_bus.publish(
                    events.ISM_DEVICE_LOST,
                    {
                        "key": dev.get("key"),
                        "model": dev.get("model"),
                        "id": dev.get("id"),
                        "last_seen": dev.get("last_seen"),
                    },
                )
            except Exception:
                self.log.debug("event publish failed", exc_info=True)
        if evicted:
            self._snapshot_dirty = True
        self._update_snapshot_cache()

    def get_device_inventory(self) -> dict[str, Any]:
        with self._devices_lock:
            devices = []
            for dev in sorted(
                self._devices.values(),
                key=lambda d: d.get("last_seen", 0),
                reverse=True,
            ):
                d = dict(dev)
                d.pop("first_seen", None)
                devices.append(d)
            return {
                "devices": devices,
                "stats": dict(self._stats),
                "devices_active": len(self._devices),
            }

    def _update_snapshot_cache(self) -> None:
        if not self._snapshot_dirty:
            return
        self._snapshot_dirty = False
        with self._devices_lock:
            active = len(self._devices)
            self._stats["devices_active"] = active
            snapshot_devices = [
                {
                    "key": d["key"],
                    "model": d["model"],
                    "id": d.get("id"),
                    "channel": d.get("channel"),
                    "battery_ok": d.get("battery_ok"),
                    "temperature_C": d.get("temperature_C"),
                    "humidity": d.get("humidity"),
                    "last_seen": d.get("last_seen"),
                    "message_count": d.get("message_count", 0),
                }
                for d in sorted(
                    self._devices.values(),
                    key=lambda x: x.get("last_seen", 0),
                    reverse=True,
                )[:20]
            ]
            stats = dict(self._stats)
        with self._cache_lock:
            self._snapshot_cache = {
                "status": self._status,
                "error": self._last_error,
                "devices_active": active,
                "devices": snapshot_devices,
                "stats": stats,
            }

    def get_status(self) -> dict[str, Any]:
        return {
            "active": self._active,
            "status": self._status,
            "error": self._last_error,
            "devices_active": len(self._devices),
            "messages_total": self._stats["messages_total"],
        }
