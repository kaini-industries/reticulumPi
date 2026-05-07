"""Always-on LoRa chirp detector using a dedicated RTL-SDR dongle.

Streams raw I/Q from ``rtl_sdr``, feeds the dechirp-based preamble
detector, and optionally computes an STFT waterfall.  Inherits from
PluginBase directly — no sweep/scanner inheritance chain.

When the waterfall is disabled (the default), the STFT is skipped
entirely and raw I/Q flows straight to detection, saving ~30% CPU.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import threading
import time
from collections import deque
from typing import Any

import numpy as np

from reticulumpi import events
from reticulumpi.builtin_plugins.lora_dechirp import (
    ChannelFilter,
    ChirpReference,
    Detection,
    IQRingBuffer,
    PacketExtractor,
    PacketSymbols,
    PreambleTracker,
    max_packet_samples,
)
from reticulumpi.plugin_base import PluginBase

_VALID_SAMPLE_RATES = (250_000, 1_024_000, 2_048_000)
_VALID_DETECTION_BWS = (62_500, 125_000, 250_000, 500_000)
_LORA_BW_HZ = 125_000
_SNAPSHOT_TAIL_ROWS = 32


class ChirpDetector(PluginBase):
    plugin_name = "chirp_detector"
    plugin_version = "0.1.0"
    plugin_description = "Always-on LoRa chirp detector (dedicated RTL-SDR)"
    broadcast_tier = 2
    broadcast_keys = "chirp_detector"

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def validate_config(self) -> None:
        cfg = self.config
        self._freq_mhz = float(cfg.get("freq_mhz", 903.9))
        if not 24.0 <= self._freq_mhz <= 1800.0:
            raise ValueError(f"freq_mhz must be 24-1800 (RTL-SDR range), got {self._freq_mhz}")
        sr = int(cfg.get("sample_rate", 250_000))
        if sr not in _VALID_SAMPLE_RATES:
            raise ValueError(f"sample_rate must be one of {_VALID_SAMPLE_RATES}, got {sr}")
        self._sample_rate = sr

        self._gain_db: float | None = None
        g = cfg.get("gain_db")
        if g is not None:
            g = float(g)
            if not 0 <= g <= 50:
                raise ValueError(f"gain_db must be 0-50, got {g}")
            self._gain_db = g

        self._ppm = int(cfg.get("ppm", 0))
        self._device_id = str(cfg.get("device_serial") or cfg.get("device_index", "0"))
        self._max_restarts = int(cfg.get("max_restarts", 5))

        self._fft_narrow = int(cfg.get("fft_size", 256))
        self._fft_wide = int(cfg.get("fft_size_wide", 2048))
        self._hop_divisor = int(cfg.get("hop_divisor", 4))

        self._waterfall_enabled = bool(cfg.get("waterfall_enabled", False))
        self._waterfall_depth = int(cfg.get("waterfall_depth", 1024))
        self._display_rows_per_s = int(cfg.get("display_rows_per_s", 32))
        self._batch_interval = float(cfg.get("batch_interval_s", 0.5))

        self._detection_enabled = bool(cfg.get("detection_enabled", True))
        bws = cfg.get("detection_bws", [125_000])
        for b in bws:
            if int(b) not in _VALID_DETECTION_BWS:
                raise ValueError(f"detection BW must be one of {_VALID_DETECTION_BWS}, got {b}")
        self._detection_bws: list[int] = [int(b) for b in bws]
        sfs = cfg.get("detection_sfs", [7, 8, 9, 10, 11, 12])
        for s in sfs:
            if int(s) not in range(7, 13):
                raise ValueError(f"detection SF must be 7-12, got {s}")
        self._detection_sfs: list[int] = [int(s) for s in sfs]
        self._preamble_len = int(cfg.get("detection_preamble_len", 8))
        self._snr_threshold_db = float(cfg.get("detection_snr_threshold_db", 12.0))
        self._snr_margin_db = float(cfg.get("detection_snr_margin_db", 10.0))
        self._snr_floor_db = float(cfg.get("detection_snr_floor_db", 6.0))
        self._bin_tolerance = int(cfg.get("detection_bin_tolerance", 1))
        self._cooldown_s = float(cfg.get("detection_cooldown_s", 0.5))
        self._detection_history_depth = int(cfg.get("detection_history_depth", 256))
        self._packet_history_depth = int(cfg.get("packet_history_depth", 128))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._active = True
        self._state_lock = threading.Lock()
        self._stream_lock = threading.Lock()
        self._resolved_index: int | None = None

        self._rtl_sdr_path = shutil.which("rtl_sdr")
        self._stream_process: subprocess.Popen | None = None
        self._stream_active = False
        self._stream_thread: threading.Thread | None = None
        self._restarting = False
        self._status = "idle"

        self._waterfall: deque[tuple[float, list[float]]] = deque(
            maxlen=self._waterfall_depth,
        )
        self._sweep_count = 0
        self._db_lo: float | None = None
        self._db_hi: float | None = None

        fft_size = self._fft_size_for_sr(self._sample_rate)
        self._window = np.hanning(fft_size).astype(np.float32)

        self._trackers: list[tuple[int, PreambleTracker, PacketExtractor]] = []
        self._channel_filters: dict[int, ChannelFilter] = {}
        self._detection_history: deque[dict[str, Any]] = deque(
            maxlen=self._detection_history_depth,
        )
        self._packet_history: deque[dict[str, Any]] = deque(
            maxlen=self._packet_history_depth,
        )
        self._last_detection_ts: dict[int, float] = {}
        self._iq_ring_buffer: IQRingBuffer | None = None
        self._pending_extractions: deque[tuple[Detection, PacketExtractor]] = deque(maxlen=32)

        if self._detection_enabled:
            self._init_detection()

        self._snapshot_cache: tuple[int, dict[str, Any]] | None = None

        try:
            from reticulumpi.rtlsdr import resolve_device
            self._resolved_index = resolve_device(self._device_id, caller=self.plugin_name)
        except Exception as exc:
            self.log.warning("Device resolution failed: %s", exc)

        self._start_stream()

        self.log.info(
            "Chirp detector ready (rtl_sdr=%s, waterfall=%s, detection=%s, %.3f MHz / %d Hz)",
            self._rtl_sdr_path or "NOT FOUND",
            self._waterfall_enabled,
            self._detection_enabled,
            self._freq_mhz,
            self._sample_rate,
        )

    def stop(self) -> None:
        self._active = False
        self._stream_active = False
        self._stop_stream()
        old = self._stream_thread
        if old is not None:
            old.join(timeout=8)
            self._remove_thread(old)
            self._stream_thread = None
        try:
            from reticulumpi.rtlsdr import release_device
            release_device(self._device_id, caller=self.plugin_name)
        except Exception:
            pass
        self._join_threads()

    # ------------------------------------------------------------------
    # Detection init
    # ------------------------------------------------------------------

    def _fft_size_for_sr(self, sr: int) -> int:
        return self._fft_wide if sr > 500_000 else self._fft_narrow

    def _init_detection(self) -> None:
        self._trackers = []
        self._channel_filters = {}
        for bw in self._detection_bws:
            chirp_ref = ChirpReference(self._sample_rate, bw)
            tracker = PreambleTracker(
                chirp_ref=chirp_ref,
                sfs=self._detection_sfs,
                preamble_len=self._preamble_len,
                bin_tolerance=self._bin_tolerance,
                snr_threshold_db=self._snr_threshold_db,
                snr_margin_db=self._snr_margin_db,
                snr_floor_db=self._snr_floor_db,
            )
            extractor = PacketExtractor(chirp_ref)
            self._trackers.append((bw, tracker, extractor))
            self._channel_filters[bw] = ChannelFilter(self._sample_rate, bw)

        max_sf = max(self._detection_sfs)
        min_bw = min(self._detection_bws)
        capacity = max(
            max_packet_samples(max_sf, self._sample_rate, bw=min_bw),
            self._sample_rate * 2,
        )
        self._iq_ring_buffer = IQRingBuffer(capacity)
        self._pending_extractions = deque(maxlen=32)

    # ------------------------------------------------------------------
    # Detection pipeline
    # ------------------------------------------------------------------

    def _handle_detection(self, det: Detection) -> None:
        last = self._last_detection_ts.get(det.sf, 0.0)
        if det.timestamp - last < self._cooldown_s:
            return
        self._last_detection_ts[det.sf] = det.timestamp

        payload: dict[str, Any] = {
            "timestamp": round(det.timestamp, 3),
            "sf": det.sf,
            "freq_offset_hz": det.freq_offset_hz,
            "freq_offset_bin": det.freq_offset_bin,
            "snr_db": det.snr_db,
            "freq_center_hz": int(self._freq_mhz * 1e6),
            "sample_rate": self._sample_rate,
            "detection_bw": det.bw,
            "confidence": det.confidence,
            "noise_floor_db": det.noise_floor_db,
            "threshold_db": det.threshold_db,
        }

        with self._state_lock:
            self._detection_history.append(payload)

        self.log.info(
            "LoRa preamble: SF%d BW=%dk, offset=%.1f Hz, SNR=%.1f dB",
            det.sf, det.bw // 1000, det.freq_offset_hz, det.snr_db,
        )

        try:
            self.event_bus.publish(events.CHIRP_DETECTION, payload)
        except Exception:
            self.log.debug("chirp detection publish failed", exc_info=True)

    def _run_detection(self, raw_bytes: bytes, timestamp: float) -> None:
        with self._state_lock:
            trackers = list(self._trackers)
            filters = dict(self._channel_filters)
            ring = self._iq_ring_buffer
        if not trackers or ring is None:
            return
        raw = np.frombuffer(raw_bytes, dtype=np.uint8)
        iq = (raw[0::2].astype(np.float32) - 127.5) + \
             1j * (raw[1::2].astype(np.float32) - 127.5)

        ring.write(iq)

        for bw, tracker, extractor in trackers:
            filt = filters.get(bw)
            filtered = filt.apply(iq) if filt is not None else iq
            detections = tracker.feed_chunk(filtered, timestamp)
            for det in detections:
                self._handle_detection(det)
                self._pending_extractions.append((det, extractor))

        self._process_pending_extractions()

    def _process_pending_extractions(self) -> None:
        if not self._pending_extractions or self._iq_ring_buffer is None:
            return
        oldest, _ = self._iq_ring_buffer.available_range()
        remaining: deque[tuple[Detection, PacketExtractor]] = deque(
            maxlen=self._pending_extractions.maxlen,
        )
        for det, extractor in self._pending_extractions:
            if det.sample_offset < oldest:
                continue
            pkt = extractor.try_extract(
                det, self._iq_ring_buffer, self._preamble_len,
            )
            if pkt is not None:
                self._handle_packet(pkt)
            else:
                remaining.append((det, extractor))
        self._pending_extractions = remaining

    def _handle_packet(self, pkt: PacketSymbols) -> None:
        try:
            decoded = PacketExtractor.decode(pkt)
        except Exception:
            self.log.debug("packet decode failed", exc_info=True)
            return

        payload_hex = decoded.payload.hex() if decoded.payload else ""
        payload: dict[str, Any] = {
            "timestamp": round(pkt.detection.timestamp, 3),
            "sf": pkt.detection.sf,
            "snr_db": pkt.detection.snr_db,
            "sync_word": pkt.sync_word,
            "payload_len": decoded.payload_len,
            "cr": decoded.cr,
            "has_crc": decoded.has_crc,
            "crc_ok": decoded.crc_ok,
            "header_ok": decoded.header_ok,
            "errors_corrected": decoded.errors_corrected,
            "payload_hex": payload_hex,
            "detection_bw": pkt.detection.bw,
        }

        with self._state_lock:
            self._packet_history.append(payload)

        self.log.info(
            "LoRa packet: SF%d CR4/%d len=%d crc=%s [%s]",
            decoded.detection.sf,
            4 + decoded.cr,
            decoded.payload_len,
            decoded.crc_ok,
            payload_hex[:32] + ("..." if len(payload_hex) > 32 else ""),
        )

        try:
            self.event_bus.publish(events.CHIRP_PACKET_DECODED, payload)
        except Exception:
            self.log.debug("packet decoded publish failed", exc_info=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_snapshot(self) -> dict[str, Any]:
        cached = self._snapshot_cache
        if cached is not None and cached[0] == self._sweep_count:
            return cached[1]

        nf_db = -120.0
        if self._trackers:
            nf_vals = [t.noise_floor_db() for _, t, _ in self._trackers]
            nf_db = min(nf_vals) if nf_vals else -120.0

        with self._state_lock:
            det_count = len(self._detection_history)
            tail = list(self._waterfall)[-_SNAPSHOT_TAIL_ROWS:]

        fft_size = self._fft_size_for_sr(self._sample_rate)
        snap: dict[str, Any] = {
            "status": self._status,
            "streaming": self._stream_active,
            "waterfall_enabled": self._waterfall_enabled,
            "freq_mhz": self._freq_mhz,
            "sample_rate": self._sample_rate,
            "fft_size": fft_size,
            "detection_enabled": self._detection_enabled,
            "detection_count": det_count,
            "detection_sfs": list(self._detection_sfs),
            "detection_bws": list(self._detection_bws),
            "noise_floor_db": nf_db,
            "detection": self.get_detection_stats(),
        }

        if self._waterfall_enabled and tail:
            snap["waterfall_tail"] = {
                "rows": [list(row) for _, row in tail],
                "timestamps": [round(ts, 3) for ts, _ in tail],
                "sweep_count": self._sweep_count,
                "cols": fft_size,
                "freq_center_hz": int(self._freq_mhz * 1e6),
                "sample_rate": self._sample_rate,
                "db_min": round(self._db_lo, 1) if self._db_lo is not None else -90,
                "db_max": round(self._db_hi, 1) if self._db_hi is not None else -30,
            }

        self._snapshot_cache = (self._sweep_count, snap)
        return snap

    def get_waterfall_history(self) -> dict[str, Any]:
        if not self._waterfall_enabled:
            return {"available": False, "waterfall_enabled": False}

        with self._state_lock:
            entries = list(self._waterfall)

        fft_size = self._fft_size_for_sr(self._sample_rate)
        hop = max(1, fft_size // self._hop_divisor)
        pool_factor = max(1, int((self._sample_rate / hop) / self._display_rows_per_s))
        time_res_ms = round((hop * pool_factor / self._sample_rate) * 1000, 4)

        return {
            "available": True,
            "waterfall_enabled": True,
            "sweep_count": self._sweep_count,
            "cols": fft_size,
            "waterfall_depth": self._waterfall_depth,
            "freq_center_hz": int(self._freq_mhz * 1e6),
            "sample_rate": self._sample_rate,
            "fft_size": fft_size,
            "time_res_ms": time_res_ms,
            "freq_res_hz": round(self._sample_rate / fft_size, 2),
            "db_min": round(self._db_lo, 1) if self._db_lo is not None else -90,
            "db_max": round(self._db_hi, 1) if self._db_hi is not None else -30,
            "rows": [[round(v, 1) for v in row] for _, row in entries],
            "row_timestamps": [round(ts, 3) for ts, _ in entries],
            "streaming": self._stream_active,
            "detection_bws": self._detection_bws,
            "sf_slopes": {
                bw: _compute_sf_slopes(
                    self._sample_rate, fft_size, hop, pool_factor, bw=bw,
                )
                for bw in self._detection_bws
            },
        }

    def get_detection_history(self) -> list[dict[str, Any]]:
        with self._state_lock:
            return list(self._detection_history)

    def get_packet_history(self) -> list[dict[str, Any]]:
        with self._state_lock:
            return list(self._packet_history)

    def get_detection_stats(self) -> dict[str, Any]:
        with self._state_lock:
            history = list(self._detection_history)
        by_sf: dict[int, int] = {}
        for d in history:
            sf = d["sf"]
            by_sf[sf] = by_sf.get(sf, 0) + 1
        return {
            "enabled": self._detection_enabled,
            "total": len(history),
            "by_sf": by_sf,
        }

    def get_detection_params(self) -> dict[str, Any]:
        return {
            "detection_enabled": self._detection_enabled,
            "detection_sfs": list(self._detection_sfs),
            "detection_bws": list(self._detection_bws),
            "detection_snr_threshold_db": self._snr_threshold_db,
            "detection_preamble_len": self._preamble_len,
            "sample_rate": self._sample_rate,
        }

    def set_waterfall_enabled(self, enabled: bool) -> None:
        was = self._waterfall_enabled
        self._waterfall_enabled = bool(enabled)
        if was and not enabled:
            with self._state_lock:
                self._waterfall.clear()
                self._sweep_count = 0
            self._db_lo = None
            self._db_hi = None
        self._snapshot_cache = None
        self.log.info("Waterfall %s", "enabled" if enabled else "disabled")

    def set_continuous_params(
        self,
        freq_mhz: float | None = None,
        sample_rate: int | None = None,
    ) -> None:
        restart = False
        if freq_mhz is not None:
            freq_mhz = float(freq_mhz)
            if not 24.0 <= freq_mhz <= 1800.0:
                raise ValueError(f"freq_mhz must be 24-1800 (RTL-SDR range), got {freq_mhz}")
            self._freq_mhz = freq_mhz
            restart = True
        if sample_rate is not None:
            if sample_rate not in _VALID_SAMPLE_RATES:
                raise ValueError(f"sample_rate must be one of {_VALID_SAMPLE_RATES}")
            self._sample_rate = int(sample_rate)
            fft_size = self._fft_size_for_sr(self._sample_rate)
            self._window = np.hanning(fft_size).astype(np.float32)
            restart = True

        if restart:
            if self._detection_enabled:
                self._init_detection()
            if self._stream_active:
                self._restart_stream()

    def set_detection_params(
        self,
        enabled: bool | None = None,
        sfs: list[int] | None = None,
        bws: list[int] | None = None,
        snr_threshold_db: float | None = None,
        preamble_len: int | None = None,
    ) -> None:
        # Validate first (no shared-state mutation).
        if sfs is not None:
            for s in sfs:
                if s not in range(7, 13):
                    raise ValueError(f"SF must be 7-12, got {s}")
        if bws is not None:
            for b in bws:
                if b not in _VALID_DETECTION_BWS:
                    raise ValueError(f"BW must be one of {_VALID_DETECTION_BWS}, got {b}")
                if b >= self._sample_rate:
                    raise ValueError(f"BW {b} must be < sample_rate {self._sample_rate}")
        if preamble_len is not None:
            p = int(preamble_len)
            if p < 4 or p > 32:
                raise ValueError(f"preamble_len must be 4-32, got {p}")

        det_sfs = [int(s) for s in sfs] if sfs is not None else None
        det_bws = [int(b) for b in bws] if bws is not None else None
        det_enabled = bool(enabled) if enabled is not None else None

        # Apply under lock so _run_detection sees a consistent snapshot.
        with self._state_lock:
            if det_sfs is not None:
                self._detection_sfs = det_sfs
            if det_bws is not None:
                self._detection_bws = det_bws
            if snr_threshold_db is not None:
                self._snr_threshold_db = max(0.0, min(40.0, float(snr_threshold_db)))
            if preamble_len is not None:
                self._preamble_len = int(preamble_len)
            if det_enabled is not None:
                self._detection_enabled = det_enabled

            if self._detection_enabled and (not self._detection_sfs or not self._detection_bws):
                raise ValueError("Detection enabled but SFs or BWs empty")

            if self._detection_enabled:
                self._init_detection()
            else:
                self._trackers = []
                self._channel_filters = {}

            self._snapshot_cache = None

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def _start_stream(self) -> None:
        if not self._rtl_sdr_path:
            self._status = "unavailable"
            self.log.warning("rtl_sdr not found; chirp detector disabled")
            return
        self._stream_active = True
        self._status = "starting"
        self._stream_thread = self._start_thread(self._stream_loop, name="chirp-stream")

    def _restart_stream(self) -> None:
        with self._stream_lock:
            if self._restarting:
                return
            self._restarting = True
        try:
            self._stream_active = False
            self._stop_stream()
            old = self._stream_thread
            if old is not None:
                old.join(timeout=8)
                self._remove_thread(old)
                self._stream_thread = None
            self._db_lo = None
            self._db_hi = None
            with self._state_lock:
                self._waterfall.clear()
                self._sweep_count = 0
            for _bw, tracker, _ext in self._trackers:
                tracker.reset()
            if self._iq_ring_buffer is not None:
                self._iq_ring_buffer.reset()
                self._pending_extractions.clear()
            if self._active:
                self._start_stream()
        finally:
            with self._stream_lock:
                self._restarting = False

    def _stop_stream(self) -> None:
        with self._stream_lock:
            proc = self._stream_process
            self._stream_process = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
        except Exception:
            self.log.exception("Error stopping rtl_sdr stream")
        finally:
            if proc.stdout:
                try:
                    proc.stdout.close()
                except Exception:
                    pass

    def _stream_loop(self) -> None:
        restart_count = 0

        while self._active and self._stream_active:
            try:
                self._run_stream()
            except Exception:
                self.log.exception("Chirp stream crashed")

            if not self._active or not self._stream_active:
                break

            restart_count += 1
            if restart_count > self._max_restarts:
                self._status = "error"
                self.log.error("Chirp stream exceeded max restarts (%d)", self._max_restarts)
                break

            backoff = min(30.0, 2.0 ** restart_count)
            self._status = "restarting"
            self._sleep_while_active(backoff)

        self._stream_active = False

    def _run_stream(self) -> None:
        freq_hz = int(self._freq_mhz * 1e6)
        sr = self._sample_rate
        fft_size = self._fft_size_for_sr(sr)
        hop = max(1, fft_size // self._hop_divisor)
        window = np.hanning(fft_size).astype(np.float32)

        cmd = [self._rtl_sdr_path, "-f", str(freq_hz), "-s", str(sr)]
        if self._gain_db is not None:
            cmd += ["-g", f"{self._gain_db:.1f}"]
        if self._ppm:
            cmd += ["-p", str(self._ppm)]
        dev = str(self._resolved_index if self._resolved_index is not None else self._device_id)
        cmd += ["-d", dev, "-"]

        self.log.info("Starting chirp stream: %s", " ".join(cmd))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        with self._stream_lock:
            self._stream_process = proc

        self._status = "streaming"

        raw_fps = sr / hop
        pool_factor = max(1, int(raw_fps / self._display_rows_per_s))
        display_rows_per_batch = max(1, int(self._display_rows_per_s * self._batch_interval))
        raw_frames_per_batch = display_rows_per_batch * pool_factor
        samples_per_batch = (raw_frames_per_batch - 1) * hop + fft_size
        chunk_bytes = samples_per_batch * 2

        time_res_ms = round((hop * pool_factor / sr) * 1000, 4)
        freq_res_hz = round(sr / fft_size, 2)

        overlap_samples = fft_size - hop
        overlap_bytes = overlap_samples * 2
        leftover = b""

        try:
            while self._active and self._stream_active:
                new_raw = proc.stdout.read(chunk_bytes)
                if not new_raw:
                    rc = proc.poll()
                    if rc is not None:
                        self.log.warning("rtl_sdr exited (code %s)", rc)
                    break

                now = time.time()

                if self._waterfall_enabled:
                    stft_raw = (leftover + new_raw) if leftover else new_raw
                    if len(stft_raw) < fft_size * 2:
                        leftover = stft_raw
                    else:
                        if overlap_bytes > 0 and len(stft_raw) > overlap_bytes:
                            leftover = stft_raw[-overlap_bytes:]
                        else:
                            leftover = b""
                        display_rows = _process_chunk(stft_raw, fft_size, hop, window, pool_factor)
                        if display_rows is not None and len(display_rows) > 0:
                            self._ingest_rows(display_rows, now)
                            self._publish_batch(
                                display_rows, now, fft_size, freq_hz, sr,
                                time_res_ms, freq_res_hz, pool_factor, hop,
                            )

                if self._trackers:
                    self._run_detection(new_raw, now)

        except (OSError, ValueError):
            pass
        except Exception:
            self.log.exception("Chirp stream read error")
        finally:
            self._stop_stream()
            self._status = "stopped"

    # ------------------------------------------------------------------
    # Waterfall processing
    # ------------------------------------------------------------------

    def _ingest_rows(self, rows: np.ndarray, base_ts: float) -> None:
        n_rows = rows.shape[0]
        p5 = float(np.percentile(rows, 5))
        p95 = float(np.percentile(rows, 95))
        if p95 - p5 < 3:
            p95 = p5 + 3
        alpha = 0.3
        if self._db_lo is None:
            self._db_lo = p5
            self._db_hi = p95
        else:
            self._db_lo = self._db_lo * (1 - alpha) + p5 * alpha
            self._db_hi = self._db_hi * (1 - alpha) + p95 * alpha

        rounded = np.round(rows, 1)
        with self._state_lock:
            for i in range(n_rows):
                ts = base_ts + i * (self._batch_interval / max(n_rows, 1))
                self._waterfall.append((ts, rounded[i].tolist()))
                self._sweep_count += 1

    def _publish_batch(
        self,
        rows: np.ndarray,
        timestamp: float,
        fft_size: int,
        freq_hz: int,
        sample_rate: int,
        time_res_ms: float,
        freq_res_hz: float,
        pool_factor: int,
        hop: int,
    ) -> None:
        db_lo = self._db_lo if self._db_lo is not None else -90.0
        db_hi = self._db_hi if self._db_hi is not None else -30.0
        rng = db_hi - db_lo
        if rng < 1:
            rng = 1.0

        normed = np.clip((rows - db_lo) / rng, 0.0, 1.0)
        quantized = (normed * 255).astype(np.uint8)

        try:
            self.event_bus.publish(
                events.CHIRP_WATERFALL_ROWS,
                {
                    "rows_b64": base64.b64encode(quantized.tobytes()).decode(),
                    "cols": fft_size,
                    "count": int(quantized.shape[0]),
                    "timestamp": round(timestamp, 3),
                    "freq_center_hz": freq_hz,
                    "sample_rate": sample_rate,
                    "db_min": round(db_lo, 1),
                    "db_max": round(db_hi, 1),
                    "time_res_ms": time_res_ms,
                    "freq_res_hz": freq_res_hz,
                    "detection_bws": self._detection_bws,
                    "sf_slopes": {
                        bw: _compute_sf_slopes(
                            sample_rate, fft_size, hop, pool_factor, bw=bw,
                        )
                        for bw in self._detection_bws
                    },
                },
            )
        except Exception:
            self.log.debug("chirp waterfall publish failed", exc_info=True)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _process_chunk(
    raw_bytes: bytes,
    fft_size: int,
    hop: int,
    window: np.ndarray,
    pool_factor: int,
) -> np.ndarray | None:
    raw = np.frombuffer(raw_bytes, dtype=np.uint8)
    n_samples = len(raw) // 2
    if n_samples < fft_size:
        return None

    iq = (raw[0::2].astype(np.float32) - 127.5) + \
         1j * (raw[1::2].astype(np.float32) - 127.5)

    num_frames = max(1, (len(iq) - fft_size) // hop + 1)
    spectrogram = np.empty((num_frames, fft_size), dtype=np.float32)

    for i in range(num_frames):
        start = i * hop
        frame = iq[start:start + fft_size] * window
        spectrum = np.fft.fftshift(np.fft.fft(frame, n=fft_size))
        spectrogram[i] = 10.0 * np.log10(
            np.real(spectrum * np.conj(spectrum)) + 1e-10
        )

    if pool_factor > 1 and num_frames >= pool_factor:
        n_pooled = num_frames // pool_factor
        trimmed = spectrogram[:n_pooled * pool_factor]
        pooled = trimmed.reshape(n_pooled, pool_factor, fft_size).max(axis=1)
    else:
        pooled = spectrogram

    return pooled


def _compute_sf_slopes(
    sample_rate: int,
    fft_size: int,
    hop: int,
    pool_factor: int = 1,
    bw: int = _LORA_BW_HZ,
) -> list[dict[str, Any]]:
    slopes = []
    for sf in range(7, 13):
        t_symbol = (2 ** sf) / bw
        chirp_rate_hz_per_s = bw / t_symbol
        bins_per_frame = chirp_rate_hz_per_s * (hop * pool_factor / sample_rate) * (fft_size / sample_rate)
        slopes.append({
            "sf": sf,
            "t_symbol_ms": round(t_symbol * 1000, 3),
            "bins_per_frame": round(bins_per_frame, 4),
        })
    return slopes
