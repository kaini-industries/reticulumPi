"""Radiosonde weather balloon tracker plugin.

Decodes RS41/DFM radiosonde telemetry using rtl_fm piped to rs41mod.
Activates during configurable NWS launch windows.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections import deque
from typing import Any

from reticulumpi import events
from reticulumpi.builtin_plugins.signal_plugin_base import SignalPluginBase
from reticulumpi.sdr_scheduler import PRIORITY_SCHEDULED


class RadiosondeTracker(SignalPluginBase):
    """Radiosonde weather balloon tracker."""

    plugin_name = "radiosonde_tracker"
    plugin_version = "0.1.0"
    plugin_description = "Radiosonde weather balloon tracker"
    broadcast_keys = "radiosonde"

    signal_priority = PRIORITY_SCHEDULED
    signal_continuous = False
    signal_label = "Radiosonde Tracker"

    def validate_config(self) -> None:
        self._gain = self.config.get("gain", 40.0)
        self._ppm = int(self.config.get("ppm", 0))
        self._decoder_bin = str(self.config.get("decoder_bin", "rs41mod"))
        self._default_freq_hz = int(
            float(self.config.get("default_freq_mhz", 404.8)) * 1_000_000,
        )
        self._scan_freqs = self.config.get("scan_freqs_mhz", [404.8])
        self._launch_windows_utc = self.config.get(
            "launch_windows_utc", ["11:15", "23:15"],
        )
        self._window_duration_min = int(
            self.config.get("launch_window_duration_min", 120),
        )
        self._stale_timeout = float(self.config.get("stale_timeout", 300))
        self._max_profile_points = int(self.config.get("max_profile_points", 2000))
        self._max_track_points = int(self.config.get("max_track_points", 500))
        self._max_restarts = int(self.config.get("max_restarts", 5))

    def _on_start(self) -> None:
        self._active_sonde: dict[str, Any] | None = None
        self._altitude_profile: deque[dict[str, Any]] = deque(
            maxlen=self._max_profile_points,
        )
        self._position_track: deque[dict[str, Any]] = deque(
            maxlen=self._max_track_points,
        )
        self._recent_sondes: deque[dict[str, Any]] = deque(maxlen=10)
        self._stats = {
            "sondes_tracked_total": 0,
            "frames_decoded_total": 0,
            "current_session_frames": 0,
        }
        self._status = "idle"
        self._last_error: str | None = None
        self._restart_count = 0
        self._frame_count = 0
        self._last_sonde_frame_ts = 0.0

        self._schedule_windows()
        self._start_thread(self._window_scheduler_loop, name="sonde-scheduler")

    def _schedule_windows(self) -> None:
        sched = getattr(self.app, "sdr_scheduler", None)
        if sched is None or not self._dongle_serial:
            return

        sched.remove_windows(self._dongle_serial, self.plugin_name)
        now = time.time()
        gm = time.gmtime(now)
        midnight_utc = now - (gm.tm_hour * 3600 + gm.tm_min * 60 + gm.tm_sec)

        for time_str in self._launch_windows_utc:
            parts = time_str.split(":")
            if len(parts) != 2:
                continue
            try:
                h, m = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            window_start = midnight_utc + h * 3600 + m * 60
            if window_start < now:
                window_start += 86400
            window_end = window_start + self._window_duration_min * 60

            sched.add_window(
                serial=self._dongle_serial,
                caller=self.plugin_name,
                start_ts=window_start,
                end_ts=window_end,
                label=f"Radiosonde {time_str} UTC window",
            )

    def _window_scheduler_loop(self) -> None:
        while self._active:
            self._sleep_while_active(3600.0)
            if not self._active:
                break
            self._schedule_windows()

    def _launch_subprocess(self, device_index: int) -> None:
        rtl_fm = shutil.which("rtl_fm")
        decoder = shutil.which(self._decoder_bin)
        if not rtl_fm or not decoder:
            missing = []
            if not rtl_fm:
                missing.append("rtl_fm")
            if not decoder:
                missing.append(self._decoder_bin)
            self._status = "unavailable"
            self._last_error = f"Missing: {', '.join(missing)}"
            self.log.warning(self._last_error)
            return

        freq_hz = self._default_freq_hz
        rtl_cmd = [
            rtl_fm,
            "-d", str(device_index),
            "-f", str(freq_hz),
            "-s", "48000",
            "-M", "fm",
            "-p", str(self._ppm),
        ]
        if self._gain is not None:
            rtl_cmd += ["-g", str(self._gain)]
        rtl_cmd.append("-")

        decoder_cmd = [decoder, "--ptu", "--json", "--ecc2"]

        self.log.debug(
            "Launching: %s | %s", " ".join(rtl_cmd), " ".join(decoder_cmd),
        )

        rtl_proc = subprocess.Popen(
            rtl_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._process = subprocess.Popen(
            decoder_cmd,
            stdin=rtl_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._rtl_process = rtl_proc
        if rtl_proc.stdout:
            rtl_proc.stdout.close()
        self._pid = self._process.pid
        self._status = "scanning"
        self._restart_count = 0
        self._frame_count = 0

        self._start_log_reader(self._stderr_fake(rtl_proc), prefix="rtl_fm")
        self._start_thread(self._parser_loop, name="sonde-parser")

        self.log.info(
            "Radiosonde scanner started at %.3f MHz (PID %d)",
            freq_hz / 1_000_000, self._pid,
        )

    @staticmethod
    def _stderr_fake(proc: subprocess.Popen) -> Any:
        class _F:
            pass
        f = _F()
        f.stdout = proc.stderr  # type: ignore[attr-defined]
        return f

    def _kill_subprocess(self) -> None:
        rtl = getattr(self, "_rtl_process", None)
        self._rtl_process = None  # type: ignore[assignment]
        super()._kill_subprocess()
        if rtl is not None:
            try:
                if rtl.poll() is None:
                    rtl.terminate()
                    try:
                        rtl.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        rtl.kill()
                        rtl.wait(timeout=2)
            except Exception:
                pass
            finally:
                for f in (rtl.stdout, rtl.stderr):
                    if f:
                        try:
                            f.close()
                        except Exception:
                            pass

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
                    frame = json.loads(text)
                except json.JSONDecodeError:
                    continue
                self._handle_frame(frame)
        except (ValueError, OSError):
            pass
        except Exception:
            self.log.exception("Radiosonde parser crashed")

    def _handle_frame(self, frame: dict[str, Any]) -> None:
        sonde_id = frame.get("id", "")
        if not sonde_id:
            return

        now = time.time()
        self._last_sonde_frame_ts = now
        self._frame_count += 1
        self._stats["frames_decoded_total"] += 1
        self._stats["current_session_frames"] = self._frame_count

        lat = frame.get("lat")
        lon = frame.get("lon")
        alt = frame.get("alt")
        temp = frame.get("temp")
        humidity = frame.get("humidity")
        pressure = frame.get("pressure")
        vel_h = frame.get("vel_h", 0)
        vel_v = frame.get("vel_v", 0)
        sonde_type = frame.get("type", "RS41")

        if self._active_sonde is None or self._active_sonde.get("id") != sonde_id:
            if self._active_sonde is not None:
                self._finalize_sonde()
            self._active_sonde = {
                "id": sonde_id,
                "type": sonde_type,
                "freq_mhz": round(self._default_freq_hz / 1_000_000, 3),
                "first_seen": now,
                "frame_count": 0,
                "phase": "ascent",
            }
            self._altitude_profile.clear()
            self._position_track.clear()
            self._stats["sondes_tracked_total"] += 1
            self._status = "tracking"

            self.log.info("New sonde detected: %s (%s)", sonde_id, sonde_type)
            try:
                self.event_bus.publish(events.RADIOSONDE_DETECTED, {
                    "id": sonde_id, "type": sonde_type,
                })
            except Exception:
                pass

        sonde = self._active_sonde
        sonde["frame_count"] += 1
        sonde["last_seen"] = now
        if lat is not None:
            sonde["lat"] = lat
        if lon is not None:
            sonde["lon"] = lon
        if alt is not None:
            sonde["alt_m"] = alt
        sonde["vel_h_ms"] = vel_h
        sonde["vel_v_ms"] = vel_v
        if temp is not None:
            sonde["temp_c"] = temp
        if humidity is not None:
            sonde["humidity_pct"] = humidity
        if pressure is not None:
            sonde["pressure_hpa"] = pressure

        if alt is not None and vel_v < -2 and alt > 15000:
            if sonde.get("phase") == "ascent":
                sonde["phase"] = "burst"
                sonde["burst_alt_m"] = alt
                self.log.info("Sonde %s burst at %.0f m", sonde_id, alt)
                try:
                    self.event_bus.publish(events.RADIOSONDE_BURST, {
                        "id": sonde_id, "alt_m": alt,
                    })
                except Exception:
                    pass
        elif sonde.get("phase") == "burst" and vel_v < 0:
            sonde["phase"] = "descent"

        if alt is not None:
            self._altitude_profile.append({
                "ts": now, "alt_m": alt,
                "temp_c": temp, "vel_v": vel_v,
            })

        if lat is not None and lon is not None:
            if len(self._position_track) == 0 or self._frame_count % 4 == 0:
                self._position_track.append({
                    "ts": now, "lat": lat, "lon": lon,
                    "alt_m": alt,
                })

        self._update_snapshot_cache()

    def _finalize_sonde(self) -> None:
        sonde = self._active_sonde
        if sonde is None:
            return
        summary = {
            "id": sonde.get("id"),
            "type": sonde.get("type"),
            "launched_at": sonde.get("first_seen"),
            "burst_alt_m": sonde.get("burst_alt_m"),
            "duration_s": time.time() - sonde.get("first_seen", time.time()),
            "frame_count": sonde.get("frame_count", 0),
            "landed_lat": sonde.get("lat"),
            "landed_lon": sonde.get("lon"),
        }
        self._recent_sondes.appendleft(summary)
        self.log.info("Sonde %s finalized (%d frames)", sonde.get("id"), sonde.get("frame_count", 0))

    def _next_launch_window(self) -> dict[str, Any] | None:
        now = time.time()
        gm = time.gmtime(now)
        midnight_utc = now - (gm.tm_hour * 3600 + gm.tm_min * 60 + gm.tm_sec)

        best: dict[str, Any] | None = None
        for time_str in self._launch_windows_utc:
            parts = time_str.split(":")
            if len(parts) != 2:
                continue
            try:
                h, m = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            ts = midnight_utc + h * 3600 + m * 60
            if ts < now:
                ts += 86400
            countdown = ts - now
            if best is None or countdown < best["countdown_s"]:
                best = {
                    "expected_ts": ts,
                    "countdown_s": countdown,
                    "label": f"{time_str} UTC window",
                }
        return best

    def _update_snapshot_cache(self) -> None:
        with self._cache_lock:
            self._snapshot_cache = {
                "status": self._status,
                "error": self._last_error,
                "active_sonde": dict(self._active_sonde) if self._active_sonde else None,
                "altitude_profile": list(self._altitude_profile),
                "position_track": list(self._position_track),
                "recent_sondes": list(self._recent_sondes),
                "next_launch": self._next_launch_window(),
                "stats": dict(self._stats),
            }

    def get_status(self) -> dict[str, Any]:
        sonde = self._active_sonde
        return {
            "active": self._active,
            "status": self._status,
            "error": self._last_error,
            "sondes_tracked": self._stats["sondes_tracked_total"],
            "frames_decoded": self._stats["frames_decoded_total"],
            "active_sonde_id": sonde.get("id") if sonde else None,
        }
