"""FM/AM radio receiver plugin using rtl_fm.

Manages an rtl_fm subprocess to demodulate FM/AM/SSB signals and stream
live signed-16-bit PCM audio to the web dashboard via chunked HTTP.
"""

from __future__ import annotations

import asyncio
import math
import shutil
import struct
import subprocess
import threading
import time
from typing import Any

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase

_VALID_MODES = ("wbfm", "fm", "am", "usb", "lsb")

_MODE_DEFAULTS: dict[str, dict[str, int]] = {
    "wbfm": {"sample_rate_hz": 170_000, "output_rate_hz": 32_000},
    "fm":   {"sample_rate_hz": 12_000,  "output_rate_hz": 12_000},
    "am":   {"sample_rate_hz": 12_000,  "output_rate_hz": 12_000},
    "usb":  {"sample_rate_hz": 12_000,  "output_rate_hz": 12_000},
    "lsb":  {"sample_rate_hz": 12_000,  "output_rate_hz": 12_000},
}

_E4000_LO_GAP_MHZ = (1101.0, 1234.0)

_COMMON_GAIN_STEPS_DB = (
    -1.0, 1.5, 4.0, 6.5, 9.0, 11.5, 14.0, 16.5,
    19.0, 21.5, 24.0, 29.0, 34.0, 42.0,
)

_BUILTIN_PRESETS: dict[str, dict[str, Any]] = {
    "aviation": {
        "label": "Aviation",
        "mode": "am",
        "frequencies": [
            {"freq_mhz": 121.5, "label": "Guard"},
            {"freq_mhz": 123.45, "label": "Unicom"},
        ],
    },
    "marine": {
        "label": "Marine VHF",
        "mode": "fm",
        "frequencies": [
            {"freq_mhz": 156.8, "label": "Ch 16 (distress)"},
            {"freq_mhz": 156.45, "label": "Ch 9 (calling)"},
        ],
    },
    "weather": {
        "label": "NOAA Weather",
        "mode": "fm",
        "frequencies": [
            {"freq_mhz": 162.4, "label": "WX1"},
            {"freq_mhz": 162.425, "label": "WX2"},
            {"freq_mhz": 162.45, "label": "WX3"},
            {"freq_mhz": 162.475, "label": "WX4"},
            {"freq_mhz": 162.5, "label": "WX5"},
            {"freq_mhz": 162.525, "label": "WX6"},
            {"freq_mhz": 162.55, "label": "WX7"},
        ],
    },
    "two_meter_ham": {
        "label": "2m Ham",
        "mode": "fm",
        "frequencies": [
            {"freq_mhz": 146.52, "label": "National simplex"},
        ],
    },
    "seventy_cm_ham": {
        "label": "70cm Ham",
        "mode": "fm",
        "frequencies": [
            {"freq_mhz": 446.0, "label": "National simplex"},
        ],
    },
    "gmrs_frs": {
        "label": "GMRS/FRS",
        "mode": "fm",
        "frequencies": [
            {"freq_mhz": 462.5625, "label": "GMRS Ch 1"},
            {"freq_mhz": 462.5875, "label": "GMRS Ch 2"},
            {"freq_mhz": 462.6125, "label": "GMRS Ch 3"},
        ],
    },
    "ism_433": {
        "label": "ISM 433 MHz",
        "mode": "fm",
        "frequencies": [
            {"freq_mhz": 433.92, "label": "ISM center"},
        ],
    },
    "ism_915": {
        "label": "ISM 915 MHz",
        "mode": "fm",
        "frequencies": [
            {"freq_mhz": 915.0, "label": "ISM center"},
        ],
    },
    "railroad": {
        "label": "Railroad",
        "mode": "am",
        "frequencies": [
            {"freq_mhz": 160.215, "label": "AAR Ch 1"},
            {"freq_mhz": 161.37, "label": "AAR End-of-train"},
        ],
    },
}

_CHUNK_BYTES = 4096
_QUEUE_MAXSIZE = 64


class FMReceiver(PluginBase):
    """FM/AM radio receiver via RTL-SDR with live audio streaming."""

    plugin_name = "fm_receiver"
    plugin_version = "0.1.0"
    plugin_description = "FM/AM radio receiver via RTL-SDR"
    broadcast_tier = 2
    broadcast_keys = "fm_receiver"

    # ── config validation ────────────────────────────────────────────

    def validate_config(self) -> None:
        cfg = self.config

        self._freq_min_mhz = float(cfg.get("freq_min_mhz", 52.0))
        self._freq_max_mhz = float(cfg.get("freq_max_mhz", 2200.0))

        freq = float(cfg.get("default_frequency_mhz", 95.5))
        self._validate_frequency(freq)
        self._frequency_hz = int(freq * 1_000_000)

        mode = str(cfg.get("default_mode", "wbfm")).lower()
        if mode not in _VALID_MODES:
            raise ValueError(f"default_mode must be one of {_VALID_MODES}, got '{mode}'")
        self._mode = mode

        defaults = _MODE_DEFAULTS[self._mode]
        self._sample_rate_hz = int(cfg.get("sample_rate_hz", defaults["sample_rate_hz"]))
        self._output_rate_hz = int(cfg.get("output_rate_hz", defaults["output_rate_hz"]))

        gain_db = cfg.get("gain_db", None)
        if gain_db is not None:
            gain_db = float(gain_db)
            if not -10.0 <= gain_db <= 60.0:
                raise ValueError(f"gain_db must be -10..60 or null (auto), got {gain_db}")
        self._gain_db = gain_db

        self._ppm = int(cfg.get("ppm", 0))
        self._squelch_level = int(cfg.get("squelch_level", 0))
        self._volume = max(0.0, min(1.0, float(cfg.get("default_volume", 75)) / 100.0))
        self._enable_bias_tee = bool(cfg.get("enable_bias_tee", False))
        self._max_restarts = int(cfg.get("max_restarts", 5))
        self._auto_play = bool(cfg.get("auto_play", False))
        self._audio_buffer_seconds = max(1, int(cfg.get("audio_buffer_seconds", 4)))

        self._device_id = str(cfg.get("device_serial") or cfg.get("device_index", "0"))

        self._presets: dict[str, dict[str, Any]] = dict(_BUILTIN_PRESETS)
        user_presets = cfg.get("presets")
        if isinstance(user_presets, dict):
            for name, preset in user_presets.items():
                if name in self._presets:
                    merged = dict(self._presets[name])
                    merged.update(preset)
                    self._presets[name] = merged
                else:
                    self._presets[name] = preset

    def _validate_frequency(self, freq_mhz: float) -> None:
        if not self._freq_min_mhz <= freq_mhz <= self._freq_max_mhz:
            raise ValueError(
                f"Frequency {freq_mhz} MHz outside tuner range "
                f"{self._freq_min_mhz}-{self._freq_max_mhz} MHz"
            )

    def _check_dead_zone(self, freq_mhz: float) -> str | None:
        gap_lo, gap_hi = _E4000_LO_GAP_MHZ
        if gap_lo <= freq_mhz <= gap_hi:
            return (
                f"Frequency {freq_mhz:.3f} MHz is in the E4000 L-band LO gap "
                f"({gap_lo:.0f}-{gap_hi:.0f} MHz); signal may be unreliable"
            )
        return None

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        self._state_lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._pid: int | None = None
        self._restart_count = 0
        self._rtl_fm_path: str | None = None
        self._last_error: str | None = None
        self._status = "stopped"
        self._playing = False
        self._resolved_index: int | None = None
        self._supervisor_alive = False

        self._signal_rms: float = 0.0
        self._signal_db: float = -90.0
        self._dead_zone_warning: str | None = None

        self._stream_queues: list[asyncio.Queue] = []
        self._stream_lock = threading.Lock()
        self._event_loop: asyncio.AbstractEventLoop | None = None

        try:
            from reticulumpi.rtlsdr import resolve_device
            self._resolved_index = resolve_device(self._device_id, caller=self.plugin_name)
        except (RuntimeError, ValueError) as exc:
            self.log.error("RTL-SDR device resolution failed: %s", exc)
            self._set_status("error", str(exc))

        self._active = True

        freq_mhz = self._frequency_hz / 1_000_000
        warning = self._check_dead_zone(freq_mhz)
        if warning:
            self._dead_zone_warning = warning
            self.log.warning(warning)

        if self._auto_play:
            self.play()

        self.log.info(
            "%s started: %.3f MHz %s, device=%s",
            self.plugin_name,
            freq_mhz,
            self._mode.upper(),
            self._device_id,
        )

    def stop(self) -> None:
        self._active = False
        self._terminate_process()
        self._notify_clients_stopped()
        try:
            from reticulumpi.rtlsdr import release_device
            release_device(self._device_id, caller=self.plugin_name)
        except Exception:
            pass
        self._join_threads(timeout=5.0)
        self._set_status("stopped")

    # ── public API ───────────────────────────────────────────────────

    def play(self) -> dict[str, Any]:
        if self._playing or self._supervisor_alive:
            return {"status": "already_playing"}
        if self._resolved_index is None:
            return {"status": "error", "error": "No RTL-SDR device resolved"}
        self._playing = True
        self._restart_count = 0
        self._supervisor_alive = True
        self._start_thread(self._supervisor_loop, name="fm-supervisor")
        return {"status": "starting", "frequency_mhz": self._frequency_hz / 1_000_000}

    def stop_playback(self) -> dict[str, Any]:
        if not self._playing:
            return {"status": "already_stopped"}
        self._playing = False
        self._terminate_process()
        self._notify_clients_stopped()
        self._set_status("stopped")
        return {"status": "stopped"}

    def tune(self, frequency_hz: int, mode: str | None = None) -> dict[str, Any]:
        freq_mhz = frequency_hz / 1_000_000
        self._validate_frequency(freq_mhz)

        if mode is not None:
            mode = mode.lower()
            if mode not in _VALID_MODES:
                raise ValueError(f"Invalid mode '{mode}'. Must be one of {_VALID_MODES}")
            if mode != self._mode:
                self._mode = mode
                defaults = _MODE_DEFAULTS[mode]
                self._sample_rate_hz = defaults["sample_rate_hz"]
                self._output_rate_hz = defaults["output_rate_hz"]

        self._frequency_hz = frequency_hz
        self._dead_zone_warning = self._check_dead_zone(freq_mhz)
        if self._dead_zone_warning:
            self.log.warning(self._dead_zone_warning)

        if self._playing:
            self._terminate_process()
            self._notify_clients_stopped()
            self._restart_count = 0

        try:
            self.event_bus.publish(events.FM_RECEIVER_TUNED, {
                "frequency_hz": frequency_hz,
                "frequency_mhz": freq_mhz,
                "mode": self._mode,
            })
        except Exception:
            self.log.debug("event_bus publish failed", exc_info=True)

        return {
            "frequency_mhz": freq_mhz,
            "mode": self._mode,
            "dead_zone_warning": self._dead_zone_warning,
        }

    def set_gain(self, gain_db: float | None) -> dict[str, Any]:
        if gain_db is not None and not -10.0 <= gain_db <= 60.0:
            raise ValueError(f"gain_db must be -10..60 or null (auto), got {gain_db}")
        self._gain_db = gain_db
        if self._playing:
            self._terminate_process()
            self._notify_clients_stopped()
            self._restart_count = 0
        return {"gain_db": self._gain_db}

    def set_squelch(self, level: int) -> dict[str, Any]:
        self._squelch_level = max(0, int(level))
        if self._playing:
            self._terminate_process()
            self._notify_clients_stopped()
            self._restart_count = 0
        return {"squelch_level": self._squelch_level}

    def set_volume(self, volume: float) -> dict[str, Any]:
        self._volume = max(0.0, min(1.0, float(volume)))
        return {"volume": self._volume}

    def get_presets(self) -> dict[str, Any]:
        result = {}
        for name, p in self._presets.items():
            result[name] = {
                "label": p.get("label", name),
                "mode": p.get("mode", "fm"),
                "frequencies": p.get("frequencies", []),
            }
        return result

    # ── audio client management ──────────────────────────────────────

    def register_audio_client(self, queue: asyncio.Queue) -> None:
        with self._stream_lock:
            self._stream_queues.append(queue)

    def unregister_audio_client(self, queue: asyncio.Queue) -> None:
        with self._stream_lock:
            try:
                self._stream_queues.remove(queue)
            except ValueError:
                pass

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._event_loop = loop

    def _push_audio_chunk(self, chunk: bytes) -> None:
        loop = self._event_loop
        if loop is None:
            return
        with self._stream_lock:
            queues = list(self._stream_queues)
        for q in queues:
            try:
                loop.call_soon_threadsafe(q.put_nowait, chunk)
            except (asyncio.QueueFull, RuntimeError):
                pass

    def _notify_clients_stopped(self) -> None:
        loop = self._event_loop
        if loop is None:
            return
        with self._stream_lock:
            queues = list(self._stream_queues)
        for q in queues:
            try:
                if loop and loop.is_running():
                    loop.call_soon_threadsafe(q.put_nowait, None)
                else:
                    q.put_nowait(None)
            except (asyncio.QueueFull, RuntimeError):
                pass

    # ── broadcast snapshot ───────────────────────────────────────────

    def get_snapshot(self) -> dict[str, Any]:
        freq_mhz = self._frequency_hz / 1_000_000
        with self._stream_lock:
            client_count = len(self._stream_queues)
        return {
            "status": self._status,
            "playing": self._playing,
            "frequency_hz": self._frequency_hz,
            "frequency_mhz": round(freq_mhz, 4),
            "mode": self._mode,
            "gain_db": self._gain_db,
            "squelch_level": self._squelch_level,
            "volume": round(self._volume, 2),
            "signal_rms": round(self._signal_rms, 1),
            "signal_db": round(self._signal_db, 1),
            "output_rate_hz": self._output_rate_hz,
            "freq_min_mhz": self._freq_min_mhz,
            "freq_max_mhz": self._freq_max_mhz,
            "restart_count": self._restart_count,
            "error": self._last_error,
            "dead_zone_warning": self._dead_zone_warning,
            "audio_clients": client_count,
        }

    def get_status(self) -> dict[str, Any]:
        running = self._process is not None and self._process.poll() is None
        return {
            "active": self._active,
            "playing": self._playing,
            "running": running,
            "pid": self._pid,
            "status": self._status,
            "frequency_mhz": self._frequency_hz / 1_000_000,
            "mode": self._mode,
            "error": self._last_error,
        }

    # ── supervisor ───────────────────────────────────────────────────

    def _supervisor_loop(self) -> None:
        self._supervisor_alive = True
        try:
            self._supervisor_loop_inner()
        finally:
            self._supervisor_alive = False

    def _supervisor_loop_inner(self) -> None:
        self._rtl_fm_path = shutil.which("rtl_fm")
        if not self._rtl_fm_path:
            self._set_status("unavailable", "rtl_fm not found on PATH")
            self.log.warning("rtl_fm binary not found; %s will stay idle.", self.plugin_name)
            return

        while self._active and self._playing:
            try:
                self._launch_rtl_fm()
            except Exception as exc:
                self._set_status("error", f"launch failed: {exc}")
                self.log.exception("Failed to launch rtl_fm")
                break

            reader = self._start_thread(self._audio_reader_loop, name="fm-audio-reader")

            while self._active and self._playing and self._process is not None:
                rc = self._process.poll()
                if rc is not None:
                    self.log.warning("rtl_fm exited (code %s)", rc)
                    break
                if not reader.is_alive():
                    self.log.warning("Audio reader thread exited unexpectedly")
                    break
                self._sleep_while_active(1.0)

            reader.join(timeout=2.0)
            self._remove_thread(reader)
            self._terminate_process()

            if not self._active or not self._playing:
                break

            self._restart_count += 1
            if self._restart_count > self._max_restarts:
                self._set_status(
                    "error",
                    f"rtl_fm exceeded max_restarts ({self._max_restarts})",
                )
                self.log.error(
                    "rtl_fm exceeded max_restarts (%d); giving up",
                    self._max_restarts,
                )
                self._playing = False
                break

            backoff = min(60.0, 2.0 ** self._restart_count)
            self._set_status("restarting", f"backoff {backoff:.0f}s")
            self.log.info(
                "Restarting rtl_fm in %.0fs (attempt %d/%d)",
                backoff, self._restart_count, self._max_restarts,
            )
            self._sleep_while_active(backoff)

    def _launch_rtl_fm(self) -> None:
        cmd = self._build_cmd()
        self.log.debug("Launching: %s", " ".join(cmd))
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._pid = self._process.pid
        self._signal_rms = 0.0
        self._signal_db = -90.0
        self._set_status("playing")
        self._start_log_reader(self._process_stderr_reader(), prefix="rtl_fm")
        self.log.info(
            "Started rtl_fm at %.3f MHz %s (PID %d)",
            self._frequency_hz / 1_000_000, self._mode.upper(), self._pid,
        )

    def _process_stderr_reader(self) -> Any:
        class _FakeProc:
            pass
        fake = _FakeProc()
        proc = self._process
        fake.stdout = proc.stderr if proc else None
        return fake

    def _build_cmd(self) -> list[str]:
        assert self._rtl_fm_path is not None
        device_idx = (
            self._resolved_index
            if self._resolved_index is not None
            else self._device_id
        )
        cmd = [
            self._rtl_fm_path,
            "-f", str(self._frequency_hz),
            "-M", self._mode,
            "-s", str(self._sample_rate_hz),
            "-d", str(device_idx),
            "-p", str(self._ppm),
            "-l", str(self._squelch_level),
        ]
        if self._output_rate_hz != self._sample_rate_hz:
            cmd += ["-r", str(self._output_rate_hz)]
        if self._gain_db is not None:
            cmd += ["-g", f"{self._gain_db:.1f}"]
        if self._mode == "wbfm":
            cmd += ["-E", "deemp"]
        if self._enable_bias_tee:
            cmd += ["-T"]
        cmd.append("-")
        return cmd

    # ── audio reader ─────────────────────────────────────────────────

    def _audio_reader_loop(self) -> None:
        proc = self._process
        if proc is None or proc.stdout is None:
            return
        stream = proc.stdout
        try:
            while self._active and self._playing:
                chunk = stream.read(_CHUNK_BYTES)
                if not chunk:
                    break

                if self._volume < 1.0:
                    chunk = self._apply_volume(chunk, self._volume)

                self._update_signal_level(chunk)
                self._push_audio_chunk(chunk)
        except (ValueError, OSError):
            pass
        except Exception:
            self.log.exception("Audio reader loop crashed")

    @staticmethod
    def _apply_volume(chunk: bytes, volume: float) -> bytes:
        n_samples = len(chunk) // 2
        fmt = f"<{n_samples}h"
        samples = struct.unpack(fmt, chunk[:n_samples * 2])
        scaled = [max(-32768, min(32767, int(s * volume))) for s in samples]
        return struct.pack(fmt, *scaled)

    def _update_signal_level(self, chunk: bytes) -> None:
        n_samples = len(chunk) // 2
        if n_samples == 0:
            return
        fmt = f"<{n_samples}h"
        try:
            samples = struct.unpack(fmt, chunk[:n_samples * 2])
        except struct.error:
            return
        sum_sq = sum(s * s for s in samples)
        rms = math.sqrt(sum_sq / n_samples)
        self._signal_rms = rms
        self._signal_db = 20.0 * math.log10(max(rms, 1.0)) - 90.0

    # ── process management ───────────────────────────────────────────

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
                    self.log.warning("rtl_fm did not stop; sending SIGKILL")
                    proc.kill()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.log.warning("rtl_fm did not exit after SIGKILL")
        except Exception:
            self.log.exception("Error stopping rtl_fm")
        finally:
            for f in (proc.stdout, proc.stderr):
                if f:
                    try:
                        f.close()
                    except Exception:
                        pass

    def _set_status(self, status: str, error: str | None = None) -> None:
        with self._state_lock:
            prev = self._status
            self._status = status
            self._last_error = error
        if status != prev:
            try:
                self.event_bus.publish(
                    events.FM_RECEIVER_STATUS,
                    {"status": status, "error": error, "timestamp": time.time()},
                )
            except Exception:
                self.log.debug("event_bus publish failed", exc_info=True)
