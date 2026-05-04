"""LoRa dechirp DSP — preamble detection from raw RTL-SDR IQ.

Implements the core LoRa CSS demodulation math:
  1. Generate reference downchirp per spreading factor
  2. Multiply received IQ by downchirp → collapses chirp to single tone
  3. FFT → peak bin = symbol value
  4. Track consecutive same-bin peaks → preamble detection

Designed to run inline in the chirp viewer's streaming thread at ~500 ms
chunk intervals.  All math is numpy — no scipy or GNU Radio needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

_LORA_BW_HZ = 125_000
_SUPPORTED_SFS = (7, 8, 9, 10, 11, 12)
_DEFAULT_PREAMBLE_LEN = 8
_DEFAULT_BIN_TOLERANCE = 1
_DEFAULT_SNR_THRESHOLD_DB = 12.0


def max_packet_samples(
    sf: int,
    sample_rate: int,
    bw: int = _LORA_BW_HZ,
    cr: int = 4,
    max_payload_len: int = 255,
    preamble_len: int = _DEFAULT_PREAMBLE_LEN,
) -> int:
    """Compute the maximum IQ samples needed for a complete LoRa packet."""
    sym_len = int(round(sample_rate * (2**sf) / bw))
    total_payload_nibs = max_payload_len * 2 + 4  # +4 for CRC
    header_payload_nibs = max(0, sf - 6)
    remaining_nibs = max(0, total_payload_nibs - header_payload_nibs)
    ppm = 4 + cr
    n_blocks = (remaining_nibs + sf - 1) // sf
    n_payload_syms = n_blocks * ppm
    # preamble + 2 sync + 2.25 SFD + 8 header + payload
    total_sym_quarters = (preamble_len + 2 + 8 + n_payload_syms) * 4 + 9
    return (total_sym_quarters * sym_len + 3) // 4


@dataclass
class Detection:
    """A detected LoRa preamble."""

    timestamp: float
    sf: int
    freq_offset_hz: float
    freq_offset_bin: int
    snr_db: float
    sample_offset: int
    bw: int = _LORA_BW_HZ


class ChirpReference:
    """Generates and caches complex downchirp references per SF.

    At 2× oversampled rate (e.g. 250 kHz for 125 kHz BW), the reference
    length is ``2 * 2^SF`` samples.
    """

    def __init__(self, sample_rate: int, bw: int = _LORA_BW_HZ) -> None:
        self.sample_rate = sample_rate
        self.bw = bw
        self._cache: dict[int, np.ndarray] = {}

    def get(self, sf: int) -> np.ndarray:
        ref = self._cache.get(sf)
        if ref is not None:
            return ref
        ref = self._generate(sf)
        self._cache[sf] = ref
        return ref

    def symbol_length(self, sf: int) -> int:
        return int(round(self.sample_rate * (2**sf) / self.bw))

    def invalidate(self) -> None:
        self._cache.clear()

    def _generate(self, sf: int) -> np.ndarray:
        n = self.symbol_length(sf)
        bw = self.bw
        t = np.arange(n, dtype=np.float64) / self.sample_rate
        t_sym = n / self.sample_rate
        # Base upchirp (symbol 0): linear sweep from -BW/2 to +BW/2
        phase = 2.0 * np.pi * (-bw / 2.0 * t + bw / (2.0 * t_sym) * t * t)
        # Downchirp = conjugate of upchirp
        return np.exp(-1j * phase).astype(np.complex64)


class ChannelFilter:
    """FIR lowpass filter at BW/2 for rejecting out-of-band noise.

    Halves the noise bandwidth for a 125 kHz LoRa signal captured at
    250 kHz, yielding ~3 dB SNR improvement.  Kernel is cached per
    (sample_rate, bw) pair.
    """

    _cache: dict[tuple[int, int, int], np.ndarray] = {}

    def __init__(
        self,
        sample_rate: int,
        bw: int = _LORA_BW_HZ,
        num_taps: int = 31,
    ) -> None:
        self.sample_rate = sample_rate
        self.bw = bw
        self.num_taps = num_taps
        key = (sample_rate, bw, num_taps)
        kernel = self._cache.get(key)
        if kernel is None:
            kernel = self._make_kernel(sample_rate, bw, num_taps)
            self._cache[key] = kernel
        self._kernel = kernel

    @staticmethod
    def _make_kernel(sample_rate: int, bw: int, num_taps: int) -> np.ndarray:
        cutoff = bw / (2.0 * sample_rate)
        n = np.arange(num_taps, dtype=np.float64) - (num_taps - 1) / 2.0
        with np.errstate(invalid="ignore"):
            h = np.where(n == 0, 2.0 * cutoff, np.sin(2.0 * np.pi * cutoff * n) / (np.pi * n))
        h *= np.hanning(num_taps)
        h /= np.sum(h)
        return h.astype(np.float32)

    def apply(self, iq: np.ndarray) -> np.ndarray:
        if len(iq) <= self.num_taps:
            return iq
        return np.convolve(iq, self._kernel, mode="same").astype(np.complex64)


class SymbolDetector:
    """Batch-dechirps an IQ chunk into per-symbol peaks for one SF.

    Vectorised: reshapes IQ into a (n_symbols, symbol_length) matrix,
    multiplies all rows by the reference, batch-FFTs, then argmax.
    """

    @staticmethod
    def dechirp(
        iq: np.ndarray,
        sf: int,
        ref: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Dechirp *iq* and return (peak_bins, peak_mags, noise_floors, frac_bins).

        ``frac_bins`` are parabolic-interpolated fractional peak positions
        (float32), giving sub-bin accuracy for CFO estimation.

        Each array has length ``n_symbols = len(iq) // len(ref)``.
        Leftover samples shorter than one symbol are silently dropped.
        """
        sym_len = len(ref)
        n_bins = 2**sf
        n_symbols = len(iq) // sym_len
        if n_symbols == 0:
            empty = np.array([], dtype=np.float32)
            return (
                np.array([], dtype=np.int32),
                empty,
                empty,
                empty,
            )

        usable = iq[: n_symbols * sym_len].reshape(n_symbols, sym_len)
        dechirped = usable * ref
        spectra = np.fft.fft(dechirped, axis=1)
        mag = np.abs(spectra[:, :n_bins])

        peak_bins = np.argmax(mag, axis=1).astype(np.int32)
        idx = np.arange(n_symbols)
        peak_mags = mag[idx, peak_bins].astype(np.float32)

        noise_floors = np.median(mag, axis=1).astype(np.float32)

        alpha = mag[idx, (peak_bins - 1) % n_bins]
        gamma = mag[idx, (peak_bins + 1) % n_bins]
        denom = alpha - 2.0 * peak_mags + gamma
        safe = np.abs(denom) > 1e-10
        delta = np.zeros(n_symbols, dtype=np.float32)
        delta[safe] = (0.5 * (alpha[safe] - gamma[safe]) / denom[safe]).astype(np.float32)
        frac_bins = peak_bins.astype(np.float32) + delta

        return peak_bins, peak_mags, noise_floors, frac_bins


_DEFAULT_SNR_MARGIN_DB = 10.0
_DEFAULT_SNR_FLOOR_DB = 6.0
_DEFAULT_NOISE_EMA_ALPHA = 0.01


@dataclass
class _SfState:
    """Per-SF tracking state inside PreambleTracker."""

    last_bin: int = -1
    count: int = 0
    first_sample_offset: int = 0
    peak_bins: list[float] = field(default_factory=list)
    peak_mags: list[float] = field(default_factory=list)
    noise_floors: list[float] = field(default_factory=list)
    leftover: np.ndarray | None = None
    noise_ema: float = 0.0


class PreambleTracker:
    """Stateful preamble detector that scans all configured SFs.

    Call :meth:`feed_chunk` with each IQ chunk from the streaming loop.
    Detected preambles are returned as :class:`Detection` objects.
    State carries across chunk boundaries so preambles split between
    two chunks are still caught.
    """

    def __init__(
        self,
        chirp_ref: ChirpReference,
        sfs: tuple[int, ...] | list[int] = _SUPPORTED_SFS,
        preamble_len: int = _DEFAULT_PREAMBLE_LEN,
        bin_tolerance: int = _DEFAULT_BIN_TOLERANCE,
        snr_threshold_db: float = _DEFAULT_SNR_THRESHOLD_DB,
        snr_margin_db: float = _DEFAULT_SNR_MARGIN_DB,
        snr_floor_db: float = _DEFAULT_SNR_FLOOR_DB,
        noise_ema_alpha: float = _DEFAULT_NOISE_EMA_ALPHA,
    ) -> None:
        self.chirp_ref = chirp_ref
        self.sfs = tuple(sfs)
        self.preamble_len = preamble_len
        self.bin_tolerance = bin_tolerance
        self.snr_threshold_db = snr_threshold_db
        self.snr_margin_db = snr_margin_db
        self.snr_floor_db = snr_floor_db
        self._noise_ema_alpha = noise_ema_alpha
        self._state: dict[int, _SfState] = {sf: _SfState() for sf in self.sfs}
        self._chunk_sample_offset = 0

    def reset(self) -> None:
        for sf in self.sfs:
            self._state[sf] = _SfState()
        self._chunk_sample_offset = 0

    def feed_chunk(
        self,
        iq: np.ndarray,
        timestamp: float,
    ) -> list[Detection]:
        detections: list[Detection] = []

        for sf in self.sfs:
            dets = self._scan_sf(iq, sf, timestamp)
            detections.extend(dets)

        self._chunk_sample_offset += len(iq)
        return detections

    def _scan_sf(
        self,
        iq: np.ndarray,
        sf: int,
        timestamp: float,
    ) -> list[Detection]:
        st = self._state[sf]
        ref = self.chirp_ref.get(sf)
        sym_len = len(ref)
        n_bins = 2**sf

        # Prepend leftover from previous chunk
        if st.leftover is not None and len(st.leftover) > 0:
            iq_full = np.concatenate([st.leftover, iq])
            prepend_len = len(st.leftover)
        else:
            iq_full = iq
            prepend_len = 0

        # Save new leftover (tail shorter than one symbol)
        remainder = len(iq_full) % sym_len
        if remainder > 0:
            st.leftover = iq_full[-remainder:].copy()
            iq_full = iq_full[:-remainder]
        else:
            st.leftover = None

        if len(iq_full) < sym_len:
            return []

        peak_bins, peak_mags, noise_floors, frac_bins = SymbolDetector.dechirp(
            iq_full, sf, ref,
        )

        detections: list[Detection] = []

        # Update noise floor EMA — use 25th percentile to resist
        # inflation from signal-bearing symbols (SFD, header, payload).
        if len(noise_floors) > 0:
            chunk_noise = float(np.percentile(noise_floors, 25))
            a = self._noise_ema_alpha
            if st.noise_ema < 1e-10:
                st.noise_ema = chunk_noise
            else:
                st.noise_ema = st.noise_ema * (1 - a) + chunk_noise * a

        for i in range(len(peak_bins)):
            b = int(peak_bins[i])
            fb = float(frac_bins[i])
            mag = float(peak_mags[i])
            nf = float(noise_floors[i])

            dist = abs(b - st.last_bin)
            dist = min(dist, n_bins - dist)
            if st.count > 0 and dist <= self.bin_tolerance:
                st.count += 1
                st.peak_bins.append(fb)
                st.peak_mags.append(mag)
                st.noise_floors.append(nf)
            else:
                st.last_bin = b
                st.count = 1
                st.first_sample_offset = (
                    self._chunk_sample_offset - prepend_len + i * sym_len
                )
                st.peak_bins = [fb]
                st.peak_mags = [mag]
                st.noise_floors = [nf]

            if st.count == self.preamble_len:
                det = self._make_detection(sf, st, timestamp)
                if det is not None:
                    detections.append(det)
                st.last_bin = -1
                st.count = 0
                st.peak_bins = []
                st.peak_mags = []
                st.noise_floors = []

        return detections

    def _make_detection(
        self,
        sf: int,
        st: _SfState,
        timestamp: float,
    ) -> Detection | None:
        mean_peak = float(np.mean(st.peak_mags))
        mean_noise = float(np.mean(st.noise_floors))
        if mean_noise < 1e-10:
            mean_noise = 1e-10
        snr_db = 20.0 * np.log10(mean_peak / mean_noise)

        if snr_db < self.snr_floor_db:
            return None

        # Adaptive threshold: when the noise EMA is primed, require the
        # peak to exceed noise_ema by snr_margin_db.  Otherwise fall back
        # to the fixed snr_threshold_db.
        if st.noise_ema > 1e-10:
            adaptive_peak = st.noise_ema * 10.0 ** (self.snr_margin_db / 20.0)
            if mean_peak < adaptive_peak:
                return None
        elif snr_db < self.snr_threshold_db:
            return None

        bw = self.chirp_ref.bw
        n_bins = 2**sf
        freq_offset_hz = st.last_bin * bw / n_bins

        return Detection(
            timestamp=timestamp,
            sf=sf,
            freq_offset_hz=round(freq_offset_hz, 1),
            freq_offset_bin=st.last_bin,
            snr_db=round(snr_db, 1),
            sample_offset=st.first_sample_offset,
            bw=bw,
        )


# ---------------------------------------------------------------------------
# IQ ring buffer — holds recent IQ indexed by absolute stream offset
# ---------------------------------------------------------------------------


class IQRingBuffer:
    """Circular buffer for complex IQ samples indexed by absolute stream offset.

    Capacity defaults to ~2 seconds at 250 kHz (500 000 samples).
    """

    def __init__(self, capacity: int = 500_000) -> None:
        self._buf = np.zeros(capacity, dtype=np.complex64)
        self._capacity = capacity
        self._write_offset = 0

    @property
    def write_offset(self) -> int:
        return self._write_offset

    @property
    def capacity(self) -> int:
        return self._capacity

    def write(self, iq: np.ndarray) -> None:
        n = len(iq)
        if n == 0:
            return
        if n >= self._capacity:
            self._buf[:] = iq[-self._capacity :]
            self._write_offset += n
            return
        pos = self._write_offset % self._capacity
        end = pos + n
        if end <= self._capacity:
            self._buf[pos:end] = iq
        else:
            first = self._capacity - pos
            self._buf[pos:] = iq[:first]
            self._buf[: n - first] = iq[first:]
        self._write_offset += n

    def read(self, start_offset: int, length: int) -> np.ndarray | None:
        """Read *length* samples starting at absolute *start_offset*.

        Returns ``None`` if the requested range has been overwritten or is
        not yet available.
        """
        oldest = max(0, self._write_offset - self._capacity)
        if start_offset < oldest or start_offset + length > self._write_offset:
            return None
        pos = start_offset % self._capacity
        end = pos + length
        if end <= self._capacity:
            return self._buf[pos:end].copy()
        first = self._capacity - pos
        return np.concatenate([self._buf[pos:], self._buf[: length - first]]).copy()

    def available_range(self) -> tuple[int, int]:
        oldest = max(0, self._write_offset - self._capacity)
        return (oldest, self._write_offset)

    def reset(self) -> None:
        self._write_offset = 0
        self._buf[:] = 0


# ---------------------------------------------------------------------------
# Sync-word helpers
# ---------------------------------------------------------------------------


def sync_word_bins(sync_byte: int, sf: int) -> tuple[int, int]:
    """Map a LoRa sync byte to the two chirp-bin values.

    Each nibble of the sync byte is scaled by ``2^(SF-4)``.
    """
    step = 1 << (sf - 4)
    high = ((sync_byte >> 4) & 0xF) * step
    low = (sync_byte & 0xF) * step
    return (high, low)


# ---------------------------------------------------------------------------
# Packet symbol / decoded-packet containers
# ---------------------------------------------------------------------------


@dataclass
class PacketSymbols:
    """Raw dechirped symbols of a detected LoRa packet."""

    detection: Detection
    sync_word: int
    header_symbols: list[int]
    payload_symbols: list[int]
    header_nibbles: list[int]
    payload_len: int
    cr: int
    has_crc: bool
    header_ok: bool


@dataclass
class DecodedPacket:
    """Fully decoded LoRa packet."""

    detection: Detection
    sync_word: int
    payload_len: int
    cr: int
    has_crc: bool
    header_ok: bool
    payload: bytes
    crc_ok: bool | None
    errors_corrected: int


# ---------------------------------------------------------------------------
# Packet extractor — turns a Detection + IQ ring buffer into symbols/bytes
# ---------------------------------------------------------------------------


class PacketExtractor:
    """Extract and decode LoRa packets from buffered IQ.

    After :class:`PreambleTracker` fires a :class:`Detection`, call
    :meth:`try_extract` with the detection and the IQ ring buffer.  If
    enough post-preamble IQ has been buffered it returns a
    :class:`PacketSymbols`; otherwise ``None`` (try again next chunk).

    :meth:`decode` then runs the full PHY pipeline (Gray → de-interleave
    → Hamming FEC → de-whiten → CRC) and returns a :class:`DecodedPacket`.
    """

    _DEFAULT_SYNC_WORDS = (0x34, 0x12, 0x2B)
    _SFD_FACTOR = 9  # 2.25 symbols → 9/4 of sym_len
    _HEADER_CR = 4
    _HEADER_PPM = 8  # 4 + _HEADER_CR

    def __init__(
        self,
        chirp_ref: ChirpReference,
        known_sync_words: tuple[int, ...] | None = None,
        sync_tolerance: int = 1,
    ) -> None:
        self.chirp_ref = chirp_ref
        self.sync_words = known_sync_words or self._DEFAULT_SYNC_WORDS
        self.sync_tolerance = sync_tolerance

    def try_extract(
        self,
        detection: Detection,
        ring_buf: IQRingBuffer,
        preamble_len: int = _DEFAULT_PREAMBLE_LEN,
    ) -> PacketSymbols | None:
        """Try to extract packet symbols from buffered IQ.

        Returns ``None`` when the ring buffer does not yet (or no longer)
        contain enough data for this detection.
        """
        sf = detection.sf
        sym_len = self.chirp_ref.symbol_length(sf)
        ref = self.chirp_ref.get(sf)
        sr = self.chirp_ref.sample_rate
        bw = self.chirp_ref.bw

        post = detection.sample_offset + preamble_len * sym_len

        # Refine CFO from preamble IQ via high-resolution FFT
        cfo_hz = self._estimate_cfo(
            ring_buf, detection.sample_offset, preamble_len, ref, sf, sr,
        )

        # --- Sync word (2 upchirps) ---
        sync_iq = ring_buf.read(post, 2 * sym_len)
        if sync_iq is None:
            return None
        sync_iq = self._compensate_cfo(sync_iq, cfo_hz, sr, post)
        sync_bins, _, _, _ = SymbolDetector.dechirp(sync_iq, sf, ref)
        if len(sync_bins) < 2:
            return None
        sync_byte = self._match_sync_word(
            int(sync_bins[0]), int(sync_bins[1]), sf,
        )
        if sync_byte is None:
            return None

        # --- Skip SFD (2.25 downchirps) ---
        sfd_len = sym_len * self._SFD_FACTOR // 4

        # --- Header block (8 symbols, CR=4, reduced rate) ---
        hdr_start = post + 2 * sym_len + sfd_len
        hdr_iq = ring_buf.read(hdr_start, self._HEADER_PPM * sym_len)
        if hdr_iq is None:
            return None
        hdr_iq = self._compensate_cfo(hdr_iq, cfo_hz, sr, hdr_start)
        hdr_bins, _, _, _ = SymbolDetector.dechirp(hdr_iq, sf, ref)
        if len(hdr_bins) < self._HEADER_PPM:
            return None
        header_symbols = [int(b) for b in hdr_bins[: self._HEADER_PPM]]

        header_nibbles, header_ok = self._decode_header_block(header_symbols, sf)
        if header_nibbles is None:
            return None

        payload_len = (header_nibbles[1] << 4) | header_nibbles[0]
        cr = (header_nibbles[2] >> 1) & 0x7
        has_crc = bool(header_nibbles[2] & 1)
        if cr < 1 or cr > 4:
            return None
        if payload_len > 255:
            return None

        # --- Payload symbol blocks ---
        total_payload_nibs = payload_len * 2 + (4 if has_crc else 0)
        header_payload_nibs = max(0, sf - 6)
        remaining_nibs = total_payload_nibs - header_payload_nibs

        payload_symbols: list[int] = []
        if remaining_nibs > 0:
            ppm = 4 + cr
            n_blocks = (remaining_nibs + sf - 1) // sf
            n_syms = n_blocks * ppm
            pay_start = hdr_start + self._HEADER_PPM * sym_len
            pay_iq = ring_buf.read(pay_start, n_syms * sym_len)
            if pay_iq is None:
                return None
            pay_iq = self._compensate_cfo(pay_iq, cfo_hz, sr, pay_start)
            pay_bins, _, _, _ = SymbolDetector.dechirp(pay_iq, sf, ref)
            payload_symbols = [int(b) for b in pay_bins]

        return PacketSymbols(
            detection=detection,
            sync_word=sync_byte,
            header_symbols=header_symbols,
            payload_symbols=payload_symbols,
            header_nibbles=list(header_nibbles),
            payload_len=payload_len,
            cr=cr,
            has_crc=has_crc,
            header_ok=header_ok,
        )

    @staticmethod
    def _estimate_cfo(
        ring_buf: IQRingBuffer,
        sample_offset: int,
        preamble_len: int,
        ref: np.ndarray,
        sf: int,
        sample_rate: int,
    ) -> float:
        """Estimate carrier frequency offset from the preamble IQ.

        Dechirps the full preamble and uses a long FFT across all symbols
        for sub-bin frequency resolution.
        """
        sym_len = len(ref)
        preamble_iq = ring_buf.read(sample_offset, preamble_len * sym_len)
        if preamble_iq is None:
            return 0.0
        dechirped = preamble_iq * np.tile(ref, preamble_len)
        n_fft = len(dechirped)
        spectrum = np.abs(np.fft.fft(dechirped))
        half = n_fft // 2
        peak = int(np.argmax(spectrum))
        if peak > half:
            peak -= n_fft
        return peak * sample_rate / n_fft

    @staticmethod
    def _compensate_cfo(
        iq: np.ndarray,
        cfo_hz: float,
        sample_rate: int,
        start_sample: int,
    ) -> np.ndarray:
        if abs(cfo_hz) < 1.0:
            return iq
        t = (start_sample + np.arange(len(iq), dtype=np.float64)) / sample_rate
        return (iq * np.exp(-2j * np.pi * cfo_hz * t)).astype(np.complex64)

    # ------------------------------------------------------------------

    def _match_sync_word(
        self, bin0: int, bin1: int, sf: int,
    ) -> int | None:
        tol = self.sync_tolerance
        for sw in self.sync_words:
            expected = sync_word_bins(sw, sf)
            if (abs(bin0 - expected[0]) <= tol
                    and abs(bin1 - expected[1]) <= tol):
                return sw
        return None

    @staticmethod
    def _decode_header_block(
        symbols: list[int],
        sf: int,
    ) -> tuple[list[int] | None, bool]:
        from reticulumpi.builtin_plugins.lora_decode import (
            deinterleave,
            gray_decode,
            hamming_decode,
        )

        ppm = 8
        gray_decoded = [gray_decode(s) for s in symbols[:ppm]]
        try:
            codewords = deinterleave(gray_decoded, sf, 4, reduced=True)
        except ValueError:
            return None, False

        nibbles: list[int] = []
        uncorrectable = False
        for cw in codewords:
            nib, err = hamming_decode(cw, 4)
            nibbles.append(nib)
            if err < 0:
                uncorrectable = True
        # nibbles[3] is the header checksum — not verified because the
        # exact formula varies across LoRa implementations and we lack
        # real-hardware test vectors.  Hamming FEC already catches bit errors.
        return nibbles, not uncorrectable

    @staticmethod
    def decode(pkt: PacketSymbols) -> DecodedPacket:
        """Decode extracted symbols through the full LoRa PHY pipeline."""
        from reticulumpi.builtin_plugins.lora_decode import (
            crc16_ccitt,
            deinterleave,
            gray_decode,
            hamming_decode,
            whiten,
        )

        sf = pkt.detection.sf
        cr = pkt.cr

        # Payload nibbles carried in the header block (after 4 metadata nibs)
        all_nibs: list[int] = list(pkt.header_nibbles[4:])
        errors = 0

        # Decode payload symbol blocks
        if pkt.payload_symbols:
            ppm = 4 + cr
            for i in range(0, len(pkt.payload_symbols), ppm):
                block = pkt.payload_symbols[i : i + ppm]
                if len(block) < ppm:
                    break
                gray_dec = [gray_decode(s) for s in block]
                try:
                    cws = deinterleave(gray_dec, sf, cr)
                except ValueError:
                    break
                for cw in cws:
                    nib, err = hamming_decode(cw, cr)
                    all_nibs.append(nib)
                    if err > 0:
                        errors += err

        # Assemble nibbles → bytes (lo nibble first in each pair)
        total_bytes = pkt.payload_len + (2 if pkt.has_crc else 0)
        n_pairs = min(len(all_nibs) // 2, total_bytes)
        raw = bytearray(n_pairs)
        for j in range(n_pairs):
            lo = all_nibs[j * 2]
            hi = all_nibs[j * 2 + 1]
            raw[j] = (hi << 4) | lo

        # De-whiten
        if raw:
            raw = bytearray(whiten(bytes(raw)))

        # Split payload / CRC
        crc_ok: bool | None = None
        payload_bytes = bytes(raw[: pkt.payload_len])
        if pkt.has_crc and len(raw) >= pkt.payload_len + 2:
            received = (raw[pkt.payload_len] << 8) | raw[pkt.payload_len + 1]
            crc_ok = received == crc16_ccitt(payload_bytes)

        return DecodedPacket(
            detection=pkt.detection,
            sync_word=pkt.sync_word,
            payload_len=pkt.payload_len,
            cr=pkt.cr,
            has_crc=pkt.has_crc,
            header_ok=pkt.header_ok,
            payload=payload_bytes,
            crc_ok=crc_ok,
            errors_corrected=errors,
        )
