"""LoRa chirp spectrogram viewer — continuous I/Q streaming waterfall.

Extends LoraScanner (which extends SpectrumScanner) so it inherits the
channel analysis and noise-floor estimation.  Instead of the rtl_power
sweep waterfall, this plugin runs ``rtl_sdr`` as a long-lived streaming
process and computes a rolling STFT spectrogram:

  1. Launch ``rtl_sdr`` as a continuous subprocess (no sample limit)
  2. Read I/Q chunks from stdout
  3. Compute windowed FFT → power-spectrum rows
  4. Max-pool to a display row rate (~32 rows/s default)
  5. Push quantized batches to the dashboard via event bus

The result is a scrolling chirp-resolution waterfall that reveals
individual LoRa chirps as diagonal lines — without the user having to
click a capture button.
"""

from __future__ import annotations

import base64
import math
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
    DecodedPacket,
    Detection,
    IQRingBuffer,
    PacketExtractor,
    PacketSymbols,
    PreambleTracker,
    max_packet_samples,
)
from reticulumpi.builtin_plugins.lora_scanner import LoraScanner

_CHIRP_DEFAULTS: dict[str, object] = {
    "chirp_default_freq_mhz": 903.9,
    "chirp_default_sample_rate": 250_000,
    "chirp_fft_size": 256,
    "chirp_fft_size_wide": 2048,
    "chirp_hop_divisor": 4,
    "chirp_continuous": True,
    "chirp_display_rows_per_s": 32,
    "chirp_batch_interval_s": 0.5,
    "chirp_waterfall_depth": 1024,
    "chirp_detection_enabled": True,
    "chirp_detection_bws": [125_000],
    "chirp_detection_sfs": [7, 8, 9, 10, 11, 12],
    "chirp_detection_preamble_len": 8,
    "chirp_detection_snr_threshold_db": 12.0,
    "chirp_detection_snr_margin_db": 10.0,
    "chirp_detection_snr_floor_db": 6.0,
    "chirp_detection_bin_tolerance": 1,
    "chirp_detection_cooldown_s": 0.5,
    "chirp_detection_history_depth": 256,
    "chirp_packet_history_depth": 128,
}

_VALID_SAMPLE_RATES = (250_000, 1_024_000, 2_048_000)
_LORA_BW_HZ = 125_000

_SNAPSHOT_TAIL_ROWS = 32


class LoraChirpViewer(LoraScanner):
    plugin_name = "lora_chirp_viewer"
    plugin_version = "0.2.0"
    plugin_description = "LoRa spectrum scanner with continuous chirp spectrogram waterfall"

    def validate_config(self) -> None:
        for key, default in _CHIRP_DEFAULTS.items():
            self.config.setdefault(key, default)

        self._chirp_freq_mhz = float(self.config["chirp_default_freq_mhz"])
        self._chirp_sr = int(self.config["chirp_default_sample_rate"])
        self._chirp_fft_narrow = int(self.config["chirp_fft_size"])
        self._chirp_fft_wide = int(self.config["chirp_fft_size_wide"])
        self._chirp_hop_div = int(self.config["chirp_hop_divisor"])
        self._continuous_enabled = bool(self.config["chirp_continuous"])
        self._display_rows_per_s = int(self.config["chirp_display_rows_per_s"])
        self._batch_interval = float(self.config["chirp_batch_interval_s"])
        self._chirp_wf_depth = int(self.config["chirp_waterfall_depth"])

        self._detection_enabled = bool(self.config["chirp_detection_enabled"])
        raw_bws = self.config["chirp_detection_bws"]
        if not isinstance(raw_bws, list):
            raw_bws = [raw_bws]
        self._detection_bws: list[int] = [int(b) for b in raw_bws]
        self._detection_sfs = [int(s) for s in self.config["chirp_detection_sfs"]]
        self._detection_preamble_len = int(self.config["chirp_detection_preamble_len"])
        self._detection_snr_threshold_db = float(self.config["chirp_detection_snr_threshold_db"])
        self._detection_snr_margin_db = float(self.config["chirp_detection_snr_margin_db"])
        self._detection_snr_floor_db = float(self.config["chirp_detection_snr_floor_db"])
        self._detection_bin_tolerance = int(self.config["chirp_detection_bin_tolerance"])
        self._detection_cooldown_s = float(self.config["chirp_detection_cooldown_s"])
        self._detection_history_depth = int(self.config["chirp_detection_history_depth"])
        self._packet_history_depth = int(self.config["chirp_packet_history_depth"])

        super().validate_config()

    def start(self) -> None:
        super().start()

        if self._continuous_enabled:
            self._device_released = True
            self._terminate_process()

        self._rtl_sdr_path = shutil.which("rtl_sdr")
        self._stream_process: subprocess.Popen | None = None
        self._stream_active = False
        self._stream_thread: threading.Thread | None = None
        self._stream_lock = threading.Lock()
        self._chirp_status = "idle"

        # Rolling waterfall of (timestamp, dB-row) tuples
        self._chirp_waterfall: deque[tuple[float, list[float]]] = deque(
            maxlen=self._chirp_wf_depth
        )
        self._chirp_sweep_count = 0

        # Running dB scale for quantisation (EMA-smoothed)
        self._db_lo: float | None = None
        self._db_hi: float | None = None

        # Precompute STFT window
        fft_size = self._fft_size_for_sr(self._chirp_sr)
        self._window = np.hanning(fft_size).astype(np.float32)

        # Chirp detection (dechirp-based preamble finder)
        self._trackers: list[tuple[int, PreambleTracker, PacketExtractor]] = []
        self._detection_history: deque[dict[str, Any]] = deque(
            maxlen=self._detection_history_depth,
        )
        self._packet_history: deque[dict[str, Any]] = deque(
            maxlen=self._packet_history_depth,
        )
        self._last_detection_ts: dict[int, float] = {}
        if self._detection_enabled:
            self._init_detection()

        if self._continuous_enabled:
            self._start_continuous()

        self.log.info(
            "Chirp viewer ready (rtl_sdr=%s, continuous=%s, %.3f MHz / %d Hz)",
            self._rtl_sdr_path or "NOT FOUND",
            self._continuous_enabled,
            self._chirp_freq_mhz,
            self._chirp_sr,
        )

    def stop(self) -> None:
        self._stream_active = False
        self._stop_stream()
        old = self._stream_thread
        if old is not None:
            old.join(timeout=8)
            self._stream_thread = None
        super().stop()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _fft_size_for_sr(self, sr: int) -> int:
        return self._chirp_fft_wide if sr > 500_000 else self._chirp_fft_narrow

    def _init_detection(self) -> None:
        self._trackers: list[tuple[int, PreambleTracker, PacketExtractor]] = []
        self._channel_filters: dict[int, ChannelFilter] = {}
        for bw in self._detection_bws:
            chirp_ref = ChirpReference(self._chirp_sr, bw)
            tracker = PreambleTracker(
                chirp_ref=chirp_ref,
                sfs=self._detection_sfs,
                preamble_len=self._detection_preamble_len,
                bin_tolerance=self._detection_bin_tolerance,
                snr_threshold_db=self._detection_snr_threshold_db,
                snr_margin_db=self._detection_snr_margin_db,
                snr_floor_db=self._detection_snr_floor_db,
            )
            extractor = PacketExtractor(chirp_ref)
            self._trackers.append((bw, tracker, extractor))
            self._channel_filters[bw] = ChannelFilter(self._chirp_sr, bw)
        max_sf = max(self._detection_sfs)
        min_bw = min(self._detection_bws)
        capacity = max_packet_samples(
            max_sf, self._chirp_sr, bw=min_bw,
        )
        capacity = max(capacity, self._chirp_sr * 2)
        self._iq_ring_buffer = IQRingBuffer(capacity)
        self._pending_extractions: deque[tuple[Detection, PacketExtractor]] = deque(maxlen=32)

    def _handle_detection(self, det: Detection) -> None:
        last = self._last_detection_ts.get(det.sf, 0.0)
        if det.timestamp - last < self._detection_cooldown_s:
            return
        self._last_detection_ts[det.sf] = det.timestamp

        payload: dict[str, Any] = {
            "timestamp": round(det.timestamp, 3),
            "sf": det.sf,
            "freq_offset_hz": det.freq_offset_hz,
            "freq_offset_bin": det.freq_offset_bin,
            "snr_db": det.snr_db,
            "freq_center_hz": int(self._chirp_freq_mhz * 1e6),
            "sample_rate": self._chirp_sr,
            "detection_bw": det.bw,
        }

        with self._state_lock:
            self._detection_history.append(payload)

        self.log.info(
            "LoRa preamble: SF%d, offset=%.1f Hz, SNR=%.1f dB",
            det.sf, det.freq_offset_hz, det.snr_db,
        )

        try:
            self.event_bus.publish(events.CHIRP_DETECTION, payload)
        except Exception:
            self.log.debug("chirp detection publish failed", exc_info=True)

    def _run_detection(self, raw_bytes: bytes, timestamp: float) -> None:
        if not self._trackers:
            return
        raw = np.frombuffer(raw_bytes, dtype=np.uint8)
        iq = (raw[0::2].astype(np.float32) - 127.5) + \
             1j * (raw[1::2].astype(np.float32) - 127.5)

        self._iq_ring_buffer.write(iq)

        for _bw, tracker, extractor in self._trackers:
            filt = self._channel_filters.get(_bw)
            filtered = filt.apply(iq) if filt is not None else iq
            detections = tracker.feed_chunk(filtered, timestamp)
            for det in detections:
                self._handle_detection(det)
                self._pending_extractions.append((det, extractor))

        self._process_pending_extractions()

    def _process_pending_extractions(self) -> None:
        if not self._pending_extractions:
            return
        oldest, _ = self._iq_ring_buffer.available_range()
        remaining: deque[tuple[Detection, PacketExtractor]] = deque(
            maxlen=self._pending_extractions.maxlen,
        )
        for det, extractor in self._pending_extractions:
            if det.sample_offset < oldest:
                continue
            pkt = extractor.try_extract(
                det, self._iq_ring_buffer, self._detection_preamble_len,
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

    def get_capture_status(self) -> dict[str, Any]:
        with self._state_lock:
            det_count = len(self._detection_history)
        return {
            "state": self._chirp_status,
            "streaming": self._stream_active,
            "freq_mhz": self._chirp_freq_mhz,
            "sample_rate": self._chirp_sr,
            "fft_size": self._fft_size_for_sr(self._chirp_sr),
            "display_rows_per_s": self._display_rows_per_s,
            "waterfall_count": len(self._chirp_waterfall),
            "detection_enabled": self._detection_enabled,
            "detection_count": det_count,
        }

    def get_snapshot(self) -> dict[str, Any]:
        snap = super().get_snapshot()
        snap["chirp_status"] = self.get_capture_status()

        with self._state_lock:
            tail = list(self._chirp_waterfall)[-_SNAPSHOT_TAIL_ROWS:]
        snap["chirp_detection"] = self.get_detection_stats()
        snap["chirp_waterfall_tail"] = {
            "rows": [
                [round(v, 1) if v is not None else None for v in row]
                for _, row in tail
            ],
            "timestamps": [round(ts, 3) for ts, _ in tail],
            "sweep_count": self._chirp_sweep_count,
            "cols": self._fft_size_for_sr(self._chirp_sr),
            "freq_center_hz": int(self._chirp_freq_mhz * 1e6),
            "sample_rate": self._chirp_sr,
            "db_min": round(self._db_lo, 1) if self._db_lo is not None else -90,
            "db_max": round(self._db_hi, 1) if self._db_hi is not None else -30,
        }
        return snap

    def get_chirp_waterfall_history(self) -> dict[str, Any]:
        with self._state_lock:
            entries = list(self._chirp_waterfall)
        fft_size = self._fft_size_for_sr(self._chirp_sr)
        hop = max(1, fft_size // self._chirp_hop_div)
        time_res_ms = round((hop / self._chirp_sr) * 1000, 4)
        pool_factor = max(1, int(
            (self._chirp_sr / hop) / self._display_rows_per_s
        ))
        display_time_res_ms = round(time_res_ms * pool_factor, 4)
        return {
            "available": True,
            "sweep_count": self._chirp_sweep_count,
            "cols": fft_size,
            "waterfall_depth": self._chirp_wf_depth,
            "freq_center_hz": int(self._chirp_freq_mhz * 1e6),
            "sample_rate": self._chirp_sr,
            "fft_size": fft_size,
            "time_res_ms": display_time_res_ms,
            "freq_res_hz": round(self._chirp_sr / fft_size, 2),
            "db_min": round(self._db_lo, 1) if self._db_lo is not None else -90,
            "db_max": round(self._db_hi, 1) if self._db_hi is not None else -30,
            "rows": [
                [round(v, 1) for v in row]
                for _, row in entries
            ],
            "row_timestamps": [round(ts, 3) for ts, _ in entries],
            "streaming": self._stream_active,
            "detection_bws": self._detection_bws,
            "sf_slopes": {
                bw: _compute_sf_slopes(
                    self._chirp_sr, fft_size, hop, pool_factor, bw=bw,
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

    def set_continuous_params(
        self,
        freq_mhz: float | None = None,
        sample_rate: int | None = None,
    ) -> None:
        restart = False
        if freq_mhz is not None:
            self._chirp_freq_mhz = freq_mhz
            restart = True
        if sample_rate is not None:
            if sample_rate not in _VALID_SAMPLE_RATES:
                raise ValueError(f"sample_rate must be one of {_VALID_SAMPLE_RATES}")
            self._chirp_sr = sample_rate
            fft_size = self._fft_size_for_sr(sample_rate)
            self._window = np.hanning(fft_size).astype(np.float32)
            restart = True

        if restart:
            if self._detection_enabled:
                self._init_detection()
            if self._stream_active:
                self._restart_continuous()

    # ------------------------------------------------------------------
    # Continuous streaming
    # ------------------------------------------------------------------

    def _start_continuous(self) -> None:
        if not self._rtl_sdr_path:
            self._chirp_status = "unavailable"
            self.log.warning("rtl_sdr not found; continuous chirp capture disabled")
            return
        self._stream_active = True
        self._chirp_status = "starting"
        self._stream_thread = self._start_thread(self._continuous_loop, name="chirp-stream")

    def _restart_continuous(self) -> None:
        self._stream_active = False
        self._stop_stream()
        old = self._stream_thread
        if old is not None:
            old.join(timeout=8)
            self._stream_thread = None
        self._db_lo = None
        self._db_hi = None
        self._chirp_waterfall.clear()
        self._chirp_sweep_count = 0
        for _bw, tracker, _ext in self._trackers:
            tracker.reset()
        if hasattr(self, "_iq_ring_buffer"):
            self._iq_ring_buffer.reset()
            self._pending_extractions.clear()
        if self._active:
            self._start_continuous()

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

    def _continuous_loop(self) -> None:
        restart_count = 0
        max_restarts = 5

        while self._active and self._stream_active:
            try:
                self._run_stream()
            except Exception:
                self.log.exception("Chirp stream crashed")

            if not self._active or not self._stream_active:
                break

            restart_count += 1
            if restart_count > max_restarts:
                self._chirp_status = "error"
                self.log.error("Chirp stream exceeded max restarts (%d)", max_restarts)
                break

            backoff = min(30.0, 2.0 ** restart_count)
            self._chirp_status = "restarting"
            self.log.info("Restarting chirp stream in %.0fs (attempt %d)", backoff, restart_count)
            self._sleep_while_active(backoff)

        self._stream_active = False

    def _run_stream(self) -> None:
        freq_hz = int(self._chirp_freq_mhz * 1e6)
        sr = self._chirp_sr
        fft_size = self._fft_size_for_sr(sr)
        hop = max(1, fft_size // self._chirp_hop_div)
        window = np.hanning(fft_size).astype(np.float32)

        cmd = [self._rtl_sdr_path, "-f", str(freq_hz), "-s", str(sr)]
        if self._gain_db is not None:
            cmd += ["-g", f"{self._gain_db:.1f}"]
        cmd += ["-d", str(self._device_index), "-"]
        self.log.info("Starting chirp stream: %s", " ".join(cmd))

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        with self._stream_lock:
            self._stream_process = proc

        self._chirp_status = "streaming"
        self.log.info("Chirp stream started (PID %d): %.3f MHz, %d Hz SR", proc.pid, self._chirp_freq_mhz, sr)

        # How many raw STFT frames to max-pool into one display row
        raw_fps = sr / hop
        pool_factor = max(1, int(raw_fps / self._display_rows_per_s))
        display_rows_per_batch = max(1, int(self._display_rows_per_s * self._batch_interval))
        raw_frames_per_batch = display_rows_per_batch * pool_factor
        # Bytes needed: enough I/Q samples for all frames in one batch
        samples_per_batch = (raw_frames_per_batch - 1) * hop + fft_size
        chunk_bytes = samples_per_batch * 2  # 2 bytes per I/Q sample (uint8 I, uint8 Q)

        time_res_ms = round((hop * pool_factor / sr) * 1000, 4)
        freq_res_hz = round(sr / fft_size, 2)

        self.log.info(
            "Chirp STFT: fft=%d, hop=%d, pool=%dx, %d display rows/batch (%.1f ms/row, %.1f Hz/bin)",
            fft_size, hop, pool_factor, display_rows_per_batch, time_res_ms, freq_res_hz,
        )

        # Overlap buffer: carry fft_size-hop samples from previous chunk
        # so we don't lose frames at chunk boundaries.
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

                # Prepend leftover from previous chunk for STFT continuity
                stft_raw = (leftover + new_raw) if leftover else new_raw

                # Need at least one FFT window
                if len(stft_raw) < fft_size * 2:
                    leftover = stft_raw
                    continue

                # Save overlap for next iteration
                if overlap_bytes > 0 and len(stft_raw) > overlap_bytes:
                    leftover = stft_raw[-overlap_bytes:]
                else:
                    leftover = b""

                now = time.time()
                display_rows = self._process_chunk(
                    stft_raw, fft_size, hop, window, pool_factor,
                )

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
            self._chirp_status = "stopped"

    # ------------------------------------------------------------------
    # STFT chunk processing
    # ------------------------------------------------------------------

    def _process_chunk(
        self,
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

        # Max-pool along time axis to display rate
        if pool_factor > 1 and num_frames >= pool_factor:
            n_pooled = num_frames // pool_factor
            trimmed = spectrogram[:n_pooled * pool_factor]
            pooled = trimmed.reshape(n_pooled, pool_factor, fft_size).max(axis=1)
        else:
            pooled = spectrogram

        return pooled

    def _ingest_rows(self, rows: np.ndarray, base_ts: float) -> None:
        n_rows = rows.shape[0]
        # Update running dB scale (EMA on 5th/95th percentiles)
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

        with self._state_lock:
            for i in range(n_rows):
                ts = base_ts + i * (self._batch_interval / max(n_rows, 1))
                row_list = rows[i].tolist()
                self._chirp_waterfall.append((ts, row_list))
                self._chirp_sweep_count += 1

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
# Helpers (module-level)
# ---------------------------------------------------------------------------

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
