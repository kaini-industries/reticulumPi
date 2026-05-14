"""NOAA Weather Radio SAME alert monitor.

Decodes Specific Area Message Encoding (SAME) headers from NOAA Weather
Radio broadcasts using rtl_fm piped to multimon-ng.  Publishes severe
weather alerts on the event bus.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from collections import deque
from typing import Any

from reticulumpi import events
from reticulumpi.builtin_plugins.signal_plugin_base import SignalPluginBase
from reticulumpi.sdr_scheduler import PRIORITY_CRITICAL

_SAME_RE = re.compile(
    r"EAS:\s*ZCZC-"
    r"(?P<org>[A-Z]{3})-"
    r"(?P<event>[A-Z]{3})-"
    r"(?P<fips>[\d+\-]+)-"
    r"(?P<purge>\d{4})-"
    r"(?P<issued>\d{7})-"
    r"(?P<callsign>[A-Za-z0-9/]+)-?"
)

_EVENT_CODES: dict[str, tuple[str, str]] = {
    "TOR": ("Tornado Warning", "extreme"),
    "SVR": ("Severe Thunderstorm Warning", "severe"),
    "FFW": ("Flash Flood Warning", "severe"),
    "SMW": ("Special Marine Warning", "severe"),
    "EWW": ("Extreme Wind Warning", "extreme"),
    "FRW": ("Fire Warning", "severe"),
    "HUW": ("Hurricane Warning", "extreme"),
    "TSW": ("Tsunami Warning", "extreme"),
    "SVA": ("Severe Thunderstorm Watch", "moderate"),
    "TOA": ("Tornado Watch", "moderate"),
    "FFA": ("Flash Flood Watch", "moderate"),
    "WSW": ("Winter Storm Warning", "severe"),
    "BZW": ("Blizzard Warning", "severe"),
    "WCH": ("Wind Chill Warning", "severe"),
    "EHW": ("Excessive Heat Warning", "severe"),
    "HWW": ("High Wind Warning", "severe"),
    "RWT": ("Required Weekly Test", "info"),
    "RMT": ("Required Monthly Test", "info"),
    "ADR": ("Administrative Message", "info"),
    "DMO": ("Practice/Demo Warning", "info"),
}

_SEVERITY_RANK = {"extreme": 0, "severe": 1, "moderate": 2, "minor": 3, "info": 4}

_ORG_NAMES = {
    "WXR": "National Weather Service",
    "CIV": "Civil Authorities",
    "EAS": "EAS Participant",
    "PEP": "Primary Entry Point",
}

_FIPS_STATES: dict[str, str] = {
    "00": "US", "01": "AL", "02": "AK", "04": "AZ", "05": "AR",
    "06": "CA", "08": "CO", "09": "CT", "10": "DE", "11": "DC",
    "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL",
    "18": "IN", "19": "IA", "20": "KS", "21": "KY", "22": "LA",
    "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
    "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV",
    "33": "NH", "34": "NJ", "35": "NM", "36": "NY", "37": "NC",
    "38": "ND", "39": "OH", "40": "OK", "41": "OR", "42": "PA",
    "44": "RI", "45": "SC", "46": "SD", "47": "TN", "48": "TX",
    "49": "UT", "50": "VT", "51": "VA", "53": "WA", "54": "WV",
    "55": "WI", "56": "WY",
}


def _parse_fips_codes(raw: str) -> list[str]:
    parts = re.split(r"[+\-]", raw)
    return [p for p in parts if p and len(p) == 6]


def _fips_label(code: str) -> str:
    if len(code) != 6:
        return code
    state_code = code[1:3]
    state = _FIPS_STATES.get(state_code, state_code)
    county = code[3:]
    return f"FIPS {code} ({state}-{county})"


def _parse_issued_ts(issued: str) -> float | None:
    if len(issued) != 7:
        return None
    try:
        jday = int(issued[:3])
        hour = int(issued[3:5])
        minute = int(issued[5:7])
        now_ts = time.time()
        now = time.gmtime(now_ts)
        year = now.tm_year
        import calendar
        jan1 = calendar.timegm((year, 1, 1, 0, 0, 0, 0, 1, -1))
        ts = jan1 + (jday - 1) * 86400 + hour * 3600 + minute * 60
        if ts > now_ts + 86400:
            jan1_prev = calendar.timegm((year - 1, 1, 1, 0, 0, 0, 0, 1, -1))
            ts = jan1_prev + (jday - 1) * 86400 + hour * 3600 + minute * 60
        return ts
    except (ValueError, OverflowError):
        return None


def _compute_purge_ts(issued_ts: float | None, purge: str) -> float | None:
    if issued_ts is None or len(purge) != 4:
        return None
    try:
        hours = int(purge[:2])
        minutes = int(purge[2:4])
        return issued_ts + hours * 3600 + minutes * 60
    except ValueError:
        return None


class WeatherAlert(SignalPluginBase):
    """NOAA Weather Radio SAME alert decoder."""

    plugin_name = "weather_alert"
    plugin_version = "0.1.0"
    plugin_description = "NOAA Weather Radio SAME alert monitor"
    broadcast_keys = "weather_alert"

    signal_priority = PRIORITY_CRITICAL
    signal_continuous = True
    signal_label = "Weather Alert Monitor"

    def validate_config(self) -> None:
        self._freq_hz = int(
            float(self.config.get("freq_mhz", 162.55)) * 1_000_000,
        )
        self._gain_db = self.config.get("gain", None)
        self._ppm = int(self.config.get("ppm", 0))
        self._max_history = int(self.config.get("max_history", 50))
        self._fips_filter: set[str] | None = None
        fips = self.config.get("fips_filter")
        if fips:
            self._fips_filter = set(str(f) for f in fips)
        self._forward_to_alert_system = bool(
            self.config.get("forward_to_alert_system", True),
        )

    def _on_start(self) -> None:
        self._alert_history: deque[dict[str, Any]] = deque(maxlen=self._max_history)
        self._active_alert: dict[str, Any] | None = None
        self._stats = {
            "headers_decoded_total": 0,
            "alerts_by_type": {},
            "last_header_at": None,
        }
        self._status = "idle"
        self._last_error: str | None = None

    def _on_stop(self) -> None:
        pass

    def _launch_subprocess(self, device_index: int) -> None:
        rtl_fm = shutil.which("rtl_fm")
        multimon = shutil.which("multimon-ng")
        if not rtl_fm or not multimon:
            missing = []
            if not rtl_fm:
                missing.append("rtl_fm")
            if not multimon:
                missing.append("multimon-ng")
            self._status = "unavailable"
            self._last_error = f"Missing: {', '.join(missing)}"
            self.log.warning(self._last_error)
            return

        rtl_cmd = [
            rtl_fm,
            "-d", str(device_index),
            "-f", str(self._freq_hz),
            "-s", "22050",
            "-M", "fm",
            "-E", "dc",
            "-A", "fast",
            "-p", str(self._ppm),
        ]
        if self._gain_db is not None:
            rtl_cmd += ["-g", str(self._gain_db)]
        rtl_cmd.append("-")

        multimon_cmd = [multimon, "-t", "raw", "-a", "EAS", "-q", "-"]

        self.log.debug("Launching: %s | %s", " ".join(rtl_cmd), " ".join(multimon_cmd))

        rtl_proc = subprocess.Popen(
            rtl_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._process = subprocess.Popen(
            multimon_cmd,
            stdin=rtl_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._rtl_process = rtl_proc
        if rtl_proc.stdout:
            rtl_proc.stdout.close()
        self._pid = self._process.pid
        self._status = "monitoring"

        self._start_log_reader(self._stderr_fake(rtl_proc), prefix="rtl_fm")
        self._start_thread(self._parser_loop, name="wx-parser")

        self.log.info(
            "Monitoring %.3f MHz for SAME headers (PID %d)",
            self._freq_hz / 1_000_000, self._pid,
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
                if not text:
                    continue
                m = _SAME_RE.search(text)
                if m:
                    self._handle_same_header(m)
        except (ValueError, OSError):
            pass
        except Exception:
            self.log.exception("SAME parser crashed")

    def _handle_same_header(self, m: re.Match) -> None:
        org = m.group("org")
        event_code = m.group("event")
        fips_raw = m.group("fips")
        purge = m.group("purge")
        issued = m.group("issued")
        callsign = m.group("callsign")

        fips_codes = _parse_fips_codes(fips_raw)
        if self._fips_filter:
            if not any(f in self._fips_filter for f in fips_codes):
                return

        event_desc, severity = _EVENT_CODES.get(
            event_code, (f"Unknown ({event_code})", "info"),
        )
        issued_ts = _parse_issued_ts(issued)
        purge_ts = _compute_purge_ts(issued_ts, purge)

        alert = {
            "event_code": event_code,
            "event_desc": event_desc,
            "originator": org,
            "originator_desc": _ORG_NAMES.get(org, org),
            "severity": severity,
            "fips_codes": fips_codes,
            "counties": [_fips_label(f) for f in fips_codes],
            "issued_ts": issued_ts,
            "purge_ts": purge_ts,
            "callsign": callsign,
            "raw_header": m.group(0),
            "received_at": time.time(),
            "expired": False,
        }

        self._alert_history.appendleft(alert)
        self._stats["headers_decoded_total"] += 1
        self._stats["last_header_at"] = time.time()
        counts = self._stats["alerts_by_type"]
        counts[event_code] = counts.get(event_code, 0) + 1

        if self._active_alert is None or _SEVERITY_RANK.get(
            severity, 99,
        ) <= _SEVERITY_RANK.get(
            self._active_alert.get("severity", "info"), 99,
        ):
            self._active_alert = alert

        self._update_snapshot_cache()

        self.log.info(
            "SAME: %s — %s (%s) from %s",
            event_code, event_desc, severity, callsign,
        )

        try:
            self.event_bus.publish(events.WEATHER_ALERT_RECEIVED, alert)
        except Exception:
            pass

        if severity in ("extreme", "severe"):
            try:
                self.event_bus.publish(events.WEATHER_ALERT_SEVERE, alert)
            except Exception:
                pass
            if self._forward_to_alert_system:
                try:
                    self.event_bus.publish(events.ALERT_TRIGGERED, {
                        "source": "weather_alert",
                        "title": event_desc,
                        "message": f"{event_desc} for {', '.join(alert['counties'])}",
                        "severity": severity,
                    })
                except Exception:
                    pass

    def _check_expired(self) -> None:
        now = time.time()
        if self._active_alert:
            purge = self._active_alert.get("purge_ts")
            if purge and now > purge:
                self._active_alert["expired"] = True
                try:
                    self.event_bus.publish(
                        events.WEATHER_ALERT_EXPIRED, self._active_alert,
                    )
                except Exception:
                    pass
                self._active_alert = None

    def _update_snapshot_cache(self) -> None:
        self._check_expired()
        with self._cache_lock:
            self._snapshot_cache = {
                "status": self._status,
                "error": self._last_error,
                "freq_mhz": round(self._freq_hz / 1_000_000, 3),
                "active_alert": dict(self._active_alert) if self._active_alert else None,
                "alert_history": list(self._alert_history),
                "stats": dict(self._stats),
            }

    def get_status(self) -> dict[str, Any]:
        return {
            "active": self._active,
            "status": self._status,
            "error": self._last_error,
            "freq_mhz": round(self._freq_hz / 1_000_000, 3),
            "headers_decoded": self._stats["headers_decoded_total"],
            "active_alert": self._active_alert is not None,
        }
