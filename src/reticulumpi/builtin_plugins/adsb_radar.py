"""ADS-B aircraft tracker plugin — RTL-SDR + dump1090.

Launches ``dump1090`` as a long-lived subprocess that demodulates 1090 MHz
Mode-S / ADS-B Extended Squitter transmissions from nearby aircraft.  The
plugin connects to dump1090's SBS BaseStation TCP port (default 30003),
parses the CSV stream, and maintains an in-memory dict of currently-tracked
aircraft that the web dashboard reads via ``get_snapshot()``.

Example config::

    adsb_radar:
      enabled: true
      dump1090_bin: "dump1090"
      device_index: 0
      gain: "max"
      ppm: 0
      enable_bias_tee: true
      sbs_port: 30003
      stale_timeout: 300
      receiver_lat: null
      receiver_lon: null

Requirements:
    * ``dump1090`` (any fork: dump1090-mutability, dump1090-fa, readsb)
      available on PATH or specified via ``dump1090_bin``.
    * RTL-SDR kernel driver blacklisted (``/etc/modprobe.d/blacklist-rtlsdr.conf``).
    * Optional: ``pyModeS`` for aircraft-category enrichment.
"""

from __future__ import annotations

import math
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase

_EMERGENCY_SQUAWKS = frozenset({"7500", "7600", "7700"})


@dataclass
class AircraftState:
    icao: str
    callsign: str | None = None
    altitude: int | None = None
    ground_speed: float | None = None
    track: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    vertical_rate: int | None = None
    squawk: str | None = None
    on_ground: bool = False
    category: str | None = None
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    message_count: int = 0
    distance_nm: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "icao": self.icao,
            "callsign": self.callsign,
            "altitude": self.altitude,
            "ground_speed": self.ground_speed,
            "track": self.track,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "vertical_rate": self.vertical_rate,
            "squawk": self.squawk,
            "on_ground": self.on_ground,
            "category": self.category,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "message_count": self.message_count,
            "distance_nm": self.distance_nm,
        }


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles."""
    from reticulumpi.geo import haversine_nm
    return haversine_nm(lat1, lon1, lat2, lon2)


class AdsbRadarPlugin(PluginBase):
    """ADS-B aircraft tracker using an RTL-SDR dongle and dump1090."""

    plugin_name = "adsb_radar"
    plugin_version = "1.0.0"
    plugin_description = "ADS-B aircraft tracker using RTL-SDR and dump1090"
    broadcast_tier = 2
    broadcast_keys = "adsb"

    _MAX_SBS_BUF = 1_048_576  # 1 MB

    # ── config ────────────────────────────────────────────────────────

    def validate_config(self) -> None:
        cfg = self.config
        self._dump1090_bin = str(cfg.get("dump1090_bin", "dump1090"))
        self._device_id = str(cfg.get("device_serial") or cfg.get("device_index", "0"))
        self._gain = str(cfg.get("gain", "max"))
        self._ppm = int(cfg.get("ppm", 0))
        self._enable_bias_tee = bool(cfg.get("enable_bias_tee", False))
        self._sbs_port = int(cfg.get("sbs_port", 30003))
        self._stale_timeout = float(cfg.get("stale_timeout", 300))
        self._max_restarts = int(cfg.get("max_restarts", 5))
        self._max_aircraft = int(cfg.get("max_aircraft", 500))

        self._receiver_lat: float | None = None
        self._receiver_lon: float | None = None
        lat = cfg.get("receiver_lat")
        lon = cfg.get("receiver_lon")
        if lat is not None and lon is not None:
            self._receiver_lat = float(lat)
            self._receiver_lon = float(lon)

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        self._state_lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._pid: int | None = None
        self._restart_count = 0
        self._status = "starting"
        self._last_error: str | None = None
        self._dump1090_path: str | None = None
        self._rtl_biast_path: str | None = None
        self._bias_tee_active = False

        self._aircraft: dict[str, AircraftState] = {}
        self._total_messages = 0
        self._aircraft_seen_total = 0
        self._resolved_index: int | None = None
        self._broadcast_cache: tuple[float, dict] | None = None
        self._broadcast_cache_ttl = 3.0

        self._msg_rate_history: deque[float] = deque(maxlen=60)
        self._msg_rate_window_start = time.time()
        self._msg_rate_window_count = 0
        self._max_distance_nm = 0.0
        self._emergency_history: deque[dict[str, Any]] = deque(maxlen=20)

        self._active = True

        self.event_bus.subscribe(events.GPS_FIX_RECEIVED, self._on_gps_fix)
        self.event_bus.subscribe(events.GPS_FIX_UPDATED, self._on_gps_fix)

        if self._receiver_lat is None or self._receiver_lon is None:
            gps = self.app.get_plugin("gps_telemetry") if hasattr(self.app, "get_plugin") else None
            if gps is not None and hasattr(gps, "last_fix"):
                fix = getattr(gps, "last_fix", None)
                if isinstance(fix, dict) and fix.get("lat") is not None and fix.get("lon") is not None:
                    self._receiver_lat = float(fix["lat"])
                    self._receiver_lon = float(fix["lon"])
                    self.log.info("Receiver position from GPS: %.4f, %.4f", self._receiver_lat, self._receiver_lon)

        self._start_thread(self._supervisor_loop, name="adsb-supervisor")
        self._start_thread(self._maintenance_loop, name="adsb-maintenance")

        self.log.info(
            "adsb_radar started: device %s, gain %s, ppm %d, SBS port %d",
            self._device_id, self._gain, self._ppm, self._sbs_port,
        )

    def stop(self) -> None:
        self._active = False
        self.event_bus.unsubscribe(events.GPS_FIX_RECEIVED, self._on_gps_fix)
        self.event_bus.unsubscribe(events.GPS_FIX_UPDATED, self._on_gps_fix)
        self._terminate_process()
        if self._enable_bias_tee:
            self._set_bias_tee(False)
        try:
            from reticulumpi.rtlsdr import release_device
            release_device(self._device_id, caller=self.plugin_name)
        except Exception:
            pass
        self._join_threads(timeout=5.0)
        self._set_status("stopped")

    # ── public API ────────────────────────────────────────────────────

    def get_status(self) -> dict[str, Any]:
        running = self._process is not None and self._process.poll() is None
        with self._state_lock:
            return {
                "active": self._active,
                "running": running,
                "pid": self._pid,
                "status": self._status,
                "aircraft_count": len(self._aircraft),
                "total_messages": self._total_messages,
                "aircraft_seen_total": self._aircraft_seen_total,
                "error": self._last_error,
            }

    def broadcast_snapshot(self, cycle_count: int = 0) -> dict[str, Any] | None:
        now = time.monotonic()
        cached = self._broadcast_cache
        if cached is not None and (now - cached[0]) < self._broadcast_cache_ttl:
            return cached[1]
        result = self.get_snapshot()
        self._broadcast_cache = (now, result)
        return result

    def get_snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            aircraft_list = [ac.to_dict() for ac in self._aircraft.values()]
            return {
                "status": self._status,
                "error": self._last_error,
                "aircraft": aircraft_list,
                "stats": {
                    "aircraft_count": len(self._aircraft),
                    "total_messages": self._total_messages,
                    "aircraft_seen_total": self._aircraft_seen_total,
                    "receiver_lat": self._receiver_lat,
                    "receiver_lon": self._receiver_lon,
                    "max_distance_nm": self._max_distance_nm,
                    "msg_rate_history": list(self._msg_rate_history),
                    "emergency_history": list(self._emergency_history),
                },
            }

    # ── supervisor ────────────────────────────────────────────────────

    def _supervisor_loop(self) -> None:
        self._dump1090_path = shutil.which(self._dump1090_bin)
        if not self._dump1090_path:
            self._set_status(
                "unavailable",
                f"{self._dump1090_bin} not found on PATH — "
                "install dump1090, dump1090-mutability, dump1090-fa, or readsb",
            )
            self.log.warning(
                "%s not found; adsb_radar will stay idle", self._dump1090_bin,
            )
            return

        try:
            from reticulumpi.rtlsdr import resolve_device
            self._resolved_index = resolve_device(self._device_id, caller=self.plugin_name)
        except RuntimeError as exc:
            self._set_status("error", str(exc))
            self.log.error("%s", exc)
            return

        if self._enable_bias_tee:
            self._rtl_biast_path = shutil.which("rtl_biast")
            if not self._rtl_biast_path:
                self.log.warning(
                    "rtl_biast not found; bias-tee may not work with this dump1090 build",
                )

        while self._active:
            try:
                self._launch_dump1090()
            except Exception as exc:
                self._set_status("error", f"launch failed: {exc}")
                self.log.exception("Failed to launch %s", self._dump1090_bin)
                break

            parser = self._start_thread(self._parser_loop, name="adsb-parser")

            while self._active and self._process is not None:
                rc = self._process.poll()
                if rc is not None:
                    self.log.warning("dump1090 exited (code %s)", rc)
                    break
                self._sleep_while_active(5.0)

            parser.join(timeout=3.0)
            self._remove_thread(parser)
            self._terminate_process()

            if not self._active:
                break

            self._restart_count += 1
            if self._restart_count > self._max_restarts:
                self._set_status(
                    "error",
                    f"dump1090 exceeded max_restarts ({self._max_restarts})",
                )
                self.log.error(
                    "dump1090 exceeded max_restarts (%d); giving up",
                    self._max_restarts,
                )
                break

            backoff = min(60.0, 2.0 ** self._restart_count)
            self._set_status("restarting", f"backoff {backoff:.0f}s")
            self.log.info(
                "Restarting dump1090 in %.0fs (attempt %d/%d)",
                backoff, self._restart_count, self._max_restarts,
            )
            self._sleep_while_active(backoff)

    def _launch_dump1090(self) -> None:
        if self._enable_bias_tee:
            self._set_bias_tee(True)
        cmd = self._build_cmd()
        self.log.debug("Launching: %s", " ".join(cmd))
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self._pid = self._process.pid
        self._start_log_reader(self._process, prefix="dump1090")
        self._set_status("running")
        self.log.info("Started dump1090 (PID %d)", self._pid)

    def _build_cmd(self) -> list[str]:
        assert self._dump1090_path is not None
        cmd = [
            self._dump1090_path,
            "--device-index", str(self._resolved_index if self._resolved_index is not None else self._device_id),
            "--gain", self._gain,
            "--ppm", str(self._ppm),
            "--net",
            "--net-sbs-port", str(self._sbs_port),
            "--quiet",
        ]
        if self._receiver_lat is not None and self._receiver_lon is not None:
            cmd += ["--lat", str(self._receiver_lat), "--lon", str(self._receiver_lon)]
        return cmd

    def _terminate_process(self) -> None:
        proc = self._process
        self._process = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.log.warning("dump1090 did not stop; sending SIGKILL")
                    proc.kill()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.log.warning("dump1090 did not exit after SIGKILL")
        except Exception:
            self.log.exception("Error stopping dump1090")
        finally:
            if proc.stdout:
                try:
                    proc.stdout.close()
                except Exception:
                    pass

    def _set_bias_tee(self, on: bool) -> None:
        if self._rtl_biast_path is None:
            return
        idx = self._resolved_index if self._resolved_index is not None else 0
        flag = "1" if on else "0"
        try:
            result = subprocess.run(
                [self._rtl_biast_path, "-d", str(idx), "-b", flag],
                capture_output=True, timeout=10,
            )
            if result.returncode == 0:
                self._bias_tee_active = on
                self.log.info("Bias-tee %s (device %d)", "enabled" if on else "disabled", idx)
            else:
                self.log.warning(
                    "rtl_biast exited %d: %s",
                    result.returncode,
                    result.stderr.decode(errors="replace").strip(),
                )
        except Exception:
            self.log.warning("Failed to %s bias-tee", "enable" if on else "disable", exc_info=True)

    # ── SBS parser (TCP to dump1090 port 30003) ──────────────────────

    def _parser_loop(self) -> None:
        """Connect to dump1090's SBS TCP port and parse aircraft messages."""
        while self._active:
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5.0)
                sock.connect(("127.0.0.1", self._sbs_port))
                sock.settimeout(2.0)
                self.log.info("Connected to SBS feed on port %d", self._sbs_port)
                buf = ""
                while self._active:
                    try:
                        data = sock.recv(4096)
                    except socket.timeout:
                        continue
                    if not data:
                        break
                    buf += data.decode("utf-8", errors="replace")
                    if len(buf) > self._MAX_SBS_BUF:
                        self.log.warning(
                            "SBS buffer exceeded %d bytes; discarding to resync",
                            self._MAX_SBS_BUF,
                        )
                        buf = ""
                        continue
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if line:
                            self._parse_sbs_line(line)
            except OSError as exc:
                if self._active:
                    self.log.debug("SBS connection failed: %s — retrying", exc)
            finally:
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass
            if self._active:
                self._sleep_while_active(2.0)

    def _parse_sbs_line(self, line: str) -> None:
        """Parse one SBS BaseStation CSV line and update aircraft state.

        SBS format (comma-separated):
          0: message_type (MSG, STA, ID, AIR, SEL, CLK)
          1: transmission_type (1-8 for MSG)
          2: session_id
          3: aircraft_id
          4: icao_hex
          5: flight_id
          6: date_generated
          7: time_generated
          8: date_logged
          9: time_logged
         10: callsign
         11: altitude
         12: ground_speed
         13: track
         14: latitude
         15: longitude
         16: vertical_rate
         17: squawk
         18: alert_flag
         19: emergency_flag
         20: spi_flag
         21: on_ground_flag
        """
        parts = line.split(",")
        if len(parts) < 11 or parts[0] != "MSG":
            return

        icao = parts[4].strip().upper()
        if not icao or len(icao) != 6:
            return

        now = time.time()
        new_aircraft = False
        emergency = False

        with self._state_lock:
            ac = self._aircraft.get(icao)
            if ac is None:
                ac = AircraftState(icao=icao, first_seen=now)
                self._aircraft[icao] = ac
                self._aircraft_seen_total += 1
                new_aircraft = True
                if len(self._aircraft) > self._max_aircraft:
                    oldest_icao = min(
                        self._aircraft,
                        key=lambda k: self._aircraft[k].last_seen,
                    )
                    del self._aircraft[oldest_icao]

            ac.last_seen = now
            ac.message_count += 1
            self._total_messages += 1

            # Message rate: roll window every 10 seconds
            self._msg_rate_window_count += 1
            if now - self._msg_rate_window_start >= 10.0:
                rate = self._msg_rate_window_count / (now - self._msg_rate_window_start)
                self._msg_rate_history.append(round(rate, 1))
                self._msg_rate_window_count = 0
                self._msg_rate_window_start = now

            callsign = parts[10].strip() if len(parts) > 10 else ""
            if callsign:
                ac.callsign = callsign

            if len(parts) > 11 and parts[11].strip():
                try:
                    ac.altitude = int(float(parts[11].strip()))
                except (ValueError, OverflowError):
                    pass

            if len(parts) > 12 and parts[12].strip():
                try:
                    ac.ground_speed = float(parts[12].strip())
                except ValueError:
                    pass

            if len(parts) > 13 and parts[13].strip():
                try:
                    ac.track = float(parts[13].strip())
                except ValueError:
                    pass

            if len(parts) > 14 and len(parts) > 15:
                lat_s = parts[14].strip()
                lon_s = parts[15].strip()
                if lat_s and lon_s:
                    try:
                        lat = float(lat_s)
                        lon = float(lon_s)
                        if not (lat == 0.0 and lon == 0.0):
                            ac.latitude = lat
                            ac.longitude = lon
                            if self._receiver_lat is not None and self._receiver_lon is not None:
                                ac.distance_nm = round(
                                    _haversine_nm(
                                        self._receiver_lat, self._receiver_lon, lat, lon,
                                    ),
                                    1,
                                )
                                if ac.distance_nm > self._max_distance_nm:
                                    self._max_distance_nm = ac.distance_nm
                    except ValueError:
                        pass

            if len(parts) > 16 and parts[16].strip():
                try:
                    ac.vertical_rate = int(float(parts[16].strip()))
                except (ValueError, OverflowError):
                    pass

            if len(parts) > 17 and parts[17].strip():
                squawk = parts[17].strip()
                old_squawk = ac.squawk
                ac.squawk = squawk
                if squawk in _EMERGENCY_SQUAWKS and old_squawk != squawk:
                    emergency = True

            if len(parts) > 21 and parts[21].strip():
                ac.on_ground = parts[21].strip() == "-1"

        if new_aircraft:
            self._publish(events.ADSB_AIRCRAFT_DETECTED, {"icao": icao})

        if emergency:
            squawk_val = parts[17].strip()
            emerg_record = {
                "icao": icao,
                "squawk": squawk_val,
                "callsign": callsign or None,
                "altitude": ac.altitude,
                "latitude": ac.latitude,
                "longitude": ac.longitude,
                "distance_nm": ac.distance_nm,
                "timestamp": now,
            }
            self._emergency_history.append(emerg_record)

            self._publish(events.ADSB_EMERGENCY_SQUAWK, {
                "icao": icao,
                "squawk": squawk_val,
                "callsign": callsign or None,
            })
            self._publish(events.SIGOPS_SIGNAL_DETECTED, {
                "source": "adsb_radar",
                "signal_type": "ADS-B_EMERGENCY",
                "icao": icao,
                "squawk": squawk_val,
                "callsign": callsign or None,
            })

    # ── maintenance ───────────────────────────────────────────────────

    def _maintenance_loop(self) -> None:
        """Periodically expire stale aircraft."""
        while self._active:
            self._sleep_while_active(30.0)
            if not self._active:
                break
            now = time.time()
            expired: list[str] = []
            with self._state_lock:
                for icao, ac in list(self._aircraft.items()):
                    if now - ac.last_seen > self._stale_timeout:
                        expired.append(icao)
                        del self._aircraft[icao]
            for icao in expired:
                self._publish(events.ADSB_AIRCRAFT_LOST, {"icao": icao})

    # ── GPS event handler ─────────────────────────────────────────────

    def _on_gps_fix(self, event_type: str, data: dict) -> None:
        lat = data.get("lat")
        lon = data.get("lon")
        if lat is not None and lon is not None:
            with self._state_lock:
                had_position = self._receiver_lat is not None
                self._receiver_lat = float(lat)
                self._receiver_lon = float(lon)
            if not had_position:
                self.log.info("Receiver position from GPS: %.4f, %.4f", self._receiver_lat, self._receiver_lon)

    # ── helpers ───────────────────────────────────────────────────────

    def _set_status(self, status: str, error: str | None = None) -> None:
        with self._state_lock:
            prev = self._status
            self._status = status
            self._last_error = error
        if status != prev:
            self._publish(events.ADSB_STATUS, {
                "status": status,
                "error": error,
                "timestamp": time.time(),
            })

    def _publish(self, event: str, data: dict) -> None:
        try:
            self.event_bus.publish(event, data)
        except Exception:
            self.log.debug("event_bus publish failed", exc_info=True)
