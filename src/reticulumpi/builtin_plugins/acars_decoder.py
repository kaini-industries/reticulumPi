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

        cmd = [
            decoder,
            "-d", str(device_index),
            "-p", str(self._ppm),
            "-o", "4",
            "-r", "0",
        ]
        if self._gain is not None:
            cmd += ["-g", str(self._gain)]
        for freq in self._frequencies:
            cmd.append(str(freq))

        self.log.debug("Launching: %s", " ".join(cmd))
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._pid = self._process.pid
        self._status = "running"
        self._restart_count = 0

        self._start_log_reader(self._stderr_fake(), prefix="acarsdec")
        self._start_thread(self._parser_loop, name="acars-parser")

        self.log.info(
            "ACARS decoder started on %s (PID %d)",
            ", ".join(str(f) for f in self._frequencies),
            self._pid,
        )

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
            self.log.exception("ACARS parser crashed")

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

        try:
            self.event_bus.publish(events.ACARS_MESSAGE_DECODED, {
                "flight": flight, "tail": tail, "label": label,
            })
        except Exception:
            pass

        self._update_snapshot_cache()

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
        with self._cache_lock:
            self._snapshot_cache = {
                "status": self._status,
                "error": self._last_error,
                "recent_messages": list(self._recent_messages),
                "stats": {
                    **self._stats,
                    "error_rate_pct": round(error_rate, 1),
                },
            }

    def get_status(self) -> dict[str, Any]:
        return {
            "active": self._active,
            "status": self._status,
            "error": self._last_error,
            "messages_total": self._stats["messages_total"],
            "unique_flights_today": self._stats["unique_flights_today"],
        }
