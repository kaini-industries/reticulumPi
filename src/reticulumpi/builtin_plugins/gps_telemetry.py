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
from reticulumpi.runtime_metrics import record_hung_worker
from reticulumpi.serial_devices import (
    SerialDeviceBusyError,
    SerialDeviceChangedError,
    SerialDeviceIdentityError,
    SerialDeviceLease,
    StaleSerialDeviceLeaseError,
    serial_device_registry,
    validate_stable_serial_path,
)

_SERIAL_CLOSE_TIMEOUT = 2.0


class GpsTelemetry(PluginBase):
    """Reads NMEA from a serial GPS receiver and exposes fix + satellite state."""

    plugin_name = "gps_telemetry"
    plugin_description = "NMEA GPS receiver telemetry"
    plugin_version = "1.0.1"
    broadcast_tier = 2
    broadcast_keys = "gps"

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

        port = self.config.get("serial_port", "/dev/gps")
        if source == "serial":
            validate_stable_serial_path(port)

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

        if source == "serial":
            silence = self.config.get("serial_silence_timeout", 30)
            if not isinstance(silence, (int, float)) or isinstance(silence, bool) or silence < 5:
                raise ValueError("serial_silence_timeout must be >= 5 seconds")
            if silence <= rt:
                raise ValueError("serial_silence_timeout must exceed read_timeout")

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
        previous_lock = getattr(self, "_lock", None)
        if previous_lock is not None:
            with previous_lock:
                teardown_incomplete = (
                    getattr(self, "_serial_reader_live", False)
                    or getattr(self, "_gpsd_reader_live", False)
                    or getattr(self, "_stale_monitor_live", False)
                    or bool(getattr(self, "_serial_close_attempts", set()))
                    or bool(getattr(self, "_unresolved_serial_handles", {}))
                    or (
                        (watcher := getattr(self, "_lease_release_watcher", None)) is not None
                        and watcher.is_alive()
                    )
                    or getattr(self, "_serial", None) is not None
                    or getattr(self, "_serial_device_lease", None) is not None
                    or getattr(self, "_gpsd_socket", None) is not None
                    or getattr(self, "_gpsd_reader", None) is not None
                )
            if teardown_incomplete:
                raise RuntimeError("GPS teardown is incomplete; refusing to restart")

        self._source: str = self.config.get("source", "serial")
        self._serial_port: str = self.config.get("serial_port", "/dev/gps")
        self._baudrate: int = self.config.get("baudrate", 4800)
        self._read_timeout: float = float(self.config.get("read_timeout", 2.0))
        self._serial_silence_timeout: float = (
            float(self.config.get("serial_silence_timeout", 30))
            if self._source == "serial"
            else 30.0
        )
        self._reconnect_delay: float = float(self.config.get("reconnect_delay", 5))
        self._max_reconnect_attempts: int = self.config.get("max_reconnect_attempts", 0)
        self._fix_stale_seconds: float = float(self.config.get("fix_stale_seconds", 30))
        self._stale_check_interval: float = float(self.config.get("stale_check_interval", 5))
        self._gpsd_host: str = self.config.get("gpsd_host", "localhost")
        self._gpsd_port: int = self.config.get("gpsd_port", 2947)

        self._lock = threading.Lock()
        self._serial_condition = threading.Condition(self._lock)
        self._lifecycle_generation = getattr(self, "_lifecycle_generation", 0) + 1
        lifecycle_generation = self._lifecycle_generation
        # Keep the historical serial generation field as an alias for focused
        # serial-reader fencing and compatibility with existing diagnostics.
        self._serial_generation = lifecycle_generation
        self._serial_reader_live = False
        self._gpsd_reader_live = False
        self._gpsd_reader_generation: int | None = None
        self._gpsd_socket: Any = None
        self._gpsd_reader: Any = None
        self._stale_monitor_live = False
        self._stale_monitor_generation: int | None = None
        self._serial_close_attempts: set[int] = set()
        self._unresolved_serial_handles: dict[int, Any] = {}
        self._lease_release_watcher: threading.Thread | None = None
        self._serial_teardown_complete = False
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
        self._serial_device_lease: SerialDeviceLease | None = None
        self._connected = False
        self._have_fix = False
        self._msgs_received = 0
        self._sentences_parsed = 0
        self._parse_errors = 0
        self._last_msg_monotonic = 0.0
        self._last_byte_monotonic = 0.0
        self._last_fix_monotonic = 0.0
        self._reconnect_failures = 0
        self._start_monotonic = time.monotonic()

        self._active = True
        if self._source == "gpsd":
            self._start_thread(
                lambda: self._gpsd_read_loop(lifecycle_generation),
                name="gps-gpsd-reader",
            )
        else:
            self._start_thread(
                lambda: self._read_loop(lifecycle_generation),
                name="gps-reader",
            )
        self._start_thread(
            lambda: self._stale_monitor(lifecycle_generation),
            name="gps-stale",
        )

        if self._source == "gpsd":
            self.log.info(
                "GPS telemetry started (gpsd %s:%d)",
                self._gpsd_host,
                self._gpsd_port,
            )
        else:
            self.log.info(
                "GPS telemetry started on %s @ %d baud",
                self._serial_port,
                self._baudrate,
            )

    def stop(self) -> None:
        # Serialize generation invalidation against gpsd result/event commits.
        # Once this gate is released, no stale gpsd read can mutate state or
        # publish an event for the stopped lifecycle.
        with self._lifecycle_lock:
            self._active = False
            with self._serial_condition:
                # Invalidate both already-open readers and constructors/connects
                # that may return after this stop request.
                self._lifecycle_generation += 1
                self._serial_generation = self._lifecycle_generation
                serial_handle = self._serial
                gpsd_socket = self._gpsd_socket
                if serial_handle is not None:
                    # Establish the teardown barrier before detaching the handle so
                    # neither restart nor a reconnect can treat the tty as reusable.
                    self._unresolved_serial_handles[id(serial_handle)] = serial_handle
                self._serial = None
                self._connected = False
                self._serial_condition.notify_all()

        if serial_handle is not None:
            self._close_serial_handle(serial_handle, "GPS serial handle during shutdown")
        if gpsd_socket is not None:
            self._shutdown_gpsd_socket(gpsd_socket)

        try:
            self._join_threads(timeout=5)
        finally:
            with self._serial_condition:
                self._serial_teardown_complete = True
                self._serial_condition.notify_all()
            # If the join timed out, the reader's outer finally performs this
            # check again after a blocked constructor/read call returns.
            released = self._release_serial_device_lease_if_quiescent()
            if not released:
                self._schedule_shutdown_lease_release()

    def _ensure_serial_device_lease(self) -> SerialDeviceLease:
        """Claim or revalidate the configured GPS serial endpoint."""

        with self._lock:
            lease = self._serial_device_lease
        if lease is not None:
            try:
                lease.revalidate()
            except (SerialDeviceChangedError, StaleSerialDeviceLeaseError):
                with self._lock:
                    if self._serial_device_lease is lease:
                        self._serial_device_lease = None
                        release_stale = True
                    else:
                        release_stale = False
                if release_stale:
                    lease.release()
            else:
                with self._lock:
                    if self._serial_device_lease is lease:
                        return lease
                raise StaleSerialDeviceLeaseError(
                    "GPS serial-device lease changed while validating"
                )

        claimed = serial_device_registry.claim(self._serial_port, self.plugin_name)
        try:
            claimed.revalidate()
        except Exception:
            claimed.release()
            raise

        with self._lock:
            if self._serial_device_lease is None:
                self._serial_device_lease = claimed
                return claimed
        claimed.release()
        raise StaleSerialDeviceLeaseError("GPS serial-device lease changed while claiming")

    def _release_serial_device_lease_if_quiescent(self) -> bool:
        """Release ownership only after serial teardown is fully proven."""

        with self._lock:
            if (
                getattr(self, "_source", "serial") != "serial"
                or self._active
                or not self._serial_teardown_complete
                or self._serial_reader_live
                or self._serial is not None
                or self._serial_close_attempts
                or self._unresolved_serial_handles
            ):
                return False
            lease = self._serial_device_lease
            self._serial_device_lease = None
        if lease is not None:
            lease.release()
        return True

    def _serial_generation_is_active(self, generation: int) -> bool:
        with self._lock:
            return self._active and self._serial_generation == generation

    def _close_serial_handle_once(self, serial_handle: Any, context: str) -> bool:
        """Attempt one close and retain the exact handle until it is proven closed."""

        handle_id = id(serial_handle)
        try:
            serial_handle.close()
        except BaseException:
            with self._serial_condition:
                self._unresolved_serial_handles[handle_id] = serial_handle
                self._serial_condition.notify_all()
            self.log.warning("Error closing %s; retaining device ownership", context, exc_info=True)
            return False

        with self._serial_condition:
            self._unresolved_serial_handles.pop(handle_id, None)
            self._serial_condition.notify_all()
        return True

    def _close_serial_handle(self, serial_handle: Any, context: str = "GPS serial handle") -> bool:
        """Bound close latency while retaining unresolved ownership fail closed."""

        handle_id = id(serial_handle)
        result = {"closed": False}
        with self._serial_condition:
            self._unresolved_serial_handles[handle_id] = serial_handle
            if handle_id in self._serial_close_attempts:
                return False
            self._serial_close_attempts.add(handle_id)

        def close_worker() -> None:
            try:
                result["closed"] = self._close_serial_handle_once(serial_handle, context)
            finally:
                with self._serial_condition:
                    self._serial_close_attempts.discard(handle_id)
                    self._serial_condition.notify_all()
                self._release_serial_device_lease_if_quiescent()

        thread = threading.Thread(
            target=close_worker,
            name=f"gps-serial-close-{handle_id}",
            daemon=True,
        )
        try:
            thread.start()
        except BaseException:
            with self._serial_condition:
                self._serial_close_attempts.discard(handle_id)
                self._serial_condition.notify_all()
            record_hung_worker()
            self.log.exception("Could not start %s close worker", context)
            return False

        thread.join(timeout=_SERIAL_CLOSE_TIMEOUT)
        if thread.is_alive():
            record_hung_worker()
            self.log.warning(
                "%s close exceeded %.1fs; retaining device ownership",
                context,
                _SERIAL_CLOSE_TIMEOUT,
            )
            return False
        return result["closed"]

    def _retry_unresolved_serial_handles(self) -> bool:
        """Retry failed closes before allowing any replacement serial open."""

        with self._serial_condition:
            if self._serial_close_attempts:
                return False
            handles = list(self._unresolved_serial_handles.values())
        for serial_handle in handles:
            if not self._close_serial_handle(serial_handle, "unresolved GPS serial handle"):
                return False
        with self._serial_condition:
            return not self._serial_close_attempts and not self._unresolved_serial_handles

    def _schedule_shutdown_lease_release(self) -> None:
        """Retry teardown and release the tty lease once shutdown is proven."""

        with self._serial_condition:
            existing = self._lease_release_watcher
            if existing is not None and existing.is_alive():
                return
            if self._serial_device_lease is None:
                return

        def watch() -> None:
            delay = 0.1
            try:
                while True:
                    with self._serial_condition:
                        if self._active or self._serial_device_lease is None:
                            return
                    self._retry_unresolved_serial_handles()
                    if self._release_serial_device_lease_if_quiescent():
                        return
                    with self._serial_condition:
                        self._serial_condition.wait(timeout=delay)
                    delay = min(delay * 2, 30.0)
            finally:
                with self._serial_condition:
                    if self._lease_release_watcher is threading.current_thread():
                        self._lease_release_watcher = None
                    self._serial_condition.notify_all()

        thread = threading.Thread(
            target=watch,
            name="gps-shutdown-release",
            daemon=True,
        )
        with self._serial_condition:
            self._lease_release_watcher = thread
        try:
            thread.start()
        except BaseException:
            with self._serial_condition:
                if self._lease_release_watcher is thread:
                    self._lease_release_watcher = None
                self._serial_condition.notify_all()
            record_hung_worker()
            self.log.exception("Could not start GPS shutdown release watcher")

    # ── Public status / snapshot API ────────────────────────────────────

    def get_status(self) -> dict[str, Any]:
        lock = getattr(self, "_lock", None)
        if lock is None:
            return {"active": False, "connected": False}
        with lock:
            now = time.monotonic()
            last_msg = getattr(self, "_last_msg_monotonic", 0.0)
            last_byte = getattr(self, "_last_byte_monotonic", 0.0)
            started = getattr(self, "_start_monotonic", now)
            last_age = max(0.0, now - last_msg) if last_msg else None
            return {
                "active": self._active,
                "connected": self._connected,
                "serial_port": self._serial_port,
                "baudrate": self._baudrate,
                "msgs_received": self._msgs_received,
                "sentences_parsed": self._sentences_parsed,
                "parse_errors": self._parse_errors,
                "last_msg_age_s": last_age,
                "serial_silence_s": max(0.0, now - last_byte) if last_byte else None,
                "serial_silence_timeout": getattr(
                    self,
                    "_serial_silence_timeout",
                    30.0,
                ),
                "reconnect_failures": self._reconnect_failures,
                "have_fix": self._have_fix,
                "satellites_in_view_count": len(self._satellites_in_view),
                "uptime": max(0.0, now - started),
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
                {**sat, "in_use": sat.get("prn") in in_use} for sat in self._satellites_in_view
            ]
            snap["satellites_used_prns"] = sorted(in_use)
        return snap

    # ── Read loop ───────────────────────────────────────────────────────

    def _read_loop(self, generation: int | None = None) -> None:
        import serial
        from serial import SerialException

        attempt = 0
        give_up = False
        with self._serial_condition:
            if generation is None:
                generation = self._serial_generation
            self._serial_reader_live = True
            self._serial_condition.notify_all()

        try:
            while self._serial_generation_is_active(generation):
                # A failed or still-running close owns the tty until that exact
                # handle is proven closed. Never overlap it with a replacement.
                if not self._retry_unresolved_serial_handles():
                    self._sleep_while_active(self._reconnect_delay)
                    continue
                try:
                    self._ensure_serial_device_lease()
                    if not self._serial_generation_is_active(generation):
                        break
                    ser = serial.Serial(
                        self._serial_port,
                        self._baudrate,
                        timeout=self._read_timeout,
                    )
                except (
                    SerialException,
                    OSError,
                    SerialDeviceBusyError,
                    SerialDeviceChangedError,
                    SerialDeviceIdentityError,
                    StaleSerialDeviceLeaseError,
                ) as exc:
                    if not self._serial_generation_is_active(generation):
                        break
                    attempt += 1
                    self._reconnect_failures += 1
                    self.log.warning("GPS open failed (attempt %d): %s", attempt, exc)
                    if self._max_reconnect_attempts > 0 and attempt >= self._max_reconnect_attempts:
                        self.log.error(
                            "GPS exceeded max reconnect attempts (%d), giving up",
                            self._max_reconnect_attempts,
                        )
                        self._active = False
                        give_up = True
                        break
                    delay = min(
                        self._reconnect_delay * (2 ** min(attempt - 1, 5)),
                        300.0,
                    )
                    self._sleep_while_active(delay)
                    continue

                # Publish a constructor result only if this exact generation
                # remains active.  A handle returned after stop is self-closed
                # below without ever becoming observable as connected.
                with self._serial_condition:
                    if self._active and self._serial_generation == generation:
                        self._serial = ser
                        self._connected = True
                        self._last_byte_monotonic = time.monotonic()
                        published = True
                    else:
                        published = False
                if not published:
                    self._close_serial_handle(ser)
                    break

                attempt = 0
                try:
                    self.event_bus.publish(
                        events.GPS_DEVICE_CONNECTED,
                        {"port": self._serial_port, "baudrate": self._baudrate},
                    )
                    self.log.info("GPS connected on %s", self._serial_port)

                    while self._serial_generation_is_active(generation):
                        try:
                            raw = ser.readline()
                        except (SerialException, OSError) as exc:
                            if self._serial_generation_is_active(generation):
                                self.log.warning("GPS read failed: %s", exc)
                            break
                        except TypeError:
                            # Closing pyserial can wake ``readline()`` with an
                            # internal ``None`` fd.  It is expected only after
                            # this generation has been cancelled.
                            if self._serial_generation_is_active(generation):
                                raise
                            break
                        # readline() may ignore its configured timeout or return
                        # a final buffered sentence after close(). Cancellation
                        # must win before any counters, fix state, or events move.
                        if not self._serial_generation_is_active(generation):
                            break
                        if not raw:
                            # A timeout that races with stop is normal; never
                            # turn it into a false serial-silence warning.
                            if not self._serial_generation_is_active(generation):
                                break
                            with self._lock:
                                last_byte = self._last_byte_monotonic
                            silence = time.monotonic() - last_byte
                            if (
                                silence >= self._serial_silence_timeout
                                and self._serial_generation_is_active(generation)
                            ):
                                self.log.warning(
                                    "GPS serial stream silent for %.1fs; "
                                    "reopening only this device",
                                    silence,
                                )
                                break
                            continue
                        with self._lock:
                            self._last_byte_monotonic = time.monotonic()
                        self._msgs_received += 1
                        line = raw.decode("ascii", errors="replace").strip()
                        if line.startswith("$"):
                            self._handle_sentence(line)
                finally:
                    with self._serial_condition:
                        close_here = self._serial is ser
                        if close_here:
                            self._serial = None
                        self._connected = False
                        self._serial_condition.notify_all()
                    if close_here:
                        self._close_serial_handle(ser)
                    self.event_bus.publish(
                        events.GPS_DEVICE_DISCONNECTED,
                        {"port": self._serial_port},
                    )

                if self._serial_generation_is_active(generation):
                    self._sleep_while_active(self._reconnect_delay)
        finally:
            with self._serial_condition:
                self._serial_reader_live = False
                if give_up:
                    self._serial_teardown_complete = True
                self._serial_condition.notify_all()
            released = self._release_serial_device_lease_if_quiescent()
            if not self._active and not released:
                self._schedule_shutdown_lease_release()

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
        observed_at = time.monotonic()
        kind = type(msg).__name__

        try:
            if kind == "RMC":
                self._apply_rmc(msg, now, observed_at)
            elif kind == "GGA":
                self._apply_gga(msg, now, observed_at)
            elif kind == "GSA":
                self._apply_gsa(msg)
            elif kind == "GSV":
                self._apply_gsv(msg)
            elif kind == "VTG":
                self._apply_vtg(msg, now, observed_at)
        except Exception:
            self._parse_errors += 1

    # ── Sentence handlers ───────────────────────────────────────────────

    def _apply_rmc(self, msg: Any, now: float, observed_at: float) -> None:
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
            self._last_msg_monotonic = observed_at
            self._last_fix_monotonic = observed_at
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

    def _apply_gga(self, msg: Any, now: float, observed_at: float) -> None:
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
            self._last_msg_monotonic = observed_at

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
                    "elevation_deg": _int_or_none(getattr(msg, f"elevation_deg_{i}", None)),
                    "azimuth_deg": _int_or_none(getattr(msg, f"azimuth_{i}", None)),
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
                self._satellites_in_view = [s for t in combined for s in combined[t]]

    def _apply_vtg(self, msg: Any, now: float, observed_at: float) -> None:
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
            self._last_msg_monotonic = observed_at

    # ── gpsd TCP read loop ───────────────────────────────────────────────

    def _gpsd_generation_is_active(self, generation: int) -> bool:
        with self._lock:
            return self._active and self._lifecycle_generation == generation

    @staticmethod
    def _shutdown_gpsd_socket(sock: Any) -> None:
        """Interrupt and close one exact gpsd socket without touching replacements."""

        import socket as _socket

        try:
            sock.shutdown(_socket.SHUT_RDWR)
        except (AttributeError, OSError):
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _bind_gpsd_socket(self, generation: int, sock: Any) -> bool:
        """Publish a connect-in-progress socket only for the current lifecycle."""

        with self._lifecycle_lock:
            with self._serial_condition:
                if (
                    not self._active
                    or self._lifecycle_generation != generation
                    or self._gpsd_socket is not None
                ):
                    return False
                self._gpsd_socket = sock
                self._serial_condition.notify_all()
                return True

    def _close_gpsd_endpoint(
        self,
        generation: int,
        sock: Any,
        reader: Any,
        *,
        connected_was_published: bool,
    ) -> None:
        """Close one gpsd endpoint and clear state only if it still owns it."""

        self._shutdown_gpsd_socket(sock)
        if reader is not None:
            try:
                reader.close()
            except OSError:
                pass

        with self._lifecycle_lock:
            with self._serial_condition:
                owns_socket = self._gpsd_socket is sock
                if owns_socket:
                    self._gpsd_socket = None
                if self._gpsd_reader is reader:
                    self._gpsd_reader = None
                current = owns_socket and self._active and self._lifecycle_generation == generation
                if current:
                    self._connected = False
                self._serial_condition.notify_all()

            if connected_was_published and current:
                self.event_bus.publish(
                    events.GPS_DEVICE_DISCONNECTED,
                    {"source": "gpsd", "host": self._gpsd_host},
                )

    def _gpsd_read_loop(self, generation: int | None = None) -> None:
        import socket as _socket

        attempt = 0
        with self._lifecycle_lock:
            with self._serial_condition:
                if generation is None:
                    generation = self._lifecycle_generation
                if not self._active or self._lifecycle_generation != generation:
                    return
                self._gpsd_reader_live = True
                self._gpsd_reader_generation = generation
                self._serial_condition.notify_all()

        try:
            while self._gpsd_generation_is_active(generation):
                sock = None
                reader = None
                endpoint_bound = False
                connected_was_published = False
                try:
                    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
                    if not self._bind_gpsd_socket(generation, sock):
                        self._shutdown_gpsd_socket(sock)
                        break
                    endpoint_bound = True
                    sock.settimeout(self._read_timeout)
                    sock.connect((self._gpsd_host, self._gpsd_port))
                    sock.sendall(b'?WATCH={"enable":true,"nmea":true}\r\n')
                    reader = sock.makefile("rb")
                except (OSError, _socket.error) as exc:
                    if not self._gpsd_generation_is_active(generation):
                        if endpoint_bound:
                            self._close_gpsd_endpoint(
                                generation,
                                sock,
                                reader,
                                connected_was_published=False,
                            )
                        elif sock is not None:
                            self._shutdown_gpsd_socket(sock)
                        break
                    attempt += 1
                    self._reconnect_failures += 1
                    self.log.warning("gpsd connect failed (attempt %d): %s", attempt, exc)
                    if endpoint_bound:
                        self._close_gpsd_endpoint(
                            generation,
                            sock,
                            reader,
                            connected_was_published=False,
                        )
                    elif sock is not None:
                        self._shutdown_gpsd_socket(sock)
                    if self._max_reconnect_attempts > 0 and attempt >= self._max_reconnect_attempts:
                        self.log.error(
                            "gpsd exceeded max reconnect attempts (%d), giving up",
                            self._max_reconnect_attempts,
                        )
                        with self._lifecycle_lock:
                            if self._gpsd_generation_is_active(generation):
                                self._active = False
                        return
                    delay = min(
                        self._reconnect_delay * (2 ** min(attempt - 1, 5)),
                        300.0,
                    )
                    self._sleep_while_active(delay)
                    continue

                with self._lifecycle_lock:
                    with self._serial_condition:
                        current = (
                            self._active
                            and self._lifecycle_generation == generation
                            and self._gpsd_socket is sock
                        )
                        if current:
                            self._gpsd_reader = reader
                            self._connected = True
                            attempt = 0
                        self._serial_condition.notify_all()
                    if current:
                        self.event_bus.publish(
                            events.GPS_DEVICE_CONNECTED,
                            {
                                "source": "gpsd",
                                "host": self._gpsd_host,
                                "port": self._gpsd_port,
                            },
                        )
                        connected_was_published = True
                        self.log.info(
                            "gpsd connected at %s:%d",
                            self._gpsd_host,
                            self._gpsd_port,
                        )

                if not current:
                    self._close_gpsd_endpoint(
                        generation,
                        sock,
                        reader,
                        connected_was_published=False,
                    )
                    break

                try:
                    while self._gpsd_generation_is_active(generation):
                        try:
                            raw = reader.readline()
                        except (_socket.timeout, OSError) as exc:
                            with self._lifecycle_lock:
                                if not self._gpsd_generation_is_active(generation):
                                    break
                                if isinstance(exc, _socket.timeout):
                                    continue
                                self.log.warning("gpsd read failed: %s", exc)
                            break

                        with self._lifecycle_lock:
                            if not self._gpsd_generation_is_active(generation):
                                break
                            if not raw:
                                self.log.warning("gpsd connection closed")
                                break
                            self._msgs_received += 1
                            line = raw.decode("ascii", errors="replace").strip()
                            if line.startswith("$"):
                                self._handle_sentence(line)
                finally:
                    self._close_gpsd_endpoint(
                        generation,
                        sock,
                        reader,
                        connected_was_published=connected_was_published,
                    )

                if self._gpsd_generation_is_active(generation):
                    self._sleep_while_active(self._reconnect_delay)
        finally:
            with self._serial_condition:
                if self._gpsd_reader_generation == generation:
                    self._gpsd_reader_generation = None
                    self._gpsd_reader_live = False
                self._serial_condition.notify_all()

    # ── Stale-fix monitor ───────────────────────────────────────────────

    def _stale_monitor(self, generation: int | None = None) -> None:
        with self._lifecycle_lock:
            with self._lock:
                if generation is None:
                    generation = getattr(self, "_lifecycle_generation", 0)
                current_generation = getattr(self, "_lifecycle_generation", generation)
                if not self._active or current_generation != generation:
                    return
                self._stale_monitor_live = True
                self._stale_monitor_generation = generation

        try:
            while True:
                self._sleep_while_active(self._stale_check_interval)
                with self._lifecycle_lock:
                    with self._lock:
                        current_generation = getattr(
                            self,
                            "_lifecycle_generation",
                            generation,
                        )
                        if not self._active or current_generation != generation:
                            return
                        if not self._have_fix or self.last_fix is None:
                            continue
                        observed_at = getattr(self, "_last_fix_monotonic", 0.0)
                        if not observed_at:
                            # In-memory legacy/test state has no safe conversion from
                            # an epoch timestamp to a monotonic value. Start its age
                            # window now rather than spuriously declaring it stale.
                            self._last_fix_monotonic = time.monotonic()
                            continue
                        age = max(0.0, time.monotonic() - observed_at)
                        if age <= self._fix_stale_seconds:
                            continue
                        self._have_fix = False
                        last_ts = self.last_fix.get("timestamp")
                    self.event_bus.publish(
                        events.GPS_FIX_LOST,
                        {"age_s": age, "last_timestamp": last_ts},
                    )
                    self.log.warning("GPS fix stale (%.1fs old) — marking as lost", age)
        finally:
            with self._lock:
                if self._stale_monitor_generation == generation:
                    self._stale_monitor_generation = None
                    self._stale_monitor_live = False


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
