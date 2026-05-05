"""Spectrum scanner plugin — RTL-SDR sweep-based waterfall feed.

Runs ``rtl_power`` as a long-lived subprocess that repeatedly sweeps a
configured frequency range and writes FFT power rows to stdout in CSV
form.  This plugin parses each row, assembles complete sweeps (a sweep
may be split across multiple ``rtl_power`` CSV lines when the span is
wider than the tuner's stable bandwidth, ~2 MHz), and maintains an
in-memory rolling waterfall that the web dashboard reads via
``get_snapshot()``.

Example config (under ``plugins:`` in ``/etc/reticulumpi/config.yaml``):

    spectrum_scanner:
      enabled: true
      freq_start_mhz: 88.0
      freq_stop_mhz: 108.0
      bin_khz: 25.0
      sweep_seconds: 2         # float; 0.1-60 (sub-second needs rtl_power_fftw)
      gain_db: 40.0            # null = auto
      ppm: 0
      waterfall_rows: 128
      device_index: 0
      power_command: rtl_power  # or rtl_power_fftw for faster sweeps

Requirements:
    * ``rtl_power`` available on PATH (package: ``rtl-sdr``).
    * Kernel DVB-T driver blacklisted so userspace tools can claim the
      device (see ``/etc/modprobe.d/blacklist-rtlsdr.conf``).
    * Plugin user must have udev access to the SDR (membership in the
      ``plugdev`` group satisfies the default rtl-sdr udev rule).

If ``rtl_power`` is missing or the device is unreachable, the plugin
logs a warning and stays idle rather than crashing the node.  It
auto-restarts the subprocess on unexpected exit with an exponential
back-off up to ``max_restarts`` attempts.
"""

from __future__ import annotations

import math
import shutil
import subprocess
import threading
import time
from collections import deque
from typing import Any

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase


EVENT_SPECTRUM_SWEEP = events.SPECTRUM_SWEEP
EVENT_SPECTRUM_STATUS = events.SPECTRUM_STATUS


# E4000 (and most RTL-SDR tuners) support a discrete set of LNA gain
# steps.  rtl_power snaps requested gains to the nearest supported value
# internally, so we don't strictly need to — but we warn the user if the
# request is far off a known step for documentation's sake.
_COMMON_GAIN_STEPS_DB = (
    -1.0, 1.5, 4.0, 6.5, 9.0, 11.5, 14.0, 16.5,
    19.0, 21.5, 24.0, 29.0, 34.0, 42.0,
)

# Elonics E4000 tuner has a local-oscillator dead zone in L-band; sweeps
# that overlap this range produce garbage.  R820T2 has no equivalent
# gap.  We warn (not reject) so the config works unchanged across tuner
# variants — other tuners simply won't report this range as invalid.
_E4000_LO_GAP_MHZ = (1101.0, 1234.0)


class SpectrumScanner(PluginBase):
    """Continuous ``rtl_power`` sweep feed for the web dashboard."""

    plugin_name = "spectrum_scanner"
    plugin_version = "0.1.0"
    plugin_description = "RTL-SDR spectrum sweep + waterfall feed"

    # --- config validation ---------------------------------------------------

    def validate_config(self) -> None:
        cfg = self.config

        self._freq_start_mhz = float(cfg.get("freq_start_mhz", 88.0))
        self._freq_stop_mhz = float(cfg.get("freq_stop_mhz", 108.0))
        if self._freq_stop_mhz <= self._freq_start_mhz:
            raise ValueError(
                f"freq_stop_mhz ({self._freq_stop_mhz}) must be greater than "
                f"freq_start_mhz ({self._freq_start_mhz})"
            )

        bin_khz = float(cfg.get("bin_khz", 25.0))
        if not 1.0 <= bin_khz <= 1000.0:
            raise ValueError(f"bin_khz must be 1-1000, got {bin_khz}")
        self._bin_khz = bin_khz

        sweep_seconds = float(cfg.get("sweep_seconds", 2))
        if not 0.1 <= sweep_seconds <= 60:
            raise ValueError(f"sweep_seconds must be 0.1-60, got {sweep_seconds}")
        self._sweep_seconds = sweep_seconds

        gain_db = cfg.get("gain_db", 40.0)
        if gain_db is not None:
            gain_db = float(gain_db)
            if not -10.0 <= gain_db <= 60.0:
                raise ValueError(f"gain_db must be -10..60 or null (auto), got {gain_db}")
        self._gain_db = gain_db

        self._ppm = int(cfg.get("ppm", 0))

        wf_rows = int(cfg.get("waterfall_rows", 128))
        if not 8 <= wf_rows <= 2048:
            raise ValueError(f"waterfall_rows must be 8-2048, got {wf_rows}")
        self._waterfall_rows = wf_rows

        self._device_id = str(cfg.get("device_serial") or cfg.get("device_index", "0"))
        self._power_command = str(cfg.get("power_command", "rtl_power"))
        self._max_restarts = int(cfg.get("max_restarts", 5))
        self._health_interval = float(cfg.get("health_check_interval", 5.0))

    # --- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._state_lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._pid: int | None = None
        self._restart_count = 0
        self._sweep_count = 0
        self._last_sweep_at: float | None = None
        self._rtl_power_path: str | None = None
        self._last_error: str | None = None
        self._status = "starting"
        self._resolved_index: int | None = None
        self._event_sweep_topic = EVENT_SPECTRUM_SWEEP
        self._event_status_topic = EVENT_SPECTRUM_STATUS

        # Rolling in-memory state the dashboard reads via get_snapshot().
        self._bins_hz: list[int] = []
        # Power arrays carry None for bins rtl_power couldn't sample
        # (nan/inf from the CSV, filtered in _handle_csv_line).  The
        # snapshot serializer already passes None straight through to
        # JSON null, which the dashboard treats as "no reading".
        self._latest_powers_db: list[float | None] = []
        # Each waterfall entry is (flush_timestamp, powers).  Carrying the
        # timestamp alongside the power row lets the dashboard report a
        # real wall-clock age per row instead of the drift-prone
        # rowIdx * sweep_seconds approximation — which is wrong whenever a
        # wide-span sweep takes longer than sweep_seconds, the scanner
        # restarts mid-history, or the tab was paused.
        self._waterfall: deque[tuple[float, list[float | None]]] = deque(
            maxlen=self._waterfall_rows
        )

        # Parser-local accumulator (flushed per sweep).  Maps
        # segment_start_hz -> (bin_step_hz, [power_db, ...]).
        self._segments: dict[int, tuple[int, list[float | None]]] = {}
        self._current_ts: str | None = None

        try:
            from reticulumpi.rtlsdr import resolve_device
            self._resolved_index = resolve_device(self._device_id, caller=self.plugin_name)
        except (RuntimeError, ValueError) as exc:
            self.log.error("RTL-SDR device resolution failed: %s", exc)
            self._set_status("error", str(exc))

        self._active = True
        self._device_released = False
        self._supervisor_alive = True
        self._start_thread(self._supervisor_loop, name="spectrum-supervisor")

        # E4000 gap warning — informational only.
        gap_lo, gap_hi = _E4000_LO_GAP_MHZ
        if self._freq_start_mhz < gap_hi and self._freq_stop_mhz > gap_lo:
            self.log.warning(
                "Configured span %.2f-%.2f MHz overlaps the E4000 L-band LO "
                "gap (%.0f-%.0f MHz); expect garbage bins in that range if "
                "your tuner is an Elonics E4000.",
                self._freq_start_mhz, self._freq_stop_mhz, gap_lo, gap_hi,
            )

        # Snap-to-step diagnostic.
        if self._gain_db is not None:
            nearest = min(_COMMON_GAIN_STEPS_DB, key=lambda s: abs(s - self._gain_db))
            if abs(nearest - self._gain_db) > 0.6:
                self.log.info(
                    "Requested gain %.1f dB is far from the nearest typical "
                    "tuner step (%.1f dB); rtl_power will snap it internally.",
                    self._gain_db, nearest,
                )

        self.log.info(
            "%s started: %.2f-%.2f MHz, %.1f kHz bins, %.1fs sweep%s",
            self.plugin_name,
            self._freq_start_mhz, self._freq_stop_mhz, self._bin_khz,
            self._sweep_seconds,
            f", gain {self._gain_db:.1f} dB" if self._gain_db is not None else ", auto gain",
        )

    def stop(self) -> None:
        self._active = False
        self._terminate_process()
        self._join_threads(timeout=5.0)
        self._set_status("stopped")

    # --- public API (dashboard / monitoring) ---------------------------------

    def get_status(self) -> dict[str, Any]:
        running = self._process is not None and self._process.poll() is None
        return {
            "active": self._active,
            "running": running,
            "pid": self._pid,
            "status": self._status,
            "sweep_count": self._sweep_count,
            "last_sweep_at": self._last_sweep_at,
            "restart_count": self._restart_count,
            "rtl_power_path": self._rtl_power_path,
            "error": self._last_error,
        }

    # How many recent sweeps to include in the snapshot's ``waterfall_tail``.
    # The client maintains its own scrolling canvas history; this tail only
    # carries "new sweeps since last broadcast" (plus a small buffer for
    # reconnects / bursts).  Keeping this small keeps the WebSocket
    # broadcast compact even when the plugin retains hundreds of sweeps
    # internally.
    _SNAPSHOT_TAIL_ROWS = 8

    def get_snapshot(self) -> dict[str, Any]:
        """Return a WebSocket-ready snapshot of the current sweep state.

        Sent every WebSocket broadcast tick (~5s).  To keep the wire
        payload bounded, we include only the most recent few sweeps
        (``waterfall_tail``) plus a monotonic ``sweep_count``; the client
        uses the count to dedupe and appends new tail rows to its own
        in-browser scrolling history.
        """
        with self._state_lock:
            tail = list(self._waterfall)[-self._SNAPSHOT_TAIL_ROWS:]
            # Decompose (ts, powers) tuples into two parallel lists kept in
            # lock-step on the wire.  `waterfall_tail` is unchanged for
            # backward-compat with older clients; `waterfall_tail_times` is
            # the new sibling field the dashboard uses for honest per-row age.
            return {
                "status": self._status,
                "error": self._last_error,
                "freq_start_hz": int(self._freq_start_mhz * 1_000_000),
                "freq_stop_hz": int(self._freq_stop_mhz * 1_000_000),
                "bin_hz_requested": int(self._bin_khz * 1000),
                "sweep_seconds": self._sweep_seconds,
                "gain_db": self._gain_db,
                "ppm": self._ppm,
                "sweep_count": self._sweep_count,
                "last_sweep_at": self._last_sweep_at,
                "waterfall_rows": self._waterfall_rows,
                "bins_hz": list(self._bins_hz),
                "latest_powers_db": [
                    round(v, 1) if v is not None else None
                    for v in self._latest_powers_db
                ],
                "waterfall_tail": [
                    [round(v, 1) if v is not None else None for v in powers]
                    for _, powers in tail
                ],
                "waterfall_tail_times": [round(ts, 3) for ts, _ in tail],
            }

    def get_history(self) -> dict[str, Any]:
        """Return the full in-memory waterfall buffer for one-shot backfill.

        ``get_snapshot()`` deliberately ships only the last few sweeps to
        keep every WebSocket broadcast small. This method returns the
        entire rolling buffer (capped by ``waterfall_rows`` in config) so
        the dashboard can backfill its waterfall canvas on first load —
        a one-time cost per page open, with no impact on steady-state
        WS payload size.
        """
        with self._state_lock:
            # Same decomposition pattern as get_snapshot: parallel arrays
            # (rows / row_timestamps) stay in lock-step.  rows shape is
            # unchanged for backward-compat; row_timestamps is the new sibling.
            return {
                "available": True,
                "sweep_count": self._sweep_count,
                "bin_count": len(self._bins_hz),
                "waterfall_rows": self._waterfall_rows,
                "rows": [
                    [round(v, 1) if v is not None else None for v in powers]
                    for _, powers in self._waterfall
                ],
                "row_timestamps": [
                    round(ts, 3) for ts, _ in self._waterfall
                ],
            }

    # --- supervisor ----------------------------------------------------------

    def _supervisor_loop(self) -> None:
        """Launch rtl_power once; on exit, back off and retry up to max_restarts."""
        self._supervisor_alive = True
        try:
            self._supervisor_loop_inner()
        finally:
            self._supervisor_alive = False

    def _supervisor_loop_inner(self) -> None:
        self._rtl_power_path = shutil.which(self._power_command)
        if not self._rtl_power_path:
            self._set_status(
                "unavailable",
                f"{self._power_command} not found on PATH",
            )
            self.log.warning(
                "%s binary not found; %s will stay idle.",
                self._power_command, self.plugin_name,
            )
            return

        while self._active:
            if self._device_released:
                self._set_status("paused", "device released for external use")
                self._sleep_while_active(1.0)
                continue

            try:
                self._launch_rtl_power()
            except Exception as exc:
                self._set_status("error", f"launch failed: {exc}")
                self.log.exception("Failed to launch rtl_power")
                break

            # Start a parser thread dedicated to this subprocess instance;
            # it exits when rtl_power closes its stdout.
            parser = threading.Thread(
                target=self._parser_loop,
                name="spectrum-parser",
                daemon=True,
            )
            parser.start()

            # Block until rtl_power exits or we're stopped.
            while self._active and self._process is not None:
                rc = self._process.poll()
                if rc is not None:
                    self.log.warning("rtl_power exited (code %s)", rc)
                    break
                self._sleep_while_active(self._health_interval)

            # Wait for parser to drain.
            parser.join(timeout=2.0)
            self._terminate_process()  # idempotent; cleans up zombies

            if not self._active:
                break

            # Backoff + restart.
            self._restart_count += 1
            if self._restart_count > self._max_restarts:
                self._set_status(
                    "error",
                    f"rtl_power exceeded max_restarts ({self._max_restarts})",
                )
                self.log.error(
                    "rtl_power exceeded max_restarts (%d); giving up",
                    self._max_restarts,
                )
                break

            backoff = min(60.0, 2.0 ** self._restart_count)
            self._set_status("restarting", f"backoff {backoff:.0f}s")
            self.log.info(
                "Restarting rtl_power in %.0fs (attempt %d/%d)",
                backoff, self._restart_count, self._max_restarts,
            )
            self._sleep_while_active(backoff)

    def _launch_rtl_power(self) -> None:
        cmd = self._build_cmd()
        self.log.debug("Launching: %s", " ".join(cmd))
        # stderr merged onto stdout so crash tracebacks are captured;
        # non-CSV lines are ignored by the parser.
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            # text mode enables proper line buffering; bufsize=1 only works
            # with text streams (binary mode silently falls back to block
            # buffering which would stall the parser until a large block
            # fills).
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._pid = self._process.pid
        self._current_ts = None
        self._segments = {}
        self._set_status("running")
        self.log.info(
            "Started rtl_power sweep %.2f-%.2f MHz (PID %d)",
            self._freq_start_mhz, self._freq_stop_mhz, self._pid,
        )

    def _build_cmd(self) -> list[str]:
        assert self._rtl_power_path is not None
        freq_arg = (
            f"{self._freq_start_mhz:.3f}M:{self._freq_stop_mhz:.3f}M:"
            f"{self._bin_khz:.3f}k"
        )
        cmd = [
            self._rtl_power_path,
            "-f", freq_arg,
            "-i", f"{self._sweep_seconds:g}s",
            "-d", str(self._resolved_index if self._resolved_index is not None else self._device_id),
            "-p", str(self._ppm),
        ]
        if self._gain_db is not None:
            cmd += ["-g", f"{self._gain_db:.1f}"]
        # Run forever; write CSV to stdout.
        cmd += ["-e", "0", "-"]
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
                    self.log.warning("rtl_power did not stop; sending SIGKILL")
                    proc.kill()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.log.warning("rtl_power did not exit after SIGKILL")
        except Exception:
            self.log.exception("Error stopping rtl_power")
        finally:
            if proc.stdout:
                try:
                    proc.stdout.close()
                except Exception:
                    pass

    def _restart_sweep(self) -> None:
        """Restart the rtl_power supervisor after an external interruption.

        Subclasses (e.g. LoraChirpViewer) call this after temporarily
        stopping the sweep to use the USB device for I/Q capture.
        Clears the device-released flag so the existing supervisor thread
        resumes; only spawns a new thread if the old one exited.
        """
        self._restart_count = 0
        self._segments = {}
        self._current_ts = None
        self._device_released = False
        if not self._supervisor_alive:
            self._start_thread(self._supervisor_loop, name="spectrum-supervisor")

    # --- parser --------------------------------------------------------------

    def _parser_loop(self) -> None:
        """Read rtl_power CSV from stdout, assembling one sweep per timestamp."""
        proc = self._process
        if proc is None or proc.stdout is None:
            return
        try:
            for raw in proc.stdout:
                if not self._active:
                    break
                # text=True on Popen yields str rows already.
                line = raw.strip() if isinstance(raw, str) else raw.decode(
                    "utf-8", errors="replace"
                ).strip()
                if not line or line.startswith("#"):
                    continue
                self._handle_csv_line(line)
        except (ValueError, OSError):
            # stdout closed — process exited.
            pass
        except Exception:
            self.log.exception("Parser loop crashed")

        # Final flush — rtl_power may have been killed mid-sweep, but if
        # we have at least some segments, emit them so the last
        # waterfall row isn't lost.
        self._flush_current_sweep()

    def _handle_csv_line(self, line: str) -> None:
        # rtl_power CSV:
        #   date, time, freq_lo_hz, freq_hi_hz, bin_step_hz, samples, dB0, dB1, ...
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            return
        try:
            ts = parts[0] + " " + parts[1]
            freq_lo = int(float(parts[2]))
            _freq_hi = int(float(parts[3]))  # unused directly; we derive from bin_step × count
            bin_step = int(float(parts[4]))
            # parts[5] = samples count, unused
            # rtl_power can emit "nan" / "-inf" for bins it couldn't
            # sample (DC-spike filter edges, tuner settling, etc.).
            # float() parses those successfully but they break strict
            # JSON serialization and corrupt auto-scale / colourmap
            # math downstream.  Substitute None (JSON null) so the
            # bin *position* is preserved — dbs is indexed by position
            # to compute per-bin frequencies in _flush_current_sweep.
            dbs: list[float | None] = []
            for x in parts[6:]:
                if not x:
                    dbs.append(None)
                    continue
                v = float(x)
                dbs.append(v if math.isfinite(v) else None)
        except (ValueError, IndexError):
            return
        if not dbs or bin_step <= 0:
            return

        if self._current_ts is not None and ts != self._current_ts:
            self._flush_current_sweep()

        self._current_ts = ts
        self._segments[freq_lo] = (bin_step, dbs)

    def _flush_current_sweep(self) -> None:
        """Assemble accumulated segments into a complete sweep; publish + roll waterfall."""
        segs = self._segments
        self._segments = {}
        if not segs:
            return

        freqs: list[int] = []
        powers: list[float | None] = []
        for freq_lo in sorted(segs.keys()):
            step, dbs = segs[freq_lo]
            for i, db in enumerate(dbs):
                freqs.append(freq_lo + i * step)
                powers.append(db)

        if not freqs:
            return

        now = time.time()
        with self._state_lock:
            self._bins_hz = freqs
            self._latest_powers_db = powers
            self._waterfall.append((now, powers))
            self._sweep_count += 1
            self._last_sweep_at = now

        # Fire-and-forget event bus notification for any subscribers
        # (the dashboard pulls via get_snapshot() so it doesn't rely on
        # this — but external plugins could tap in).
        try:
            self.event_bus.publish(
                self._event_sweep_topic,
                {
                    "timestamp": now,
                    "sweep_count": self._sweep_count,
                    "bin_count": len(freqs),
                    "freq_start_hz": freqs[0],
                    "freq_stop_hz": freqs[-1],
                },
            )
        except Exception:
            self.log.debug("event_bus publish failed", exc_info=True)

    # --- status helper -------------------------------------------------------

    def _set_status(self, status: str, error: str | None = None) -> None:
        prev = self._status
        with self._state_lock:
            self._status = status
            self._last_error = error
        if status != prev:
            try:
                self.event_bus.publish(
                    self._event_status_topic,
                    {"status": status, "error": error, "timestamp": time.time()},
                )
            except Exception:
                self.log.debug("event_bus publish failed", exc_info=True)
