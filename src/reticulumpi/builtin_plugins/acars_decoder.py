"""ACARS aircraft message decoder plugin.

Decodes Aircraft Communications Addressing and Reporting System messages
using acarsdec with RTL-SDR.
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
from reticulumpi.process_supervisor import (
    ManagedProcessGroup,
    ProcessFailure,
    ProcessSpec,
    RestartPolicy,
)
from reticulumpi.sdr_scheduler import PRIORITY_BACKGROUND

_ACARS_LABELS: dict[str, str] = {
    "H1": "General message",
    "SA": "Position report",
    "5Z": "Airline designated",
    "Q0": "Link test",
    "QA": "Free text",
    "QC": "Free text",
    "QD": "Free text",
    "QF": "Free text",
    "QK": "Free text",
    "QR": "Free text",
    "80": "Terminal weather",
    "83": "Departure clearance",
    "B6": "ACARS data",
    "C1": "Position report",
    "AA": "Weather request",
    "AB": "Weather data",
    "BA": "Flight information",
    "_d": "Command response",
    "4T": "Free text",
}


class ACARSDecoder(SignalPluginBase):
    """ACARS message decoder via acarsdec."""

    plugin_name = "acars_decoder"
    plugin_version = "0.1.0"
    plugin_description = "ACARS aircraft message decoder"
    broadcast_keys = "acars"

    signal_priority = PRIORITY_BACKGROUND
    signal_continuous = True
    signal_label = "ACARS Decoder"

    def validate_config(self) -> None:
        self._gain = self.config.get("gain", None)
        self._ppm = int(self.config.get("ppm", 0))
        self._decoder_bin = str(self.config.get("decoder_bin", "acarsdec"))
        self._frequencies = self.config.get("frequencies_mhz", [131.550, 131.525, 131.725])
        self._max_messages = int(self.config.get("max_messages", 200))
        self._max_restarts = int(self.config.get("max_restarts", 5))
        self._station_id = str(self.config.get("station_id", "reticulumpi"))

    def _on_start(self) -> None:
        self._recent_messages: deque[dict[str, Any]] = deque(maxlen=self._max_messages)
        self._stats = {
            "messages_total": 0,
            "messages_by_label": {},
            "messages_by_freq": {},
            "unique_flights_today": 0,
            "unique_tails_today": 0,
            "error_count": 0,
            "last_message_at": None,
        }
        self._seen_flights: set[str] = set()
        self._seen_tails: set[str] = set()
        self._daily_reset_ts = time.time()
        self._airline_stats: dict[str, int] = {}
        self._hourly_rate: deque[int] = deque(maxlen=24)
        self._hourly_count = 0
        self._hourly_ts = time.time()
        self._level_min: float | None = None
        self._level_max: float | None = None
        self._level_sum: float = 0.0
        self._level_count: int = 0
        self._status = "idle"
        self._last_error: str | None = None
        self._restart_count = 0
        self._snapshot_dirty = True

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
            "-p",
            str(self._ppm),
            "-o",
            "4",
            "-r",
            "0",
        ]
        if self._gain is not None:
            cmd += ["-g", str(self._gain)]
        for freq in self._frequencies:
            cmd.append(str(freq))

        self.log.debug("Launching: %s", " ".join(cmd))
        group = ManagedProcessGroup(
            [
                ProcessSpec(
                    tuple(cmd),
                    name="acarsdec",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            ],
            # Scheduler-backed decoders release the dongle during backoff and
            # restart only after normal scheduler reacquisition.
            restart_policy=RestartPolicy(enabled=False),
            on_started=self._on_decoder_started,
            on_unexpected_exit=self._on_decoder_failure,
        )
        self._process_group = group
        try:
            group.start()
        except Exception:
            self._process_group = None
            raise

        self.log.info(
            "ACARS decoder started on %s (PID %d)",
            ", ".join(str(f) for f in self._frequencies),
            self._pid,
        )

    def _on_decoder_started(
        self,
        processes: tuple[subprocess.Popen[Any], ...],
        restarted: bool,
    ) -> None:
        process = processes[0]
        self._process = process
        self._pid = process.pid
        self._status = "running"
        self._last_error = None
        was_retry = restarted or self._sdr_retry_pending
        if restarted:
            group = self._process_group
            self._restart_count = group.restart_count if group is not None else 1
        elif not was_retry:
            self._restart_count = 0
        if self.plugin_state.value == "ready":
            self.mark_ready()
        self._start_stderr_reader(process, prefix="acarsdec")
        self._start_thread(lambda: self._parser_loop(process), name="acars-parser")
        if was_retry:
            self.log.warning(
                "ACARS decoder restarted after unexpected exit (attempt %d)",
                self._restart_count,
            )

    def _on_decoder_failure(self, failure: ProcessFailure) -> None:
        self._status = "restarting"
        self._last_error = (
            f"{failure.stage_name or 'decoder'}: {failure.reason} (rc={failure.returncode})"
        )
        self.mark_degraded(self._last_error)
        self._update_snapshot_cache()
        if not self._schedule_sdr_retry(self._max_restarts):
            self._on_decoder_exhausted(failure)

    def _on_decoder_restart_failed(self, error: BaseException, attempt: int) -> None:
        self._status = "restarting"
        self._last_error = f"restart {attempt} failed: {error}"
        self.mark_degraded(self._last_error)

    def _on_decoder_exhausted(self, failure: ProcessFailure) -> None:
        self._status = "error"
        self._last_error = f"ACARS decoder restart budget exhausted: {failure.reason}"
        self.mark_degraded(self._last_error)
        should_release = self._dongle_active or self._dongle_generation is not None
        self._dongle_active = False
        if should_release:
            self._release_dongle(suspend=True)
        self._update_snapshot_cache()

    def _parser_loop(self, process: subprocess.Popen[Any] | None = None) -> None:
        proc = process or self._process
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
            self.log.exception("ACARS parser crashed")
        finally:
            group = self._process_group
            if self._active and self._process is proc and group is not None and group.running:
                group.notify_unexpected_eof(0, "ACARS stdout ended")

    def _handle_message(self, msg: dict[str, Any]) -> None:
        self._maybe_reset_daily()

        label = msg.get("label", "??")
        freq = msg.get("freq", 0)
        flight = msg.get("flight", "").strip()
        tail = msg.get("tail", "").strip()
        text = msg.get("text", "").strip()
        error = msg.get("error", 0)
        level = msg.get("level", 0)

        record = {
            "timestamp": msg.get("timestamp", time.time()),
            "freq_mhz": freq,
            "level_dbfs": level,
            "mode": msg.get("mode", ""),
            "label": label,
            "label_desc": _ACARS_LABELS.get(label, f"Label {label}"),
            "block_id": msg.get("block_id", ""),
            "tail": tail,
            "flight": flight,
            "msgno": msg.get("msgno", ""),
            "text": text,
            "error": error,
        }

        self._recent_messages.appendleft(record)

        self._stats["messages_total"] += 1
        self._stats["last_message_at"] = time.time()

        label_counts = self._stats["messages_by_label"]
        label_counts[label] = label_counts.get(label, 0) + 1

        freq_key = str(freq)
        freq_counts = self._stats["messages_by_freq"]
        freq_counts[freq_key] = freq_counts.get(freq_key, 0) + 1

        if error:
            self._stats["error_count"] += 1

        if flight and flight not in self._seen_flights:
            self._seen_flights.add(flight)
            self._stats["unique_flights_today"] = len(self._seen_flights)

        if tail and tail not in self._seen_tails:
            self._seen_tails.add(tail)
            self._stats["unique_tails_today"] = len(self._seen_tails)

        # Airline code tracking (first 3 chars of flight number)
        if flight and len(flight) >= 3:
            airline = flight[:3].strip()
            if airline:
                self._airline_stats[airline] = self._airline_stats.get(airline, 0) + 1

        # Hourly message rate
        now_ts = time.time()
        if now_ts - self._hourly_ts >= 3600:
            self._hourly_rate.append(self._hourly_count)
            self._hourly_count = 0
            self._hourly_ts = now_ts
        self._hourly_count += 1

        # Signal level min/max/running-average
        if level:
            lf = float(level)
            if self._level_min is None or lf < self._level_min:
                self._level_min = lf
            if self._level_max is None or lf > self._level_max:
                self._level_max = lf
            self._level_sum += lf
            self._level_count += 1

        try:
            self.event_bus.publish(
                events.ACARS_MESSAGE_DECODED,
                {
                    "flight": flight,
                    "tail": tail,
                    "label": label,
                },
            )
        except Exception:
            self.log.debug("event publish failed", exc_info=True)

        self._snapshot_dirty = True

    def _maybe_reset_daily(self) -> None:
        now = time.time()
        if now - self._daily_reset_ts > 86400:
            self._seen_flights.clear()
            self._seen_tails.clear()
            self._stats["unique_flights_today"] = 0
            self._stats["unique_tails_today"] = 0
            self._daily_reset_ts = now

    def _update_snapshot_cache(self) -> None:
        total = self._stats["messages_total"]
        error_count = self._stats["error_count"]
        error_rate = (error_count / total * 100) if total > 0 else 0.0

        # Top 20 airlines by message count
        top_airlines = dict(
            sorted(self._airline_stats.items(), key=lambda x: x[1], reverse=True)[:20],
        )

        level_avg = round(self._level_sum / self._level_count, 1) if self._level_count > 0 else None

        with self._cache_lock:
            self._snapshot_cache = {
                "status": self._status,
                "error": self._last_error,
                "recent_messages": list(self._recent_messages),
                "stats": {
                    **self._stats,
                    "error_rate_pct": round(error_rate, 1),
                    "airline_stats": top_airlines,
                    "hourly_rate": list(self._hourly_rate),
                    "level_min": self._level_min,
                    "level_max": self._level_max,
                    "level_avg": level_avg,
                },
            }
        self._snapshot_dirty = False

    def broadcast_snapshot(self, cycle_count: int = 0) -> dict[str, Any] | None:
        if self._snapshot_dirty:
            self._update_snapshot_cache()
        return super().broadcast_snapshot(cycle_count)

    def get_status(self) -> dict[str, Any]:
        return {
            "active": self._active,
            "status": self._status,
            "error": self._last_error,
            "messages_total": self._stats["messages_total"],
            "unique_flights_today": self._stats["unique_flights_today"],
        }
