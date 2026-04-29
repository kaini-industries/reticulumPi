"""GPS Telemetry plugin — reads NMEA 0183 from a serial GPS receiver.

Exposes the ``last_fix`` contract consumed by ``space_tracker`` and produces a
``get_snapshot()`` dict driving the web dashboard's GPS panel.

Tested against a GlobalSat BU-353N (SiRF chipset, 4800 baud) but any receiver
emitting standard NMEA 0183 sentences (RMC / GGA / GSA / GSV / VTG) should work.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase


class GpsTelemetry(PluginBase):
    """Reads NMEA from a serial GPS receiver and exposes fix + satellite state."""

    plugin_name = "gps_telemetry"
    plugin_description = "NMEA GPS receiver telemetry"
    plugin_version = "1.0.0"

    # ── Configuration validation ────────────────────────────────────────

    def validate_config(self) -> None:
        source = self.config.get("source", "serial")
        if source not in ("serial", "gpsd"):
            raise ValueError("source must be 'serial' or 'gpsd'")

        try:
            import pynmea2  # noqa: F401
        except ImportError as exc:
            raise ValueError(f"pynmea2 required ({exc})")

        if source == "serial":
            try:
                import serial  # noqa: F401
            except ImportError as exc:
                raise ValueError(
                    "pyserial required for serial source. "
                    "Install with: pip install reticulumpi[gps] "
                    f"({exc})"
                )

        port = self.config.get("serial_port", "/dev/ttyUSB0")
        if source == "serial" and (not isinstance(port, str) or not port):
            raise ValueError("serial_port must be a non-empty string")

        if source == "gpsd":
            host = self.config.get("gpsd_host", "localhost")
            if not isinstance(host, str) or not host:
                raise ValueError("gpsd_host must be a non-empty string")
            gport = self.config.get("gpsd_port", 2947)
            if not isinstance(gport, int) or gport <= 0:
                raise ValueError("gpsd_port must be a positive integer")

        baud = self.config.get("baudrate", 4800)
        if not isinstance(baud, int) or baud <= 0:
            raise ValueError("baudrate must be a positive integer")

        rt = self.config.get("read_timeout", 2.0)
        if not isinstance(rt, (int, float)) or rt <= 0:
            raise ValueError("read_timeout must be a positive number")

        rd = self.config.get("reconnect_delay", 5)
        if not isinstance(rd, (int, float)) or rd < 1:
            raise ValueError("reconnect_delay must be >= 1 second")

        mra = self.config.get("max_reconnect_attempts", 0)
        if not isinstance(mra, int) or mra < 0:
            raise ValueError("max_reconnect_attempts must be >= 0 (0 = infinite)")

        fss = self.config.get("fix_stale_seconds", 30)
        if not isinstance(fss, (int, float)) or fss < 5:
            raise ValueError("fix_stale_seconds must be >= 5")

        stale_check = self.config.get("stale_check_interval", 5)
        if not isinstance(stale_check, (int, float)) or stale_check < 1:
            raise ValueError("stale_check_interval must be >= 1")

    # ── Lifecycle ───────────────────────────────────────────────────────

    def start(self) -> None:
        self._source: str = self.config.get("source", "serial")
        self._serial_port: str = self.config.get("serial_port", "/dev/ttyUSB0")
        self._baudrate: int = self.config.get("baudrate", 4800)
        self._read_timeout: float = float(self.config.get("read_timeout", 2.0))
        self._reconnect_delay: float = float(self.config.get("reconnect_delay", 5))
        self._max_reconnect_attempts: int = self.config.get("max_reconnect_attempts", 0)
        self._fix_stale_seconds: float = float(self.config.get("fix_stale_seconds", 30))
        self._stale_check_interval: float = float(
            self.config.get("stale_check_interval", 5)
        )
        self._gpsd_host: str = self.config.get("gpsd_host", "localhost")
        self._gpsd_port: int = self.config.get("gpsd_port", 2947)

        self._lock = threading.Lock()
        self.last_fix: dict[str, Any] | None = None
        self._satellites_in_view: list[dict[str, Any]] = []
        self._gsv_accum: dict[str, list[dict[str, Any]]] = {}
        self._gsv_expected: dict[str, int] = {}
        # PRNs reported as used-in-fix by the most recent GSA per talker.
        # Multi-GNSS receivers emit one GSA per constellation (GP/GL/GA/...) or
        # a combined GN GSA; a single-GNSS puck like the BU-353N emits only GP.
        # Snapshot takes the union so either shape works transparently.
        self._sats_in_use_by_talker: dict[str, set[int]] = {}

        self._serial = None
        self._connected = False
        self._have_fix = False
        self._msgs_received = 0
        self._sentences_parsed = 0
        self._parse_errors = 0
        self._last_msg_time = 0.0
        self._reconnect_failures = 0
        self._start_time = time.time()

        self._active = True
        if self._source == "gpsd":
            self._start_thread(self._gpsd_read_loop, name="gps-gpsd-reader")
        else:
            self._start_thread(self._read_loop, name="gps-reader")
        self._start_thread(self._stale_monitor, name="gps-stale")

        if self._source == "gpsd":
            self.log.info(
                "GPS telemetry started (gpsd %s:%d)",
                self._gpsd_host, self._gpsd_port,
            )
        else:
            self.log.info(
                "GPS telemetry started on %s @ %d baud",
                self._serial_port, self._baudrate,
            )

    def stop(self) -> None:
        self._active = False
        with self._lock:
            if self._serial is not None:
                try:
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None
        self._join_threads(timeout=5)

    # ── Public status / snapshot API ────────────────────────────────────

    def get_status(self) -> dict[str, Any]:
        lock = getattr(self, "_lock", None)
        if lock is None:
            return {"active": False, "connected": False}
        with lock:
            last_age = (
                time.time() - self._last_msg_time if self._last_msg_time else None
            )
            return {
                "active": self._active,
                "connected": self._connected,
                "serial_port": self._serial_port,
                "baudrate": self._baudrate,
                "msgs_received": self._msgs_received,
                "sentences_parsed": self._sentences_parsed,
                "parse_errors": self._parse_errors,
                "last_msg_age_s": last_age,
                "reconnect_failures": self._reconnect_failures,
                "have_fix": self._have_fix,
                "satellites_in_view_count": len(self._satellites_in_view),
                "uptime": time.time() - self._start_time,
            }

    def get_snapshot(self) -> dict[str, Any]:
        """Merged status + last_fix + satellites — one dict for the WS stream."""
        snap = self.get_status()
        lock = getattr(self, "_lock", None)
        if lock is None:
            return snap
        with lock:
            snap["last_fix"] = dict(self.last_fix) if self.last_fix else None
            in_use: set[int] = set()
            for s in self._sats_in_use_by_talker.values():
                in_use |= s
            snap["satellites_in_view"] = [
                {**sat, "in_use": sat.get("prn") in in_use}
                for sat in self._satellites_in_view
            ]
            snap["satellites_used_prns"] = sorted(in_use)
        return snap

    # ── Read loop ───────────────────────────────────────────────────────

    def _read_loop(self) -> None:
        import serial
        from serial import SerialException

        attempt = 0
        while self._active:
            try:
                ser = serial.Serial(
                    self._serial_port, self._baudrate, timeout=self._read_timeout
                )
            except (SerialException, OSError) as exc:
                attempt += 1
                self._reconnect_failures += 1
                self.log.warning(
                    "GPS open failed (attempt %d): %s", attempt, exc
                )
                if (
                    self._max_reconnect_attempts > 0
                    and attempt >= self._max_reconnect_attempts
                ):
                    self.log.error(
                        "GPS exceeded max reconnect attempts (%d), giving up",
                        self._max_reconnect_attempts,
                    )
                    self._active = False
                    return
                delay = min(
                    self._reconnect_delay * (2 ** min(attempt - 1, 5)), 300.0
                )
                self._sleep_while_active(delay)
                continue

            # Successfully opened
            with self._lock:
                self._serial = ser
                self._connected = True
            attempt = 0
            self.event_bus.publish(
                events.GPS_DEVICE_CONNECTED,
                {"port": self._serial_port, "baudrate": self._baudrate},
            )
            self.log.info("GPS connected on %s", self._serial_port)

            try:
                while self._active:
                    try:
                        raw = ser.readline()
                    except (SerialException, OSError) as exc:
                        self.log.warning("GPS read failed: %s", exc)
                        break
                    except TypeError:
                        # stop() closed the port from under us: pyserial zeroes
                        # its internal fd to None, then this thread wakes from
                        # readline() → os.read(None, ...) and TypeErrors.  By
                        # the time that happens stop() has already flipped
                        # _active=False, so treat it as a clean shutdown and
                        # bail.  Re-raise if we're still supposed to be active,
                        # since that would be a real bug worth seeing.
                        if self._active:
                            raise
                        break
                    if not raw:
                        continue  # read timeout — loop around to check _active
                    self._msgs_received += 1
                    line = raw.decode("ascii", errors="replace").strip()
                    if line.startswith("$"):
                        self._handle_sentence(line)
            finally:
                with self._lock:
                    self._connected = False
                    try:
                        ser.close()
                    except Exception:
                        pass
                    self._serial = None
                self.event_bus.publish(
                    events.GPS_DEVICE_DISCONNECTED,
                    {"port": self._serial_port},
                )

            if self._active:
                self._sleep_while_active(self._reconnect_delay)

    # ── Sentence dispatch ───────────────────────────────────────────────

    def _handle_sentence(self, line: str) -> None:
        import pynmea2

        try:
            msg = pynmea2.parse(line)
        except Exception:
            self._parse_errors += 1
            return
        self._sentences_parsed += 1
        now = time.time()
        kind = type(msg).__name__

        try:
            if kind == "RMC":
                self._apply_rmc(msg, now)
            elif kind == "GGA":
                self._apply_gga(msg, now)
            elif kind == "GSA":
                self._apply_gsa(msg)
            elif kind == "GSV":
                self._apply_gsv(msg)
            elif kind == "VTG":
                self._apply_vtg(msg, now)
        except Exception:
            self._parse_errors += 1

    # ── Sentence handlers ───────────────────────────────────────────────

    def _apply_rmc(self, msg: Any, now: float) -> None:
        if getattr(msg, "status", None) != "A":
            return
        lat = _decimal_or_none(getattr(msg, "latitude", None))
        lon = _decimal_or_none(getattr(msg, "longitude", None))
        if lat is None or lon is None:
            return

        speed_kn = _decimal_or_none(getattr(msg, "spd_over_grnd", None))
        heading = _decimal_or_none(getattr(msg, "true_course", None))

        utc_time = None
        utc_date = None
        tstamp = getattr(msg, "timestamp", None)
        if tstamp:
            utc_time = tstamp.strftime("%H:%M:%S")
        dstamp = getattr(msg, "datestamp", None)
        if dstamp:
            utc_date = dstamp.strftime("%Y-%m-%d")

        with self._lock:
            fix = dict(self.last_fix) if self.last_fix else {}
            fix["lat"] = lat
            fix["lon"] = lon
            if speed_kn is not None:
                fix["speed_kn"] = speed_kn
            if heading is not None:
                fix["heading_deg"] = heading
            else:
                fix.setdefault("heading_deg", None)
            if utc_time:
                fix["utc_time"] = utc_time
            if utc_date:
                fix["utc_date"] = utc_date
            fix["timestamp"] = now
            # Defaults for any key that may not have been filled by GGA/GSA yet
            fix.setdefault("alt_m", 0.0)
            fix.setdefault("fix_quality", 0)
            fix.setdefault("fix_type", 0)
            fix.setdefault("satellites_used", 0)
            fix.setdefault("hdop", None)
            fix.setdefault("pdop", None)
            fix.setdefault("vdop", None)
            self.last_fix = fix
            self._last_msg_time = now
            first_fix = not self._have_fix
            self._have_fix = True

        event = events.GPS_FIX_RECEIVED if first_fix else events.GPS_FIX_UPDATED
        self.event_bus.publish(
            event,
            {
                "lat": lat,
                "lon": lon,
                "alt_m": fix.get("alt_m", 0.0),
                "timestamp": now,
            },
        )

    def _apply_gga(self, msg: Any, now: float) -> None:
        alt = _decimal_or_none(getattr(msg, "altitude", None))
        hdop = _decimal_or_none(getattr(msg, "horizontal_dil", None))
        quality = _int_or_none(getattr(msg, "gps_qual", None))
        sats_used = _int_or_none(getattr(msg, "num_sats", None))

        with self._lock:
            fix = dict(self.last_fix) if self.last_fix else {}
            # GGA carries position too — fill in lat/lon if we don't yet have them
            if "lat" not in fix:
                lat = _decimal_or_none(getattr(msg, "latitude", None))
                lon = _decimal_or_none(getattr(msg, "longitude", None))
                if lat is not None and lon is not None:
                    fix["lat"] = lat
                    fix["lon"] = lon
            if alt is not None:
                fix["alt_m"] = alt
            if hdop is not None:
                fix["hdop"] = hdop
            if quality is not None:
                fix["fix_quality"] = quality
            if sats_used is not None:
                fix["satellites_used"] = sats_used
            fix.setdefault("timestamp", now)
            if "lat" in fix and "lon" in fix:
                self.last_fix = fix
            self._last_msg_time = now

    def _apply_gsa(self, msg: Any) -> None:
        fix_type = _int_or_none(getattr(msg, "mode_fix_type", None))
        pdop = _decimal_or_none(getattr(msg, "pdop", None))
        hdop = _decimal_or_none(getattr(msg, "hdop", None))
        vdop = _decimal_or_none(getattr(msg, "vdop", None))
        talker = getattr(msg, "talker", "GP") or "GP"
        in_use: set[int] = set()
        for i in range(1, 13):
            prn = _int_or_none(getattr(msg, f"sv_id{i:02d}", None))
            if prn:
                in_use.add(prn)
        with self._lock:
            self._sats_in_use_by_talker[talker] = in_use
            if self.last_fix is None:
                return
            fix = dict(self.last_fix)
            if fix_type is not None:
                fix["fix_type"] = fix_type
            if pdop is not None:
                fix["pdop"] = pdop
            if hdop is not None and fix.get("hdop") is None:
                fix["hdop"] = hdop
            if vdop is not None:
                fix["vdop"] = vdop
            self.last_fix = fix

    def _apply_gsv(self, msg: Any) -> None:
        talker = getattr(msg, "talker", "GP") or "GP"
        try:
            num_msgs = int(msg.num_messages)
            msg_num = int(msg.msg_num)
        except (AttributeError, TypeError, ValueError):
            return

        sats: list[dict[str, Any]] = []
        for i in range(1, 5):  # up to 4 satellites per sentence
            prn = _int_or_none(getattr(msg, f"sv_prn_num_{i}", None))
            if prn is None:
                continue
            sats.append(
                {
                    "prn": prn,
                    "talker": talker,
                    "elevation_deg": _int_or_none(
                        getattr(msg, f"elevation_deg_{i}", None)
                    ),
                    "azimuth_deg": _int_or_none(
                        getattr(msg, f"azimuth_{i}", None)
                    ),
                    "snr_db": _int_or_none(getattr(msg, f"snr_{i}", None)),
                }
            )

        with self._lock:
            if msg_num == 1:
                self._gsv_accum[talker] = []
                self._gsv_expected[talker] = num_msgs
            self._gsv_accum.setdefault(talker, []).extend(sats)
            if msg_num == num_msgs and self._gsv_expected.get(talker) == num_msgs:
                # Completed group for this talker — merge into the combined view
                combined: dict[str, list[dict[str, Any]]] = {
                    t: list(self._gsv_accum[t]) for t in self._gsv_accum
                }
                # Mark this talker's accumulation as complete by leaving the
                # list in _gsv_accum; next msg_num==1 will reset it.
                self._satellites_in_view = [
                    s for t in combined for s in combined[t]
                ]

    def _apply_vtg(self, msg: Any, now: float) -> None:
        speed_kn = _decimal_or_none(getattr(msg, "spd_over_grnd_kts", None))
        heading = _decimal_or_none(getattr(msg, "true_track", None))
        with self._lock:
            if self.last_fix is None:
                return
            fix = dict(self.last_fix)
            # RMC is authoritative for speed/heading; fill in only if unset
            if speed_kn is not None and fix.get("speed_kn") is None:
                fix["speed_kn"] = speed_kn
            if heading is not None and fix.get("heading_deg") is None:
                fix["heading_deg"] = heading
            self.last_fix = fix
            self._last_msg_time = now

    # ── gpsd TCP read loop ───────────────────────────────────────────────

    def _gpsd_read_loop(self) -> None:
        import socket as _socket

        attempt = 0
        while self._active:
            sock = None
            reader = None
            try:
                sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
                sock.settimeout(self._read_timeout)
                sock.connect((self._gpsd_host, self._gpsd_port))
                sock.sendall(b'?WATCH={"enable":true,"nmea":true}\r\n')
                reader = sock.makefile("rb")
            except (OSError, _socket.error) as exc:
                attempt += 1
                self._reconnect_failures += 1
                self.log.warning(
                    "gpsd connect failed (attempt %d): %s", attempt, exc
                )
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass
                if (
                    self._max_reconnect_attempts > 0
                    and attempt >= self._max_reconnect_attempts
                ):
                    self.log.error(
                        "gpsd exceeded max reconnect attempts (%d), giving up",
                        self._max_reconnect_attempts,
                    )
                    self._active = False
                    return
                delay = min(
                    self._reconnect_delay * (2 ** min(attempt - 1, 5)), 300.0
                )
                self._sleep_while_active(delay)
                continue

            # Successfully connected
            with self._lock:
                self._connected = True
            attempt = 0
            self.event_bus.publish(
                events.GPS_DEVICE_CONNECTED,
                {"source": "gpsd", "host": self._gpsd_host, "port": self._gpsd_port},
            )
            self.log.info("gpsd connected at %s:%d", self._gpsd_host, self._gpsd_port)

            try:
                while self._active:
                    try:
                        raw = reader.readline()
                    except (_socket.timeout, OSError) as exc:
                        if isinstance(exc, _socket.timeout):
                            continue  # read timeout — loop to check _active
                        self.log.warning("gpsd read failed: %s", exc)
                        break
                    if not raw:
                        self.log.warning("gpsd connection closed")
                        break
                    self._msgs_received += 1
                    line = raw.decode("ascii", errors="replace").strip()
                    if line.startswith("$"):
                        self._handle_sentence(line)
            finally:
                with self._lock:
                    self._connected = False
                try:
                    if reader:
                        reader.close()
                    if sock:
                        sock.close()
                except Exception:
                    pass
                self.event_bus.publish(
                    events.GPS_DEVICE_DISCONNECTED,
                    {"source": "gpsd", "host": self._gpsd_host},
                )

            if self._active:
                self._sleep_while_active(self._reconnect_delay)

    # ── Stale-fix monitor ───────────────────────────────────────────────

    def _stale_monitor(self) -> None:
        while self._active:
            self._sleep_while_active(self._stale_check_interval)
            if not self._active:
                return
            with self._lock:
                if not self._have_fix or self.last_fix is None:
                    continue
                age = time.time() - float(self.last_fix.get("timestamp", 0))
                if age <= self._fix_stale_seconds:
                    continue
                self._have_fix = False
                last_ts = self.last_fix.get("timestamp")
            self.event_bus.publish(
                events.GPS_FIX_LOST,
                {"age_s": age, "last_timestamp": last_ts},
            )
            self.log.warning("GPS fix stale (%.1fs old) — marking as lost", age)


# ── Helpers ─────────────────────────────────────────────────────────────


def _decimal_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None
