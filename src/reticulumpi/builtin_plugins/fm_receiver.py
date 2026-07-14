"""FM/AM radio receiver plugin using rtl_fm.

Manages an rtl_fm subprocess to demodulate FM/AM/SSB signals and stream
live signed-16-bit PCM audio to the web dashboard via chunked HTTP.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import struct
import subprocess
import threading
import time
import uuid
from collections import deque
from typing import Any

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase
from reticulumpi.process_supervisor import (
    ManagedProcessGroup,
    ProcessFailure,
    ProcessSpec,
    RestartPolicy,
)
from reticulumpi.sdr_scheduler import PRIORITY_BACKGROUND

_VALID_MODES = ("wbfm", "fm", "am", "usb", "lsb")
_SCHEDULER_RESTART_DELAYS = (1.0, 2.0, 4.0, 8.0, 30.0)
_SCHEDULER_RESTART_WINDOW_SECONDS = 600.0

_MODE_DEFAULTS: dict[str, dict[str, int]] = {
    "wbfm": {"sample_rate_hz": 170_000, "output_rate_hz": 32_000},
    "fm": {"sample_rate_hz": 12_000, "output_rate_hz": 12_000},
    "am": {"sample_rate_hz": 12_000, "output_rate_hz": 12_000},
    "usb": {"sample_rate_hz": 12_000, "output_rate_hz": 12_000},
    "lsb": {"sample_rate_hz": 12_000, "output_rate_hz": 12_000},
}

_E4000_LO_GAP_MHZ = (1101.0, 1234.0)

_COMMON_GAIN_STEPS_DB = (
    -1.0,
    1.5,
    4.0,
    6.5,
    9.0,
    11.5,
    14.0,
    16.5,
    19.0,
    21.5,
    24.0,
    29.0,
    34.0,
    42.0,
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
            {"freq_mhz": 462.5625, "label": "Ch 1"},
            {"freq_mhz": 462.5875, "label": "Ch 2"},
            {"freq_mhz": 462.6125, "label": "Ch 3"},
            {"freq_mhz": 462.6375, "label": "Ch 4"},
            {"freq_mhz": 462.6625, "label": "Ch 5"},
            {"freq_mhz": 462.6875, "label": "Ch 6"},
            {"freq_mhz": 462.7125, "label": "Ch 7"},
            {"freq_mhz": 467.5625, "label": "Ch 8"},
            {"freq_mhz": 467.5875, "label": "Ch 9"},
            {"freq_mhz": 467.6125, "label": "Ch 10"},
            {"freq_mhz": 467.6375, "label": "Ch 11"},
            {"freq_mhz": 467.6625, "label": "Ch 12"},
            {"freq_mhz": 467.6875, "label": "Ch 13"},
            {"freq_mhz": 467.7125, "label": "Ch 14"},
            {"freq_mhz": 462.5500, "label": "Ch 15"},
            {"freq_mhz": 462.5750, "label": "Ch 16"},
            {"freq_mhz": 462.6000, "label": "Ch 17"},
            {"freq_mhz": 462.6250, "label": "Ch 18"},
            {"freq_mhz": 462.6500, "label": "Ch 19"},
            {"freq_mhz": 462.6750, "label": "Ch 20"},
            {"freq_mhz": 462.7000, "label": "Ch 21"},
            {"freq_mhz": 462.7250, "label": "Ch 22"},
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

_STATE_FILENAME = "last_state.json"
_FAVORITES_FILENAME = "favorites.json"
_MAX_FAVORITES_DEFAULT = 100

_RECORDINGS_DIR = "recordings"
_MAX_RECORDING_SECONDS_DEFAULT = 3600
_MAX_RECORDING_SIZE_MB_DEFAULT = 500
_MAX_RECORDINGS_DEFAULT = 50


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
        self._restart_limit = min(5, max(0, self._max_restarts))
        self._scheduler_retry_lock = threading.Lock()
        self._scheduler_retry_times: deque[float] = deque()
        self._scheduler_retry_epoch = 0
        self._scheduler_retry_pending = False
        self._scheduler_retry_registration_id: int | None = None
        self._preserve_restart_count_on_play = False
        self._auto_play = bool(cfg.get("auto_play", False))
        self._audio_buffer_seconds = max(1, int(cfg.get("audio_buffer_seconds", 4)))

        from reticulumpi.rtlsdr import configured_device

        self._device_id, self._device_selector = configured_device(cfg)

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

        self._dongle_active = False
        self._preempted_by = ""
        self._preempted_by_label = ""
        self._preempted_until_ts: float | None = None
        self._locked = False
        self._was_playing_before_yield = False

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

    # ── state persistence ──────────────────────────────────────────

    def _resolve_state_dir(self) -> str:
        base = os.environ.get(
            "XDG_DATA_HOME",
            os.path.expanduser("~/.local/share"),
        )
        return os.path.join(base, "reticulumpi", "fm_receiver")

    def _resolve_state_path(self) -> str:
        return os.path.join(self._resolve_state_dir(), _STATE_FILENAME)

    def _load_state(self) -> None:
        try:
            with open(self._resolve_state_path(), encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError, json.JSONDecodeError):
            return

        freq_mhz = state.get("frequency_mhz")
        if isinstance(freq_mhz, (int, float)):
            try:
                self._validate_frequency(float(freq_mhz))
                self._frequency_hz = int(freq_mhz * 1_000_000)
            except ValueError:
                pass

        mode = state.get("mode")
        if isinstance(mode, str) and mode.lower() in _VALID_MODES:
            self._mode = mode.lower()
            defaults = _MODE_DEFAULTS[self._mode]
            self._sample_rate_hz = defaults["sample_rate_hz"]
            self._output_rate_hz = defaults["output_rate_hz"]

        gain_db = state.get("gain_db")
        if gain_db is None:
            self._gain_db = None
        elif isinstance(gain_db, (int, float)) and -10.0 <= gain_db <= 60.0:
            self._gain_db = float(gain_db)

        squelch = state.get("squelch_level")
        if isinstance(squelch, (int, float)) and squelch >= 0:
            self._squelch_level = int(squelch)

        volume = state.get("volume")
        if isinstance(volume, (int, float)) and 0.0 <= volume <= 1.0:
            self._volume = float(volume)

        self.log.info(
            "Restored state: %.3f MHz %s",
            self._frequency_hz / 1_000_000,
            self._mode.upper(),
        )

    def _persist_state(self) -> None:
        state = {
            "frequency_mhz": round(self._frequency_hz / 1_000_000, 6),
            "mode": self._mode,
            "gain_db": self._gain_db,
            "squelch_level": self._squelch_level,
            "volume": round(self._volume, 2),
            "timestamp": time.time(),
        }
        path = self._resolve_state_path()
        tmp = path + ".tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f)
            os.replace(tmp, path)
        except Exception:
            self.log.debug("Failed to persist state", exc_info=True)
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        self._state_lock = threading.Lock()
        self._process_lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._process_group: ManagedProcessGroup | None = None
        self._pid: int | None = None
        self._restart_count = 0
        self._rtl_fm_path: str | None = None
        self._last_error: str | None = None
        self._status = "stopped"
        self._playing = False
        self._resolved_index: int | None = None
        self._device_lease = None
        self._dongle_generation: int | None = None
        self._supervisor_alive = False
        self._supervisor_generation = 0
        with self._scheduler_retry_lock:
            self._scheduler_retry_epoch += 1
            self._scheduler_retry_times.clear()
            self._scheduler_retry_pending = False
            self._scheduler_retry_registration_id = None

        self._signal_rms: float = 0.0
        self._signal_db: float = -90.0
        self._dead_zone_warning: str | None = None

        self._signal_history: deque[float] = deque(maxlen=300)
        self._squelch_break_count = 0
        self._squelch_was_open = False
        self._last_signal_history_ts = 0.0

        self._stream_queues: list[asyncio.Queue] = []
        self._stream_lock = threading.Lock()
        self._event_loop: asyncio.AbstractEventLoop | None = None

        self._favorites: list[dict[str, Any]] = []
        self._load_favorites()

        self._recording = False
        self._recording_file = None
        self._recording_path: str | None = None
        self._recording_start_ts: float | None = None
        self._recording_start_monotonic: float | None = None
        self._recording_bytes = 0
        self._recording_label: str | None = None
        self._rec_lock = threading.Lock()
        self._max_recording_seconds = int(
            self.config.get("max_recording_seconds", _MAX_RECORDING_SECONDS_DEFAULT)
        )
        self._max_recording_size_bytes = (
            int(self.config.get("max_recording_size_mb", _MAX_RECORDING_SIZE_MB_DEFAULT))
            * 1024
            * 1024
        )
        self._max_recordings = int(self.config.get("max_recordings", _MAX_RECORDINGS_DEFAULT))

        self._active = True

        sched = getattr(self.app, "sdr_scheduler", None)
        if sched is not None and self._device_id:
            sched.register(
                serial=self._device_id,
                caller=self.plugin_name,
                priority=PRIORITY_BACKGROUND,
                acquire_cb=self._on_scheduler_acquire,
                yield_cb=self._on_scheduler_yield,
                label="FM/AM Radio",
                continuous=True,
                device_selector=self._device_selector,
            )
        else:
            try:
                from reticulumpi.rtlsdr import refresh_device_lease

                self._device_lease = refresh_device_lease(
                    self._device_lease,
                    self._device_id,
                    self.plugin_name,
                    selector=self._device_selector,
                )
                self._resolved_index = self._device_lease.index
                self._dongle_active = True
            except (RuntimeError, ValueError) as exc:
                self.log.error("RTL-SDR device resolution failed: %s", exc)
                self._set_status("error", str(exc))

        self._load_state()

        freq_mhz = self._frequency_hz / 1_000_000
        warning = self._check_dead_zone(freq_mhz)
        if warning:
            self._dead_zone_warning = warning
            self.log.warning(warning)

        if self._auto_play and self._dongle_active:
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
        with self._scheduler_retry_lock:
            self._scheduler_retry_epoch += 1
            self._scheduler_retry_pending = False
            self._scheduler_retry_registration_id = None
        if self._recording:
            self._stop_recording_internal("shutdown")
        self._playing = False
        self._invalidate_supervisor()
        self._terminate_process()
        self._notify_clients_stopped()
        sched = getattr(self.app, "sdr_scheduler", None)
        if sched is not None and self._device_id:
            sched.unregister(self._device_id, self.plugin_name)
        else:
            self._release_standalone_lease()
        self._join_threads(timeout=5.0)
        self._set_status("stopped")

    # ── scheduler callbacks ────────────────────────────────────────────

    def _on_scheduler_acquire(self, serial: str, device_index: int) -> None:
        self._resolved_index = device_index
        scheduler = getattr(self.app, "sdr_scheduler", None)
        allocation_getter = getattr(type(scheduler), "get_allocation_generation", None)
        if callable(allocation_getter):
            self._dongle_generation = allocation_getter(
                scheduler,
                serial,
                self.plugin_name,
            )
        else:
            generation_getter = getattr(type(scheduler), "get_generation", None)
            self._dongle_generation = (
                generation_getter(scheduler, serial) if callable(generation_getter) else None
            )
        self._dongle_active = True
        self._preempted_by = ""
        self._preempted_by_label = ""
        self._preempted_until_ts = None
        self.log.info("Acquired dongle %s (index %d)", serial, device_index)
        with self._scheduler_retry_lock:
            retrying = self._scheduler_retry_pending
            if retrying:
                self._scheduler_retry_pending = False
                self._scheduler_retry_registration_id = None
        if self._was_playing_before_yield or retrying:
            self._was_playing_before_yield = False
            self._preserve_restart_count_on_play = retrying
            self.play()
        elif self._auto_play:
            self.play()

    def _on_scheduler_yield(
        self,
        preempted_by: str,
        preempted_by_label: str,
        preempted_until_ts: float | None,
    ) -> bool:
        self._was_playing_before_yield = self._playing
        if self._recording:
            self._stop_recording_internal("preempted")
        self._dongle_active = False
        self._dongle_generation = None
        self._preempted_by = preempted_by
        self._preempted_by_label = preempted_by_label
        self._preempted_until_ts = preempted_until_ts
        if self._playing:
            self._playing = False
            self._invalidate_supervisor()
            self._terminate_process()
            self._notify_clients_stopped()
            self._set_status("paused")
        self.log.info("Yielded dongle to %s", preempted_by)
        return True

    # ── lock / unlock ───────────────────────────────────────────────

    def lock_dongle(self) -> dict[str, Any]:
        if not self._dongle_active:
            return {"locked": False, "error": "dongle not active"}
        sched = getattr(self.app, "sdr_scheduler", None)
        if sched is None:
            return {"locked": False, "error": "scheduler not available"}
        if sched.lock(self._device_id, self.plugin_name):
            self._locked = True
            return {"locked": True}
        return {"locked": False, "error": "lock rejected"}

    def unlock_dongle(self) -> dict[str, Any]:
        sched = getattr(self.app, "sdr_scheduler", None)
        if sched is None:
            return {"locked": False, "error": "scheduler not available"}
        sched.unlock(self._device_id, self.plugin_name)
        self._locked = False
        return {"locked": False}

    # ── public properties ────────────────────────────────────────────

    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def output_rate_hz(self) -> int:
        return self._output_rate_hz

    # ── public API ───────────────────────────────────────────────────

    def play(self) -> dict[str, Any]:
        if self._playing:
            return {"status": "already_playing"}
        if self._supervisor_alive:
            return {"status": "already_playing"}
        if not self._dongle_active:
            return {"status": "error", "error": "Dongle in use by another signal"}
        if self._resolved_index is None:
            return {"status": "error", "error": "No RTL-SDR device resolved"}
        self._playing = True
        if not self._preserve_restart_count_on_play:
            self._restart_count = 0
        try:
            self._start_supervisor()
        except BaseException:
            self._preserve_restart_count_on_play = False
            raise
        return {"status": "starting", "frequency_mhz": self._frequency_hz / 1_000_000}

    def stop_playback(self) -> dict[str, Any]:
        with self._scheduler_retry_lock:
            retry_pending = self._scheduler_retry_pending
        if not self._playing and not retry_pending:
            return {"status": "already_stopped"}
        self._cancel_scheduler_retry(restore_eligibility=True)
        if self._recording:
            self._stop_recording_internal("stop")
        self._playing = False
        self._invalidate_supervisor()
        self._terminate_process()
        self._notify_clients_stopped()
        self._set_status("stopped")
        return {"status": "stopped"}

    def tune(self, frequency_hz: int, mode: str | None = None) -> dict[str, Any]:
        freq_mhz = frequency_hz / 1_000_000
        self._validate_frequency(freq_mhz)
        if self._recording:
            self._stop_recording_internal("tune")

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
            self._restart_playback()

        self._persist_state()

        try:
            self.event_bus.publish(
                events.FM_RECEIVER_TUNED,
                {
                    "frequency_hz": frequency_hz,
                    "frequency_mhz": freq_mhz,
                    "mode": self._mode,
                },
            )
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
        self._persist_state()
        if self._playing:
            self._restart_playback()
        return {"gain_db": self._gain_db}

    def set_squelch(self, level: int) -> dict[str, Any]:
        self._squelch_level = max(0, int(level))
        self._persist_state()
        if self._playing:
            self._restart_playback()
        return {"squelch_level": self._squelch_level}

    def set_volume(self, volume: float) -> dict[str, Any]:
        self._volume = max(0.0, min(1.0, float(volume)))
        self._persist_state()
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

    # ── favorites ────────────────────────────────────────────────────

    def _favorites_path(self) -> str:
        return os.path.join(self._resolve_state_dir(), _FAVORITES_FILENAME)

    def _load_favorites(self) -> None:
        try:
            with open(self._favorites_path(), encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self._favorites = data
            else:
                self._favorites = []
        except (OSError, ValueError, json.JSONDecodeError):
            self._favorites = []

    def _save_favorites(self) -> None:
        path = self._favorites_path()
        tmp = path + ".tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with self._state_lock:
                data = list(self._favorites)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, path)
        except Exception:
            self.log.debug("Failed to save favorites", exc_info=True)
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def get_favorites(self) -> list[dict[str, Any]]:
        with self._state_lock:
            return list(self._favorites)

    def add_favorite(
        self,
        label: str,
        frequency_mhz: float,
        mode: str,
        gain_db: float | None = None,
    ) -> dict[str, Any]:
        mode = mode.lower()
        if mode not in _VALID_MODES:
            raise ValueError(f"Invalid mode '{mode}'")
        self._validate_frequency(frequency_mhz)
        fav = {
            "id": str(uuid.uuid4()),
            "label": label or f"{frequency_mhz:.3f} MHz",
            "frequency_mhz": round(frequency_mhz, 6),
            "mode": mode,
            "gain_db": gain_db,
            "created_at": time.time(),
            "last_used_at": None,
        }
        with self._state_lock:
            max_fav = int(self.config.get("max_favorites", _MAX_FAVORITES_DEFAULT))
            if len(self._favorites) >= max_fav:
                raise ValueError(f"Maximum favorites ({max_fav}) reached")
            self._favorites.append(fav)
        self._save_favorites()
        return fav

    def remove_favorite(self, favorite_id: str) -> bool:
        with self._state_lock:
            for i, fav in enumerate(self._favorites):
                if fav.get("id") == favorite_id:
                    self._favorites.pop(i)
                    break
            else:
                return False
        self._save_favorites()
        return True

    def update_favorite(self, favorite_id: str, **kwargs: Any) -> dict[str, Any] | None:
        with self._state_lock:
            for fav in self._favorites:
                if fav.get("id") != favorite_id:
                    continue
                if "label" in kwargs:
                    fav["label"] = str(kwargs["label"])
                if "frequency_mhz" in kwargs:
                    freq = float(kwargs["frequency_mhz"])
                    self._validate_frequency(freq)
                    fav["frequency_mhz"] = round(freq, 6)
                if "mode" in kwargs:
                    mode = str(kwargs["mode"]).lower()
                    if mode not in _VALID_MODES:
                        raise ValueError(f"Invalid mode '{mode}'")
                    fav["mode"] = mode
                if "gain_db" in kwargs:
                    fav["gain_db"] = kwargs["gain_db"]
                result = fav
                break
            else:
                return None
        self._save_favorites()
        return result

    def tune_favorite(self, favorite_id: str) -> dict[str, Any]:
        with self._state_lock:
            for fav in self._favorites:
                if fav.get("id") == favorite_id:
                    fav["last_used_at"] = time.time()
                    freq_hz = int(fav["frequency_mhz"] * 1_000_000)
                    matched_fav = fav
                    break
            else:
                raise ValueError(f"Favorite '{favorite_id}' not found")
        self._save_favorites()
        return self.tune(freq_hz, mode=matched_fav.get("mode"))

    # ── recording ────────────────────────────────────────────────────

    def _recordings_dir(self) -> str:
        return os.path.join(self._resolve_state_dir(), _RECORDINGS_DIR)

    def start_recording(self, label: str | None = None) -> dict[str, Any]:
        if self._recording:
            return {"recording": True, "error": "Already recording"}
        if not self._playing:
            return {"recording": False, "error": "Not playing"}

        rec_dir = self._recordings_dir()
        os.makedirs(rec_dir, exist_ok=True)

        existing = self._list_recording_files()
        if len(existing) >= self._max_recordings:
            return {
                "recording": False,
                "error": f"Maximum recordings ({self._max_recordings}) reached",
            }

        freq_mhz = self._frequency_hz / 1_000_000
        ts = time.strftime("%Y%m%d_%H%M%S")
        base = f"{ts}_{freq_mhz:.3f}MHz_{self._mode}"
        filename = f"{base}.wav"
        path = os.path.join(rec_dir, filename)
        n = 1
        while os.path.exists(path):
            filename = f"{base}_{n}.wav"
            path = os.path.join(rec_dir, filename)
            n += 1

        header = self._build_recording_wav_header()
        try:
            f = open(path, "wb")
            f.write(header)
            f.flush()
        except OSError as exc:
            try:
                f.close()
            except OSError:
                pass
            return {"recording": False, "error": str(exc)}

        with self._rec_lock:
            self._recording_file = f
            self._recording_path = path
            self._recording_start_ts = time.time()
            self._recording_start_monotonic = time.monotonic()
            self._recording_bytes = 0
            self._recording_label = label
            self._recording = True

        self.log.info("Recording started: %s", filename)
        try:
            self.event_bus.publish(
                events.FM_RECEIVER_RECORDING_STARTED,
                {
                    "filename": filename,
                    "frequency_mhz": freq_mhz,
                    "mode": self._mode,
                },
            )
        except Exception:
            self.log.debug("event_bus publish failed", exc_info=True)

        return {
            "recording": True,
            "filename": filename,
            "frequency_mhz": freq_mhz,
            "mode": self._mode,
        }

    def stop_recording(self) -> dict[str, Any]:
        if not self._recording:
            return {"recording": False}
        return self._stop_recording_internal("user")

    def _stop_recording_internal(self, reason: str = "unknown") -> dict[str, Any]:
        with self._rec_lock:
            if not self._recording:
                return {"recording": False}
            f, path, data_bytes, start_monotonic = self._stop_recording_locked()

        if f is not None:
            self._finalize_recording_file(f, data_bytes)

        duration = self._recording_elapsed(start_monotonic)
        filename = os.path.basename(path) if path else ""
        self.log.info(
            "Recording stopped (%s): %s (%.1fs)",
            reason,
            filename,
            duration,
        )
        try:
            self.event_bus.publish(
                events.FM_RECEIVER_RECORDING_STOPPED,
                {
                    "filename": filename,
                    "duration_seconds": round(duration, 1),
                    "size_bytes": data_bytes,
                    "reason": reason,
                },
            )
        except Exception:
            self.log.debug("event_bus publish failed", exc_info=True)

        return {
            "recording": False,
            "filename": filename,
            "duration_seconds": round(duration, 1),
            "size_bytes": data_bytes,
        }

    def _stop_recording_locked(self):
        """Extract recording state and clear flags. Caller must hold _rec_lock."""
        f = self._recording_file
        path = self._recording_path or ""
        data_bytes = self._recording_bytes
        start_monotonic = self._recording_start_monotonic
        self._recording = False
        self._recording_file = None
        self._recording_path = None
        self._recording_start_ts = None
        self._recording_start_monotonic = None
        self._recording_bytes = 0
        self._recording_label = None
        return f, path, data_bytes, start_monotonic

    @staticmethod
    def _recording_elapsed(start_monotonic: float | None) -> float:
        if start_monotonic is None:
            return 0.0
        return max(0.0, time.monotonic() - start_monotonic)

    def _finalize_recording_file(self, f, data_bytes: int) -> None:
        try:
            f.seek(4)
            f.write(struct.pack("<I", 36 + data_bytes))
            f.seek(40)
            f.write(struct.pack("<I", data_bytes))
            f.close()
        except OSError:
            self.log.debug("Failed to finalize recording", exc_info=True)
            try:
                f.close()
            except OSError:
                pass

    def _build_recording_wav_header(self) -> bytes:
        sample_rate = self._output_rate_hz
        channels = 1
        bits = 16
        byte_rate = sample_rate * channels * (bits // 8)
        block_align = channels * (bits // 8)
        return struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            36,
            b"WAVE",
            b"fmt ",
            16,
            1,
            channels,
            sample_rate,
            byte_rate,
            block_align,
            bits,
            b"data",
            0,
        )

    def _write_recording_chunk(self, chunk: bytes) -> None:
        with self._rec_lock:
            if not self._recording or self._recording_file is None:
                return
            over_size = self._recording_bytes + len(chunk) > self._max_recording_size_bytes
            over_time = (
                self._recording_start_monotonic is not None
                and self._recording_elapsed(self._recording_start_monotonic)
                > self._max_recording_seconds
            )
            if over_size or over_time:
                f, path, data_bytes, start_monotonic = self._stop_recording_locked()
            else:
                self._recording_file.write(chunk)
                self._recording_bytes += len(chunk)
                return

        self._finalize_recording_file(f, data_bytes)
        duration = self._recording_elapsed(start_monotonic)
        filename = os.path.basename(path) if path else ""
        self.log.info("Recording auto-stopped (limit): %s (%.1fs)", filename, duration)
        try:
            self.event_bus.publish(
                events.FM_RECEIVER_RECORDING_STOPPED,
                {
                    "filename": filename,
                    "duration_seconds": round(duration, 1),
                    "size_bytes": data_bytes,
                    "reason": "limit",
                },
            )
        except Exception:
            self.log.debug("event_bus publish failed", exc_info=True)

    def _list_recording_files(self) -> list[str]:
        rec_dir = self._recordings_dir()
        if not os.path.isdir(rec_dir):
            return []
        try:
            return sorted(
                f for f in os.listdir(rec_dir) if f.endswith(".wav") and not f.startswith(".")
            )
        except OSError:
            return []

    def get_recordings(self) -> list[dict[str, Any]]:
        rec_dir = self._recordings_dir()
        result = []
        for filename in self._list_recording_files():
            path = os.path.join(rec_dir, filename)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            duration = 0.0
            try:
                with open(path, "rb") as rf:
                    rf.seek(28)
                    byte_rate = struct.unpack("<I", rf.read(4))[0]
                data_bytes = max(0, stat.st_size - 44)
                if byte_rate > 0:
                    duration = data_bytes / byte_rate
            except (OSError, struct.error):
                pass
            parts = filename.rsplit(".", 1)[0].split("_")
            freq_mhz = 0.0
            mode = ""
            for part in parts:
                if part.endswith("MHz"):
                    try:
                        freq_mhz = float(part[:-3])
                    except ValueError:
                        pass
                elif part in _VALID_MODES:
                    mode = part
            result.append(
                {
                    "filename": filename,
                    "size_bytes": stat.st_size,
                    "created_at": stat.st_mtime,
                    "frequency_mhz": freq_mhz,
                    "mode": mode,
                    "duration_seconds": round(duration, 1),
                }
            )
        return result

    def delete_recording(self, filename: str) -> bool:
        if "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError("Invalid filename")
        if not filename.endswith(".wav"):
            raise ValueError("Invalid filename")
        rec_dir = os.path.realpath(self._recordings_dir())
        path = os.path.realpath(os.path.join(rec_dir, filename))
        if not path.startswith(rec_dir + os.sep) and path != rec_dir:
            raise ValueError("Invalid filename")
        try:
            os.unlink(path)
            return True
        except FileNotFoundError:
            return False

    def get_recording_path(self, filename: str) -> str | None:
        if "/" in filename or "\\" in filename or ".." in filename:
            return None
        if not filename.endswith(".wav"):
            return None
        rec_dir = os.path.realpath(self._recordings_dir())
        path = os.path.realpath(os.path.join(rec_dir, filename))
        if not path.startswith(rec_dir + os.sep) and path != rec_dir:
            return None
        if os.path.isfile(path):
            return path
        return None

    # ── audio client management ──────────────────────────────────────

    _MAX_AUDIO_CLIENTS = 8

    def register_audio_client(self, queue: asyncio.Queue) -> bool:
        with self._stream_lock:
            if len(self._stream_queues) >= self._MAX_AUDIO_CLIENTS:
                return False
            self._stream_queues.append(queue)
            return True

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
        dead: list[asyncio.Queue] = []
        for q in queues:
            try:
                loop.call_soon_threadsafe(q.put_nowait, chunk)
            except asyncio.QueueFull:
                pass
            except RuntimeError:
                dead.append(q)
        if dead:
            with self._stream_lock:
                for q in dead:
                    try:
                        self._stream_queues.remove(q)
                    except ValueError:
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
        with self._state_lock:
            signal_rms = self._signal_rms
            signal_db = self._signal_db
            squelch_break_count = self._squelch_break_count
            signal_history = list(self._signal_history)[-30:]
        snap: dict[str, Any] = {
            "status": self._status,
            "playing": self._playing,
            "frequency_hz": self._frequency_hz,
            "frequency_mhz": round(freq_mhz, 4),
            "mode": self._mode,
            "gain_db": self._gain_db,
            "squelch_level": self._squelch_level,
            "volume": round(self._volume, 2),
            "signal_rms": round(signal_rms, 1),
            "signal_db": round(signal_db, 1),
            "output_rate_hz": self._output_rate_hz,
            "freq_min_mhz": self._freq_min_mhz,
            "freq_max_mhz": self._freq_max_mhz,
            "restart_count": self._restart_count,
            "error": self._last_error,
            "dead_zone_warning": self._dead_zone_warning,
            "audio_clients": client_count,
            "dongle_active": self._dongle_active,
            "locked": self._locked,
        }
        if not self._dongle_active and self._preempted_by:
            snap["preempted_by"] = self._preempted_by
            snap["preempted_by_label"] = self._preempted_by_label
            snap["preempted_until_ts"] = self._preempted_until_ts
        snap["squelch_break_count"] = squelch_break_count
        snap["signal_history"] = signal_history
        if self._recording:
            snap["recording"] = {
                "active": True,
                "duration_seconds": round(
                    self._recording_elapsed(self._recording_start_monotonic), 1
                ),
                "filename": os.path.basename(self._recording_path or ""),
            }
        else:
            snap["recording"] = None
        return snap

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

    def _start_supervisor(self) -> None:
        lock = getattr(self, "_process_lock", None)
        if lock is None:
            lock = self._process_lock = threading.RLock()
        with lock:
            self._supervisor_generation = getattr(self, "_supervisor_generation", 0) + 1
            generation = self._supervisor_generation
            self._supervisor_alive = True
        self._start_thread(
            lambda: self._supervisor_loop(generation),
            name="fm-supervisor",
        )

    def _invalidate_supervisor(self) -> None:
        lock = getattr(self, "_process_lock", None)
        if lock is None:
            self._supervisor_generation = getattr(self, "_supervisor_generation", 0) + 1
            self._supervisor_alive = False
            return
        with lock:
            self._supervisor_generation = getattr(self, "_supervisor_generation", 0) + 1
            self._supervisor_alive = False

    def _restart_playback(self) -> None:
        """Apply a live tuning change through a fresh supervised launch."""

        self._invalidate_supervisor()
        self._terminate_process()
        self._notify_clients_stopped()
        self._restart_count = 0
        if self._active and self._playing and self._dongle_active:
            self._start_supervisor()

    def _supervisor_loop(self, generation: int | None = None) -> None:
        if generation is None:
            generation = getattr(self, "_supervisor_generation", 0)
        self._supervisor_alive = True
        try:
            self._supervisor_loop_inner(generation)
        finally:
            if generation == getattr(self, "_supervisor_generation", generation):
                self._supervisor_alive = False

    def _supervisor_loop_inner(self, generation: int | None = None) -> None:
        if generation is None:
            generation = getattr(self, "_supervisor_generation", 0)
        self._rtl_fm_path = shutil.which("rtl_fm")
        if not self._rtl_fm_path:
            self._set_status("unavailable", "rtl_fm not found on PATH")
            self.mark_degraded("rtl_fm not found on PATH")
            self.log.warning("rtl_fm binary not found; %s will stay idle.", self.plugin_name)
            self._playing = False
            self._release_sdr_after_failure()
            return
        if (
            generation != getattr(self, "_supervisor_generation", generation)
            or not self._active
            or not self._playing
            or not self._dongle_active
        ):
            return
        try:
            self._launch_rtl_fm(generation)
        except Exception as exc:
            if generation != getattr(self, "_supervisor_generation", generation):
                return
            self._terminate_process()
            self._set_status("error", f"launch failed: {exc}")
            self.mark_degraded(str(exc))
            if getattr(self.app, "sdr_scheduler", None) is not None:
                if not self._schedule_scheduler_retry():
                    self._playing = False
            else:
                self._playing = False
                self._release_sdr_after_failure()
            self.log.exception("Failed to launch rtl_fm")

    def _launch_rtl_fm(self, generation: int | None = None) -> None:
        cmd = self._build_cmd()
        self.log.debug("Launching: %s", " ".join(cmd))
        group: ManagedProcessGroup
        group = ManagedProcessGroup(
            [
                ProcessSpec(
                    tuple(cmd),
                    name="rtl_fm",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            ],
            restart_policy=RestartPolicy(
                enabled=getattr(self.app, "sdr_scheduler", None) is None,
                max_restarts=self._restart_limit,
            ),
            on_started=lambda processes, restarted: self._on_process_started(
                group,
                processes,
                restarted,
            ),
            on_unexpected_exit=lambda failure: self._on_process_failure(group, failure),
            on_restart=lambda attempt, delay: self._on_process_restart(
                group,
                attempt,
                delay,
            ),
            on_restart_failed=lambda error, attempt: self._on_process_restart_failed(
                group,
                error,
                attempt,
            ),
            on_exhausted=lambda failure: self._on_process_exhausted(group, failure),
        )
        lock = getattr(self, "_process_lock", None)
        if lock is None:
            lock = self._process_lock = threading.RLock()
        with lock:
            current_generation = getattr(self, "_supervisor_generation", 0)
            if generation is not None and generation != current_generation:
                return
            if not self._active or not self._playing or not self._dongle_active:
                return
            self._process_group = group
        try:
            group.start()
        except Exception:
            with lock:
                if self._process_group is group:
                    self._process_group = None
            raise

    def _on_process_started(
        self,
        group: ManagedProcessGroup,
        processes: tuple[subprocess.Popen[Any], ...],
        restarted: bool,
    ) -> None:
        process = processes[0]
        if (
            self._process_group is not group
            or not self._active
            or not self._playing
            or not self._dongle_active
        ):
            raise RuntimeError("stale rtl_fm launch completed after playback stopped")
        self._process = process
        self._pid = process.pid
        scheduler_relaunch = self._preserve_restart_count_on_play
        if restarted:
            self._restart_count = group.restart_count
        elif not scheduler_relaunch:
            self._restart_count = 0
        self._preserve_restart_count_on_play = False
        with self._state_lock:
            self._signal_rms = 0.0
            self._signal_db = -90.0
        self._set_status("playing")
        self._start_stderr_reader(process, prefix="rtl_fm")
        self._start_thread(
            lambda: self._audio_reader_loop(process),
            name="fm-audio-reader",
        )
        self.log.info(
            "Started rtl_fm at %.3f MHz %s (PID %d)",
            self._frequency_hz / 1_000_000,
            self._mode.upper(),
            self._pid,
        )

    def _on_process_failure(
        self,
        group: ManagedProcessGroup,
        failure: ProcessFailure,
    ) -> None:
        if self._process_group is not group:
            return
        self._set_status(
            "restarting",
            f"{failure.stage_name or 'rtl_fm'}: {failure.reason} (rc={failure.returncode})",
        )
        self.mark_degraded(self._last_error or failure.reason)
        if getattr(self.app, "sdr_scheduler", None) is None:
            self._release_standalone_lease()
        elif not self._schedule_scheduler_retry(group):
            self._on_process_exhausted(group, failure)

    def _on_process_restart(
        self,
        group: ManagedProcessGroup,
        attempt: int,
        delay: float,
    ) -> None:
        if (
            self._process_group is not group
            or not self._active
            or not self._playing
            or not self._dongle_active
        ):
            raise RuntimeError("rtl_fm playback was stopped during restart backoff")
        if getattr(self.app, "sdr_scheduler", None) is None:
            from reticulumpi.rtlsdr import invalidate_cache, refresh_device_lease

            invalidate_cache()
            self._device_lease = refresh_device_lease(
                self._device_lease,
                self._device_id,
                self.plugin_name,
                selector=self._device_selector,
            )
            self._resolved_index = self._device_lease.index
        group.replace_specs(
            [
                ProcessSpec(
                    tuple(self._build_cmd()),
                    name="rtl_fm",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            ]
        )
        self._restart_count = attempt
        self._set_status("restarting", f"backoff {delay:.0f}s")

    def _on_process_restart_failed(
        self,
        group: ManagedProcessGroup,
        error: BaseException,
        attempt: int,
    ) -> None:
        if self._process_group is not group:
            return
        self._restart_count = attempt
        self._set_status("restarting", f"restart {attempt} failed: {error}")
        self.mark_degraded(self._last_error or str(error))
        if getattr(self.app, "sdr_scheduler", None) is None:
            self._release_standalone_lease()

    def _on_process_exhausted(
        self,
        group: ManagedProcessGroup,
        failure: ProcessFailure,
    ) -> None:
        if self._process_group is not group:
            return
        self._process = None
        self._pid = None
        self._restart_count = max(self._restart_count, group.restart_count)
        self._playing = False
        self._set_status(
            "error",
            f"rtl_fm exceeded restart limit ({self._restart_limit}): {failure.reason}",
        )
        self.mark_degraded(self._last_error or failure.reason)
        self._notify_clients_stopped()
        self._release_sdr_after_failure()

    def _release_standalone_lease(self) -> None:
        lease = getattr(self, "_device_lease", None)
        self._device_lease = None
        if lease is not None:
            try:
                lease.release()
            except Exception:
                self.log.debug("SDR device release failed", exc_info=True)

    def _release_sdr_after_failure(self) -> int | None:
        """Release and suspend failed scheduler ownership to prevent spin."""

        scheduler = getattr(self.app, "sdr_scheduler", None)
        registration_id: int | None = None
        if scheduler is not None and self._device_id:
            suspend_method = getattr(type(scheduler), "suspend", None)
            if callable(suspend_method):
                result = suspend_method(
                    scheduler,
                    self._device_id,
                    self.plugin_name,
                    generation=getattr(self, "_dongle_generation", None),
                )
                if isinstance(result, int):
                    registration_id = result
            else:
                release_method = getattr(type(scheduler), "dongle_released", None)
                if callable(release_method):
                    release_method(
                        scheduler,
                        self._device_id,
                        self.plugin_name,
                        generation=getattr(self, "_dongle_generation", None),
                    )
                else:
                    scheduler.dongle_released(self._device_id, self.plugin_name)
            self._dongle_generation = None
        else:
            self._release_standalone_lease()
        self._dongle_active = False
        return registration_id

    def _schedule_scheduler_retry(self, group: ManagedProcessGroup | None = None) -> bool:
        """Back off with no lease, then ask the scheduler to arbitrate again."""

        scheduler = getattr(self.app, "sdr_scheduler", None)
        resume_method = getattr(type(scheduler), "resume", None)
        registration_id = self._release_sdr_after_failure()
        if registration_id is None or not callable(resume_method):
            return False

        now = time.monotonic()
        with self._scheduler_retry_lock:
            cutoff = now - _SCHEDULER_RESTART_WINDOW_SECONDS
            while self._scheduler_retry_times and self._scheduler_retry_times[0] < cutoff:
                self._scheduler_retry_times.popleft()
            if len(self._scheduler_retry_times) >= self._restart_limit:
                return False
            self._scheduler_retry_times.append(now)
            attempt = len(self._scheduler_retry_times)
            delay = _SCHEDULER_RESTART_DELAYS[min(attempt - 1, len(_SCHEDULER_RESTART_DELAYS) - 1)]
            self._scheduler_retry_epoch += 1
            retry_epoch = self._scheduler_retry_epoch
            self._scheduler_retry_pending = True
            self._scheduler_retry_registration_id = registration_id
            self._restart_count += 1

        self._process_group = None
        self._process = None
        self._pid = None
        self._playing = False
        self._was_playing_before_yield = True
        self._invalidate_supervisor()

        def _resume_after_backoff() -> None:
            if self._stop_event.wait(delay) or not self._active:
                return
            with self._scheduler_retry_lock:
                if retry_epoch != self._scheduler_retry_epoch or not self._scheduler_retry_pending:
                    return
            try:
                resumed = resume_method(
                    scheduler,
                    self._device_id,
                    self.plugin_name,
                    registration_id=registration_id,
                )
            except Exception:
                self.log.exception("FM SDR scheduler resume failed")
                resumed = False
            if not resumed:
                with self._scheduler_retry_lock:
                    if retry_epoch == self._scheduler_retry_epoch:
                        self._scheduler_retry_pending = False
                        self._scheduler_retry_registration_id = None
                self._was_playing_before_yield = False
                self._set_status("error", "SDR scheduler retry registration became stale")

        try:
            self._start_thread(_resume_after_backoff, name="fm-sdr-retry")
        except BaseException:
            if group is not None:
                self._process_group = group
            with self._scheduler_retry_lock:
                if retry_epoch == self._scheduler_retry_epoch:
                    self._scheduler_retry_pending = False
                    self._scheduler_retry_registration_id = None
            self.log.exception("Failed to start FM scheduler retry worker")
            return False
        self._set_status("restarting", f"backoff {delay:.0f}s")
        return True

    def _cancel_scheduler_retry(self, *, restore_eligibility: bool) -> None:
        """Cancel a delayed FM restart, optionally unsuspending its slot."""

        with self._scheduler_retry_lock:
            registration_id = self._scheduler_retry_registration_id
            was_pending = self._scheduler_retry_pending
            self._scheduler_retry_epoch += 1
            self._scheduler_retry_pending = False
            self._scheduler_retry_registration_id = None
        self._was_playing_before_yield = False
        self._preserve_restart_count_on_play = False
        if not restore_eligibility or not was_pending or registration_id is None:
            return
        scheduler = getattr(self.app, "sdr_scheduler", None)
        resume_method = getattr(type(scheduler), "resume", None)
        if callable(resume_method):
            resume_method(
                scheduler,
                self._device_id,
                self.plugin_name,
                registration_id=registration_id,
            )

    def _build_cmd(self) -> list[str]:
        assert self._rtl_fm_path is not None
        device_idx = self._resolved_index if self._resolved_index is not None else self._device_id
        cmd = [
            self._rtl_fm_path,
            "-f",
            str(self._frequency_hz),
            "-M",
            self._mode,
            "-s",
            str(self._sample_rate_hz),
            "-d",
            str(device_idx),
            "-p",
            str(self._ppm),
            "-l",
            str(self._squelch_level),
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

    def _audio_reader_loop(self, proc: subprocess.Popen[Any] | None = None) -> None:
        proc = proc or self._process
        if proc is None or proc.stdout is None:
            return
        stream = proc.stdout
        try:
            while self._active and self._playing and self._process is proc:
                chunk = stream.read(_CHUNK_BYTES)
                if not chunk:
                    break

                if self._volume < 1.0:
                    chunk = self._apply_volume(chunk, self._volume)

                self._update_signal_level(chunk)
                self._push_audio_chunk(chunk)

                if self._recording:
                    self._write_recording_chunk(chunk)
        except (ValueError, OSError):
            pass
        except Exception:
            self.log.exception("Audio reader loop crashed")
        finally:
            group = getattr(self, "_process_group", None)
            if (
                self._active
                and self._playing
                and self._process is proc
                and group is not None
                and group.running
            ):
                group.notify_unexpected_eof(0, "rtl_fm audio stream reached EOF")

    @staticmethod
    def _apply_volume(chunk: bytes, volume: float) -> bytes:
        n_samples = len(chunk) // 2
        fmt = f"<{n_samples}h"
        samples = struct.unpack(fmt, chunk[: n_samples * 2])
        scaled = [max(-32768, min(32767, int(s * volume))) for s in samples]
        return struct.pack(fmt, *scaled)

    def _update_signal_level(self, chunk: bytes) -> None:
        n_samples = len(chunk) // 2
        if n_samples == 0:
            return
        fmt = f"<{n_samples}h"
        try:
            samples = struct.unpack(fmt, chunk[: n_samples * 2])
        except struct.error:
            return
        sum_sq = sum(s * s for s in samples)
        rms = math.sqrt(sum_sq / n_samples)
        db = 20.0 * math.log10(max(rms, 1.0)) - 90.0

        now = time.monotonic()
        squelch_open = rms > 500

        with self._state_lock:
            self._signal_rms = rms
            self._signal_db = db

            # Signal history at ~1 sample/sec
            if now - self._last_signal_history_ts >= 1.0:
                self._signal_history.append(rms)
                self._last_signal_history_ts = now

            # Squelch break detection (RMS crosses above 500)
            if squelch_open and not self._squelch_was_open:
                self._squelch_break_count += 1
            self._squelch_was_open = squelch_open

    # ── process management ───────────────────────────────────────────

    def _terminate_process(self) -> None:
        lock = getattr(self, "_process_lock", None)
        if lock is None:
            group = getattr(self, "_process_group", None)
            self._process_group = None
            self._process = None
            self._pid = None
        else:
            with lock:
                group = getattr(self, "_process_group", None)
                self._process_group = None
                self._process = None
                self._pid = None
        if group is not None:
            group.stop()

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
