"""NOAA APT weather satellite image decoder plugin.

Records NOAA 15/18/19 APT transmissions during satellite passes
(triggered by space_tracker pass predictions), then decodes the
audio into weather satellite images.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from reticulumpi import events
from reticulumpi.builtin_plugins.signal_plugin_base import SignalPluginBase
from reticulumpi.sdr_scheduler import PRIORITY_SCHEDULED

_DEFAULT_SATELLITES: dict[str, float] = {
    "NOAA 15": 137.620,
    "NOAA 18": 137.9125,
    "NOAA 19": 137.100,
}

_DATA_DIR = os.path.expanduser("~/.local/share/reticulumpi/noaa_apt")


class NOAAAPTDecoder(SignalPluginBase):
    """NOAA APT satellite image decoder."""

    plugin_name = "noaa_apt_decoder"
    plugin_version = "0.1.0"
    plugin_description = "NOAA APT weather satellite image decoder"
    broadcast_keys = "noaa_apt"

    signal_priority = PRIORITY_SCHEDULED
    signal_continuous = False
    signal_label = "NOAA APT Decoder"

    def validate_config(self) -> None:
        self._gain = self.config.get("gain", 40.0)
        self._ppm = int(self.config.get("ppm", 0))
        self._decoder_bin = str(self.config.get("decoder_bin", "noaa-apt"))
        self._min_elevation = float(self.config.get("min_elevation_deg", 15))
        self._pre_pass_seconds = int(self.config.get("pre_pass_seconds", 30))
        self._post_pass_seconds = int(self.config.get("post_pass_seconds", 30))
        self._retention_days = int(self.config.get("retention_days", 7))
        self._max_images = int(self.config.get("max_images", 50))
        self._max_restarts = int(self.config.get("max_restarts", 3))

        sat_cfg = self.config.get("satellites")
        if isinstance(sat_cfg, dict):
            self._satellites = {str(k): float(v) for k, v in sat_cfg.items()}
        else:
            self._satellites = dict(_DEFAULT_SATELLITES)

        image_dir = self.config.get("image_dir", os.path.join(_DATA_DIR, "images"))
        recording_dir = self.config.get("recording_dir", os.path.join(_DATA_DIR, "recordings"))
        self._image_dir = os.path.expanduser(image_dir)
        self._recording_dir = os.path.expanduser(recording_dir)

    def _on_start(self) -> None:
        self._current_pass: dict[str, Any] | None = None
        self._recent_images: deque[dict[str, Any]] = deque(maxlen=20)
        self._next_passes: list[dict[str, Any]] = []
        self._passes_lock = threading.Lock()
        self._stats: dict[str, Any] = {
            "total_captures": 0,
            "successful_decodes": 0,
            "failed_decodes": 0,
            "last_capture_at": None,
            "success_rate_pct": 0.0,
            "best_pass": None,
        }
        self._status = "idle"
        self._last_error: str | None = None
        self._recording_file: str | None = None
        self._rtl_process: subprocess.Popen | None = None

        os.makedirs(self._image_dir, exist_ok=True)
        os.makedirs(self._recording_dir, exist_ok=True)

        self._load_existing_images()

        self.event_bus.subscribe(
            events.SPACE_PASS_UPCOMING,
            self._on_pass_prediction,
        )

    def _on_stop(self) -> None:
        self.event_bus.unsubscribe(
            events.SPACE_PASS_UPCOMING,
            self._on_pass_prediction,
        )

    def _load_existing_images(self) -> None:
        try:
            files = sorted(
                Path(self._image_dir).glob("*.png"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for f in files[:20]:
                stat = f.stat()
                parts = f.stem.split("_")
                sat_name = parts[0].replace("-", " ") if parts else "Unknown"
                self._recent_images.append(
                    {
                        "satellite": sat_name,
                        "filename": f.name,
                        "file_size_bytes": stat.st_size,
                        "captured_at": stat.st_mtime,
                        "quality": "unknown",
                    }
                )
        except Exception:
            self.log.debug("Could not load existing images", exc_info=True)

    def _on_pass_prediction(self, event_type: str, data: dict[str, Any]) -> None:
        passes = data.get("passes", [])
        noaa_passes: list[dict[str, Any]] = []
        for p in passes:
            name = p.get("name", "")
            if name not in self._satellites:
                continue
            if p.get("max_el", 0) < self._min_elevation:
                continue
            noaa_passes.append(
                {
                    "satellite": name,
                    "freq_mhz": self._satellites[name],
                    "aos_ts": p["aos_ts"],
                    "los_ts": p["los_ts"],
                    "max_el": p.get("max_el", 0),
                    "duration_s": p.get("duration_s", 0),
                }
            )

        new_passes = sorted(noaa_passes, key=lambda x: x["aos_ts"])[:6]
        with self._passes_lock:
            self._next_passes = new_passes

        sched = getattr(self.app, "sdr_scheduler", None)
        if sched is None or not self._dongle_serial:
            return
        sched.remove_windows(self._dongle_serial, self.plugin_name)
        for p in new_passes:
            sched.add_window(
                serial=self._dongle_serial,
                caller=self.plugin_name,
                start_ts=p["aos_ts"] - self._pre_pass_seconds,
                end_ts=p["los_ts"] + self._post_pass_seconds,
                label=f"{p['satellite']} pass ({p['max_el']:.0f}° max el)",
            )

        self._update_snapshot_cache()

    def _launch_subprocess(self, device_index: int) -> None:
        now = time.time()
        current = None
        with self._passes_lock:
            passes_snapshot = list(self._next_passes)
        for p in passes_snapshot:
            window_start = p["aos_ts"] - self._pre_pass_seconds
            window_end = p["los_ts"] + self._post_pass_seconds
            if window_start <= now <= window_end:
                current = p
                break

        if current is None:
            self._status = "idle"
            raise RuntimeError("no current pass")

        rtl_fm = shutil.which("rtl_fm")
        sox = shutil.which("sox")
        if not rtl_fm:
            self._status = "unavailable"
            self._last_error = "rtl_fm not found on PATH"
            return

        freq_hz = int(current["freq_mhz"] * 1_000_000)
        sat_safe = current["satellite"].replace(" ", "-")
        ts_str = time.strftime("%Y%m%d_%H%M", time.gmtime(now))
        wav_name = f"{sat_safe}_{ts_str}.wav"
        wav_path = os.path.join(self._recording_dir, wav_name)

        self._current_pass = {
            **current,
            "recording_file": wav_name,
            "recording_path": wav_path,
            "started_at": now,
        }
        self._recording_file = wav_path
        self._status = "recording"
        self._stats["total_captures"] += 1

        rtl_cmd = [
            rtl_fm,
            "-d",
            str(device_index),
            "-f",
            str(freq_hz),
            "-s",
            "48000",
            "-p",
            str(self._ppm),
            "-E",
            "dc",
            "-F",
            "9",
        ]
        if self._gain is not None:
            rtl_cmd.extend(["-g", str(self._gain)])
        rtl_cmd.append("-")

        if sox:
            sox_cmd = [
                sox,
                "-t",
                "raw",
                "-r",
                "48000",
                "-e",
                "signed",
                "-b",
                "16",
                "-c",
                "1",
                "-",
                wav_path,
            ]
            self.log.debug(
                "Launching: %s | %s",
                " ".join(rtl_cmd),
                " ".join(sox_cmd),
            )
            rtl_proc = subprocess.Popen(
                rtl_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._process = subprocess.Popen(
                sox_cmd,
                stdin=rtl_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            if rtl_proc.stdout:
                rtl_proc.stdout.close()
            self._rtl_process = rtl_proc
        else:
            self.log.error("sox not found — cannot record WAV for APT decode")
            self._status = "error"
            self._last_error = "sox not installed"
            self._update_snapshot_cache()
            return

        self._pid = self._process.pid

        self._start_thread(self._monitor_pass, name="noaa-monitor")

        try:
            self.event_bus.publish(
                events.NOAA_APT_CAPTURE_START,
                {
                    "satellite": current["satellite"],
                    "freq_mhz": current["freq_mhz"],
                    "max_el": current["max_el"],
                },
            )
        except Exception:
            self.log.debug("event publish failed", exc_info=True)

        self.log.info(
            "Recording %s at %.3f MHz (PID %d, file %s)",
            current["satellite"],
            current["freq_mhz"],
            self._pid,
            wav_name,
        )

    def _kill_subprocess(self) -> None:
        rtl = self._rtl_process
        self._rtl_process = None
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
            except (OSError, ProcessLookupError):
                self.log.debug("rtl subprocess cleanup failed", exc_info=True)
            finally:
                for f in (rtl.stdout, rtl.stderr):
                    if f:
                        try:
                            f.close()
                        except OSError:
                            pass

    def _monitor_pass(self) -> None:
        cp = self._current_pass
        if cp is None:
            return
        los_end = cp["los_ts"] + self._post_pass_seconds

        while self._active and self._dongle_active:
            now = time.time()
            if now >= los_end:
                break
            if cp:
                duration = cp["los_ts"] - cp["aos_ts"]
                if duration > 0:
                    elapsed = now - cp["aos_ts"]
                    cp["progress_pct"] = min(100.0, max(0.0, elapsed / duration * 100))
            self._update_snapshot_cache()
            self._sleep_while_active(5.0)

        self._kill_subprocess()

        sched = getattr(self.app, "sdr_scheduler", None)
        if sched and self._dongle_serial:
            sched.dongle_released(self._dongle_serial, self.plugin_name)
        self._dongle_active = False

        self._status = "decoding"
        self._update_snapshot_cache()

        try:
            self.event_bus.publish(
                events.NOAA_APT_CAPTURE_DONE,
                {
                    "satellite": cp.get("satellite"),
                    "recording_file": cp.get("recording_file"),
                },
            )
        except Exception:
            self.log.debug("event publish failed", exc_info=True)

        self._decode_recording(cp)

    def _decode_recording(self, pass_info: dict[str, Any]) -> None:
        wav_path = pass_info.get("recording_path", "")
        if not os.path.exists(wav_path):
            self.log.warning("Recording file not found: %s", wav_path)
            self._status = "idle"
            self._current_pass = None
            self._update_snapshot_cache()
            return

        decoder = shutil.which(self._decoder_bin)
        if not decoder:
            self.log.warning("%s not found; skipping decode", self._decoder_bin)
            self._status = "idle"
            self._current_pass = None
            self._update_snapshot_cache()
            return

        sat_safe = pass_info["satellite"].replace(" ", "-")
        ts_str = time.strftime(
            "%Y%m%d_%H%M",
            time.gmtime(pass_info.get("started_at", time.time())),
        )
        png_name = f"{sat_safe}_{ts_str}.png"
        png_path = os.path.join(self._image_dir, png_name)

        cmd = [decoder, wav_path, "-o", png_path, "--contrast", "auto"]
        self.log.info("Decoding: %s", " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                self.log.warning(
                    "Decode failed (rc=%d): %s", result.returncode, result.stderr[:200]
                )
                self._stats["failed_decodes"] += 1
            elif os.path.exists(png_path) and os.path.getsize(png_path) > 0:
                max_el = pass_info.get("max_el", 0)
                file_size = os.path.getsize(png_path)
                quality_score = 0.3  # base for successful decode
                if file_size > 500_000:
                    quality_score += 0.3
                elif file_size > 200_000:
                    quality_score += 0.2
                elif file_size > 100_000:
                    quality_score += 0.1
                if max_el >= 60:
                    quality_score += 0.4
                elif max_el >= 40:
                    quality_score += 0.3
                elif max_el >= 25:
                    quality_score += 0.2
                else:
                    quality_score += 0.1
                quality_score = round(min(1.0, quality_score), 2)
                quality = (
                    "excellent"
                    if quality_score >= 0.8
                    else "good"
                    if quality_score >= 0.6
                    else "fair"
                    if quality_score >= 0.4
                    else "poor"
                )
                image_meta = {
                    "satellite": pass_info["satellite"],
                    "captured_at": pass_info.get("los_ts", time.time()),
                    "aos_ts": pass_info.get("aos_ts"),
                    "max_el": max_el,
                    "duration_s": pass_info.get("duration_s", 0),
                    "filename": png_name,
                    "file_size_bytes": file_size,
                    "quality": quality,
                    "quality_score": quality_score,
                }
                self._recent_images.appendleft(image_meta)
                self._stats["successful_decodes"] += 1
                self._stats["last_capture_at"] = time.time()
                best = self._stats.get("best_pass")
                if best is None or quality_score > best.get("quality_score", 0):
                    self._stats["best_pass"] = {
                        "satellite": pass_info["satellite"],
                        "max_el": max_el,
                        "quality_score": quality_score,
                    }

                total = self._stats["successful_decodes"] + self._stats["failed_decodes"]
                self._stats["success_rate_pct"] = round(
                    self._stats["successful_decodes"] / max(1, total) * 100,
                    1,
                )

                self.log.info(
                    "Decoded image: %s (%s quality, score %.2f)", png_name, quality, quality_score
                )
                try:
                    self.event_bus.publish(events.NOAA_APT_DECODE_COMPLETE, image_meta)
                except Exception:
                    self.log.debug("event publish failed", exc_info=True)
        except subprocess.TimeoutExpired:
            self.log.warning("Decode timed out for %s", wav_path)
        except Exception:
            self.log.exception("Decode error")

        try:
            os.remove(wav_path)
        except OSError:
            pass

        self._cleanup_old_images()
        self._current_pass = None
        self._status = "idle"
        self._update_snapshot_cache()

    def _cleanup_old_images(self) -> None:
        try:
            files = sorted(
                Path(self._image_dir).glob("*.png"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            now = time.time()
            cutoff = now - self._retention_days * 86400
            keep: list[Path] = []
            for f in files:
                if len(keep) < self._max_images and f.stat().st_mtime >= cutoff:
                    keep.append(f)
                else:
                    f.unlink()
        except Exception:
            self.log.debug("Image cleanup error", exc_info=True)

    def _update_snapshot_cache(self) -> None:
        now = time.time()
        next_passes = []
        with self._passes_lock:
            passes_snapshot = list(self._next_passes)
        for p in passes_snapshot:
            entry = dict(p)
            entry["countdown_s"] = max(0, p["aos_ts"] - now)
            next_passes.append(entry)

        with self._cache_lock:
            self._snapshot_cache = {
                "status": self._status,
                "error": self._last_error,
                "current_pass": dict(self._current_pass) if self._current_pass else None,
                "recent_images": list(self._recent_images),
                "next_passes": next_passes,
                "stats": dict(self._stats),
            }

    def get_status(self) -> dict[str, Any]:
        with self._passes_lock:
            passes = list(self._next_passes)
        return {
            "active": self._active,
            "status": self._status,
            "error": self._last_error,
            "total_captures": self._stats["total_captures"],
            "successful_decodes": self._stats["successful_decodes"],
            "next_pass": passes[0]["satellite"] if passes else None,
        }
