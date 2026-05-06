"""Tests for lora_dechirp — LoRa chirp detection DSP module."""

from __future__ import annotations

import numpy as np
import pytest

from reticulumpi.builtin_plugins.lora_dechirp import (
    ChannelFilter,
    ChirpReference,
    DecodedPacket,
    Detection,
    IQRingBuffer,
    PacketExtractor,
    PacketSymbols,
    PreambleTracker,
    SymbolDetector,
    max_packet_samples,
    sync_word_bins,
)

SAMPLE_RATE = 250_000
BW = 125_000


# ---------------------------------------------------------------------------
# Synthetic LoRa signal helpers
# ---------------------------------------------------------------------------

def make_upchirp(
    sf: int,
    symbol: int = 0,
    sample_rate: int = SAMPLE_RATE,
    bw: int = BW,
) -> np.ndarray:
    """Generate one LoRa upchirp for *symbol* at *sf*.

    Symbol k offsets the starting frequency by k * BW / 2^SF.  The
    quadratic phase terms cancel during dechirp (multiply by conjugate
    base chirp), leaving a pure tone at k * BW / 2^SF whose FFT peak
    sits at bin k.
    """
    n = int(round(sample_rate * (2**sf) / bw))
    t = np.arange(n, dtype=np.float64) / sample_rate
    t_sym = n / sample_rate
    delta_f = symbol * bw / (2**sf)
    phase = 2.0 * np.pi * ((-bw / 2.0 + delta_f) * t + bw / (2.0 * t_sym) * t * t)
    return np.exp(1j * phase).astype(np.complex64)


def make_preamble(
    sf: int,
    n_symbols: int = 8,
    symbol: int = 0,
    snr_db: float | None = None,
    sample_rate: int = SAMPLE_RATE,
    bw: int = BW,
) -> np.ndarray:
    """Generate *n_symbols* identical upchirps (a LoRa preamble)."""
    chirp = make_upchirp(sf, symbol, sample_rate, bw)
    preamble = np.tile(chirp, n_symbols)
    if snr_db is not None:
        sig_power = np.mean(np.abs(preamble) ** 2)
        noise_power = sig_power / (10.0 ** (snr_db / 10.0))
        rng = np.random.default_rng(42)
        noise = np.sqrt(noise_power / 2) * (
            rng.standard_normal(len(preamble)).astype(np.float32)
            + 1j * rng.standard_normal(len(preamble)).astype(np.float32)
        )
        preamble = preamble + noise
    return preamble.astype(np.complex64)


# ===========================================================================
# ChirpReference
# ===========================================================================


class TestChirpReference:
    def test_length_per_sf(self):
        cr = ChirpReference(SAMPLE_RATE, BW)
        for sf in range(7, 13):
            expected = int(round(SAMPLE_RATE * (2**sf) / BW))
            assert len(cr.get(sf)) == expected

    def test_dtype(self):
        cr = ChirpReference(SAMPLE_RATE, BW)
        assert cr.get(7).dtype == np.complex64

    def test_caching(self):
        cr = ChirpReference(SAMPLE_RATE, BW)
        a = cr.get(7)
        b = cr.get(7)
        assert a is b

    def test_different_sfs_differ(self):
        cr = ChirpReference(SAMPLE_RATE, BW)
        r7 = cr.get(7)
        r8 = cr.get(8)
        assert len(r7) != len(r8)

    def test_invalidate_clears_cache(self):
        cr = ChirpReference(SAMPLE_RATE, BW)
        a = cr.get(9)
        cr.invalidate()
        b = cr.get(9)
        assert a is not b
        np.testing.assert_array_equal(a, b)

    def test_symbol_length(self):
        cr = ChirpReference(SAMPLE_RATE, BW)
        assert cr.symbol_length(7) == 2 * 128
        assert cr.symbol_length(12) == 2 * 4096


# ===========================================================================
# SymbolDetector
# ===========================================================================


class TestSymbolDetector:
    @pytest.mark.parametrize("sf", [7, 8, 9, 10, 11, 12])
    def test_dechirp_symbol_zero(self, sf: int):
        cr = ChirpReference(SAMPLE_RATE, BW)
        ref = cr.get(sf)
        chirp = make_upchirp(sf, symbol=0)
        bins, mags, nfs, _ = SymbolDetector.dechirp(chirp, sf, ref)
        assert len(bins) == 1
        assert bins[0] == 0

    @pytest.mark.parametrize("sf", [7, 8, 9, 10])
    @pytest.mark.parametrize("symbol", [1, 5, 15, 63])
    def test_dechirp_known_symbol(self, sf: int, symbol: int):
        n_bins = 2**sf
        if symbol >= n_bins:
            pytest.skip(f"symbol {symbol} >= 2^{sf}")
        cr = ChirpReference(SAMPLE_RATE, BW)
        ref = cr.get(sf)
        chirp = make_upchirp(sf, symbol=symbol)
        bins, mags, _, _ = SymbolDetector.dechirp(chirp, sf, ref)
        assert len(bins) == 1
        assert bins[0] == symbol, f"expected bin {symbol}, got {bins[0]}"

    def test_dechirp_multiple_symbols(self):
        sf = 7
        cr = ChirpReference(SAMPLE_RATE, BW)
        ref = cr.get(sf)
        symbols = [0, 10, 50, 100, 127]
        iq = np.concatenate([make_upchirp(sf, s) for s in symbols])
        bins, mags, nfs, _ = SymbolDetector.dechirp(iq, sf, ref)
        assert len(bins) == len(symbols)
        for i, s in enumerate(symbols):
            assert bins[i] == s

    def test_dechirp_with_noise(self):
        sf = 7
        cr = ChirpReference(SAMPLE_RATE, BW)
        ref = cr.get(sf)
        iq = make_preamble(sf, n_symbols=1, symbol=42, snr_db=10.0)
        bins, _, _, _ = SymbolDetector.dechirp(iq, sf, ref)
        assert bins[0] == 42

    def test_empty_input(self):
        sf = 7
        cr = ChirpReference(SAMPLE_RATE, BW)
        ref = cr.get(sf)
        iq = np.array([], dtype=np.complex64)
        bins, mags, nfs, _ = SymbolDetector.dechirp(iq, sf, ref)
        assert len(bins) == 0

    def test_short_input_dropped(self):
        sf = 7
        cr = ChirpReference(SAMPLE_RATE, BW)
        ref = cr.get(sf)
        iq = np.ones(10, dtype=np.complex64)
        bins, _, _, _ = SymbolDetector.dechirp(iq, sf, ref)
        assert len(bins) == 0


# ===========================================================================
# PreambleTracker
# ===========================================================================


class TestPreambleTracker:
    def test_detect_clean_preamble(self):
        cr = ChirpReference(SAMPLE_RATE, BW)
        tracker = PreambleTracker(
            cr, sfs=(7,), preamble_len=8, snr_threshold_db=0.0,
        )
        iq = make_preamble(7, n_symbols=8)
        dets = tracker.feed_chunk(iq, timestamp=1000.0)
        assert len(dets) == 1
        assert dets[0].sf == 7
        assert isinstance(dets[0], Detection)
        assert dets[0].bw == BW

    def test_detection_bw_field_matches_chirp_ref(self):
        """Detection.bw should reflect the ChirpReference BW, not a hardcoded default."""
        half_bw = BW // 2
        cr = ChirpReference(SAMPLE_RATE, half_bw)
        tracker = PreambleTracker(
            cr, sfs=(7,), preamble_len=8, snr_threshold_db=0.0,
        )
        iq = make_preamble(7, n_symbols=8, bw=half_bw)
        dets = tracker.feed_chunk(iq, timestamp=1000.0)
        assert len(dets) == 1
        assert dets[0].bw == half_bw

    def test_no_detection_below_threshold(self):
        cr = ChirpReference(SAMPLE_RATE, BW)
        tracker = PreambleTracker(
            cr, sfs=(7,), preamble_len=8, snr_threshold_db=0.0,
        )
        iq = make_preamble(7, n_symbols=7)
        dets = tracker.feed_chunk(iq, timestamp=1000.0)
        assert len(dets) == 0

    def test_detect_with_noise(self):
        cr = ChirpReference(SAMPLE_RATE, BW)
        tracker = PreambleTracker(
            cr, sfs=(7,), preamble_len=8, snr_threshold_db=3.0,
        )
        iq = make_preamble(7, n_symbols=10, snr_db=12.0)
        dets = tracker.feed_chunk(iq, timestamp=1000.0)
        assert len(dets) >= 1
        assert dets[0].snr_db > 6.0

    def test_snr_filter_rejects_weak(self):
        cr = ChirpReference(SAMPLE_RATE, BW)
        tracker = PreambleTracker(
            cr, sfs=(7,), preamble_len=8, snr_threshold_db=40.0,
            snr_floor_db=40.0,
        )
        iq = make_preamble(7, n_symbols=8, snr_db=3.0)
        dets = tracker.feed_chunk(iq, timestamp=1000.0)
        assert len(dets) == 0

    def test_split_across_chunks(self):
        cr = ChirpReference(SAMPLE_RATE, BW)
        tracker = PreambleTracker(
            cr, sfs=(7,), preamble_len=8, snr_threshold_db=0.0,
        )
        iq = make_preamble(7, n_symbols=8)
        sym_len = cr.symbol_length(7)
        split = 4 * sym_len
        chunk1 = iq[:split]
        chunk2 = iq[split:]

        dets1 = tracker.feed_chunk(chunk1, timestamp=1000.0)
        assert len(dets1) == 0
        dets2 = tracker.feed_chunk(chunk2, timestamp=1001.0)
        assert len(dets2) == 1
        assert dets2[0].sf == 7

    def test_split_mid_symbol(self):
        """Split a chunk in the middle of a symbol — leftover handling."""
        cr = ChirpReference(SAMPLE_RATE, BW)
        tracker = PreambleTracker(
            cr, sfs=(7,), preamble_len=8, snr_threshold_db=0.0,
        )
        iq = make_preamble(7, n_symbols=10)
        sym_len = cr.symbol_length(7)
        # Split at 3.5 symbols
        split = 3 * sym_len + sym_len // 2
        chunk1 = iq[:split]
        chunk2 = iq[split:]

        dets1 = tracker.feed_chunk(chunk1, timestamp=1000.0)
        assert len(dets1) == 0
        dets2 = tracker.feed_chunk(chunk2, timestamp=1001.0)
        assert len(dets2) == 1

    def test_bin_tolerance(self):
        """Preamble with ±1 bin wobble should still be detected."""
        sf = 7
        cr = ChirpReference(SAMPLE_RATE, BW)
        tracker = PreambleTracker(
            cr, sfs=(sf,), preamble_len=8, bin_tolerance=1,
            snr_threshold_db=0.0,
        )
        # Build a preamble where adjacent symbols are at bins 10 and 11
        chirps = []
        for i in range(8):
            sym = 10 + (i % 2)  # alternates 10, 11, 10, 11, ...
            chirps.append(make_upchirp(sf, symbol=sym))
        iq = np.concatenate(chirps)
        dets = tracker.feed_chunk(iq, timestamp=1000.0)
        assert len(dets) == 1

    def test_bin_tolerance_exceeded(self):
        """Wobble of ±2 with tolerance=1 should NOT detect."""
        sf = 7
        cr = ChirpReference(SAMPLE_RATE, BW)
        tracker = PreambleTracker(
            cr, sfs=(sf,), preamble_len=8, bin_tolerance=1,
            snr_threshold_db=0.0,
        )
        chirps = []
        for i in range(8):
            sym = 10 + (i % 3)  # 10, 11, 12, 10, 11, 12, ...
            chirps.append(make_upchirp(sf, symbol=sym))
        iq = np.concatenate(chirps)
        dets = tracker.feed_chunk(iq, timestamp=1000.0)
        assert len(dets) == 0

    def test_bin_tolerance_wraparound(self):
        """Bins 0 and n_bins-1 are adjacent modulo FFT; tolerance should wrap."""
        sf = 7
        n_bins = 2**sf
        cr = ChirpReference(SAMPLE_RATE, BW)
        tracker = PreambleTracker(
            cr, sfs=(sf,), preamble_len=8, bin_tolerance=1,
            snr_threshold_db=0.0,
        )
        chirps = []
        for i in range(8):
            sym = 0 if (i % 2 == 0) else n_bins - 1
            chirps.append(make_upchirp(sf, symbol=sym))
        iq = np.concatenate(chirps)
        dets = tracker.feed_chunk(iq, timestamp=1000.0)
        assert len(dets) == 1

    def test_multiple_sfs_simultaneously(self):
        cr = ChirpReference(SAMPLE_RATE, BW)
        tracker = PreambleTracker(
            cr, sfs=(7, 8), preamble_len=8, snr_threshold_db=0.0,
        )
        # SF7 preamble is shorter; pad to match SF8 length, then overlay
        p7 = make_preamble(7, n_symbols=8)
        p8 = make_preamble(8, n_symbols=8, symbol=5)
        max_len = max(len(p7), len(p8))
        iq = np.zeros(max_len, dtype=np.complex64)
        iq[: len(p7)] += p7
        iq[: len(p8)] += p8

        dets = tracker.feed_chunk(iq, timestamp=1000.0)
        detected_sfs = {d.sf for d in dets}
        # At least SF7 should be detected (SF8 may or may not survive
        # interference from the overlapping SF7 signal)
        assert 7 in detected_sfs

    def test_reset_clears_state(self):
        cr = ChirpReference(SAMPLE_RATE, BW)
        tracker = PreambleTracker(
            cr, sfs=(7,), preamble_len=8, snr_threshold_db=0.0,
        )
        iq = make_preamble(7, n_symbols=5)
        tracker.feed_chunk(iq, timestamp=1000.0)
        tracker.reset()
        # After reset, feeding remaining 3 symbols should NOT trigger
        iq2 = make_preamble(7, n_symbols=3)
        dets = tracker.feed_chunk(iq2, timestamp=1001.0)
        assert len(dets) == 0

    def test_detection_fields(self):
        cr = ChirpReference(SAMPLE_RATE, BW)
        tracker = PreambleTracker(
            cr, sfs=(7,), preamble_len=8, snr_threshold_db=0.0,
        )
        symbol = 20
        iq = make_preamble(7, n_symbols=8, symbol=symbol)
        dets = tracker.feed_chunk(iq, timestamp=1234.5)
        assert len(dets) == 1
        d = dets[0]
        assert d.sf == 7
        assert d.timestamp == 1234.5
        assert d.freq_offset_bin == symbol
        expected_hz = symbol * BW / (2**7)
        assert abs(d.freq_offset_hz - expected_hz) < 0.5
        assert d.snr_db > 0

    @pytest.mark.parametrize("sf", [7, 9, 12])
    def test_detect_across_sfs(self, sf: int):
        cr = ChirpReference(SAMPLE_RATE, BW)
        tracker = PreambleTracker(
            cr, sfs=(sf,), preamble_len=8, snr_threshold_db=0.0,
        )
        iq = make_preamble(sf, n_symbols=10)
        dets = tracker.feed_chunk(iq, timestamp=1000.0)
        assert len(dets) >= 1
        assert dets[0].sf == sf

    def test_two_packets_in_one_chunk(self):
        sf = 7
        cr = ChirpReference(SAMPLE_RATE, BW)
        tracker = PreambleTracker(
            cr, sfs=(sf,), preamble_len=8, snr_threshold_db=0.0,
        )
        # Two preambles at different symbols with a gap between
        gap = np.zeros(cr.symbol_length(sf) * 2, dtype=np.complex64)
        p1 = make_preamble(sf, n_symbols=8, symbol=10)
        p2 = make_preamble(sf, n_symbols=8, symbol=50)
        iq = np.concatenate([p1, gap, p2])
        dets = tracker.feed_chunk(iq, timestamp=1000.0)
        assert len(dets) == 2
        assert dets[0].freq_offset_bin == 10
        assert dets[1].freq_offset_bin == 50


# ===========================================================================
# IQRingBuffer
# ===========================================================================


class TestIQRingBuffer:
    def test_write_read(self):
        buf = IQRingBuffer(100)
        data = np.arange(50, dtype=np.complex64)
        buf.write(data)
        result = buf.read(0, 50)
        assert result is not None
        np.testing.assert_array_equal(result, data)

    def test_write_offset_advances(self):
        buf = IQRingBuffer(100)
        assert buf.write_offset == 0
        buf.write(np.zeros(30, dtype=np.complex64))
        assert buf.write_offset == 30
        buf.write(np.zeros(40, dtype=np.complex64))
        assert buf.write_offset == 70

    def test_read_not_yet_available(self):
        buf = IQRingBuffer(100)
        buf.write(np.zeros(50, dtype=np.complex64))
        assert buf.read(0, 60) is None

    def test_read_expired(self):
        buf = IQRingBuffer(100)
        buf.write(np.zeros(100, dtype=np.complex64))
        buf.write(np.zeros(50, dtype=np.complex64))
        assert buf.read(0, 10) is None

    def test_wrap_around_write_and_read(self):
        buf = IQRingBuffer(100)
        d1 = np.arange(80, dtype=np.complex64)
        d2 = np.arange(80, 130, dtype=np.complex64)
        buf.write(d1)
        buf.write(d2)
        result = buf.read(80, 50)
        assert result is not None
        np.testing.assert_array_equal(result, d2)

    def test_wrap_around_spanning_read(self):
        buf = IQRingBuffer(100)
        buf.write(np.arange(80, dtype=np.complex64))
        buf.write(np.arange(80, 130, dtype=np.complex64))
        result = buf.read(70, 40)
        assert result is not None
        np.testing.assert_array_equal(result, np.arange(70, 110, dtype=np.complex64))

    def test_available_range(self):
        buf = IQRingBuffer(100)
        assert buf.available_range() == (0, 0)
        buf.write(np.zeros(50, dtype=np.complex64))
        assert buf.available_range() == (0, 50)
        buf.write(np.zeros(80, dtype=np.complex64))
        assert buf.available_range() == (30, 130)

    def test_oversize_write(self):
        buf = IQRingBuffer(100)
        data = np.arange(200, dtype=np.complex64)
        buf.write(data)
        assert buf.read(0, 50) is None
        result = buf.read(100, 100)
        assert result is not None
        np.testing.assert_array_equal(result, data[100:])

    def test_empty_write(self):
        buf = IQRingBuffer(100)
        buf.write(np.array([], dtype=np.complex64))
        assert buf.write_offset == 0

    def test_reset(self):
        buf = IQRingBuffer(100)
        buf.write(np.ones(50, dtype=np.complex64))
        buf.reset()
        assert buf.write_offset == 0
        assert buf.available_range() == (0, 0)
        assert buf.read(0, 1) is None


# ===========================================================================
# sync_word_bins
# ===========================================================================


class TestSyncWordBins:
    def test_lorawan_public_sf7(self):
        assert sync_word_bins(0x34, 7) == (24, 32)

    def test_lorawan_public_sf12(self):
        assert sync_word_bins(0x34, 12) == (3 * 256, 4 * 256)

    def test_private_sf7(self):
        assert sync_word_bins(0x12, 7) == (8, 16)

    def test_zero_sync(self):
        assert sync_word_bins(0x00, 7) == (0, 0)

    def test_meshtastic_sf7(self):
        assert sync_word_bins(0x2B, 7) == (2 * 8, 11 * 8)

    def test_ff_sync_sf7(self):
        assert sync_word_bins(0xFF, 7) == (15 * 8, 15 * 8)


class TestMatchSyncWordWrapAround:
    """_match_sync_word must handle wrap-around at the [0, 2^SF) boundary."""

    def _make_extractor(self, sync_words=(0x00,), tol=2):
        cref = ChirpReference(SAMPLE_RATE, BW)
        ext = PacketExtractor(cref, known_sync_words=sync_words)
        ext.sync_tolerance = tol
        return ext

    def test_wrap_around_matches(self):
        ext = self._make_extractor(sync_words=(0x00,), tol=2)
        # sync 0x00 at SF7 → bins (0, 0); bin 127 is distance 1 from 0
        assert ext._match_sync_word(127, 127, 7) == 0x00

    def test_wrap_around_exceeds_tolerance(self):
        ext = self._make_extractor(sync_words=(0x00,), tol=2)
        # bin 124 is distance 4 from 0 at SF7 (128 bins) — exceeds tol=2
        assert ext._match_sync_word(124, 124, 7) is None

    def test_non_wrap_still_works(self):
        ext = self._make_extractor(sync_words=(0x34,), tol=2)
        # 0x34 at SF7 → bins (24, 32); exact match
        assert ext._match_sync_word(24, 32, 7) == 0x34


# ===========================================================================
# Synthetic LoRa packet generator
# ===========================================================================


def make_lora_packet(
    sf: int,
    payload: bytes,
    cr: int = 1,
    sync_byte: int = 0x34,
    preamble_len: int = 8,
    sample_rate: int = SAMPLE_RATE,
    bw: int = BW,
) -> np.ndarray:
    """Generate a complete synthetic LoRa packet as IQ samples."""
    from reticulumpi.builtin_plugins.lora_decode import (
        crc16_ccitt,
        gray_encode,
        hamming_encode,
        interleave,
        whiten,
    )

    crc = crc16_ccitt(payload)
    full_data = payload + bytes([(crc >> 8) & 0xFF, crc & 0xFF])
    whitened = whiten(full_data)

    all_nibbles: list[int] = []
    for b in whitened:
        all_nibbles.append(b & 0xF)
        all_nibbles.append((b >> 4) & 0xF)

    # Header block: SF-2 nibbles at CR=4 reduced rate
    rdd_h = sf - 2
    header_nibbles = [0] * rdd_h
    header_nibbles[0] = len(payload) & 0xF
    header_nibbles[1] = (len(payload) >> 4) & 0xF
    header_nibbles[2] = ((cr & 0x7) << 1) | 1  # has_crc = 1
    header_nibbles[3] = 0  # checksum — not verified (see _decode_header_block)
    nib_idx = 0
    for i in range(4, rdd_h):
        if nib_idx < len(all_nibbles):
            header_nibbles[i] = all_nibbles[nib_idx]
            nib_idx += 1

    header_cws = [hamming_encode(n, 4) for n in header_nibbles]
    header_raw = interleave(header_cws, sf, 4, reduced=True)
    header_syms = [gray_encode(s) for s in header_raw]

    # Remaining payload
    remaining = all_nibbles[nib_idx:]
    payload_syms: list[int] = []
    while remaining:
        block = remaining[:sf]
        remaining = remaining[sf:]
        while len(block) < sf:
            block.append(0)
        cws = [hamming_encode(n, cr) for n in block]
        raw = interleave(cws, sf, cr)
        payload_syms.extend([gray_encode(s) for s in raw])

    # Modulate to IQ
    parts: list[np.ndarray] = []
    base = make_upchirp(sf, 0, sample_rate, bw)
    for _ in range(preamble_len):
        parts.append(base)

    high_bin, low_bin = sync_word_bins(sync_byte, sf)
    parts.append(make_upchirp(sf, high_bin, sample_rate, bw))
    parts.append(make_upchirp(sf, low_bin, sample_rate, bw))

    dc = np.conj(base)
    parts.append(dc)
    parts.append(dc)
    parts.append(dc[: len(dc) // 4])

    for s in header_syms:
        parts.append(make_upchirp(sf, s, sample_rate, bw))
    for s in payload_syms:
        parts.append(make_upchirp(sf, s, sample_rate, bw))

    return np.concatenate(parts)


# ===========================================================================
# PacketExtractor
# ===========================================================================


class TestPacketExtractor:
    def _make_detection(self, sf: int = 7) -> Detection:
        return Detection(
            timestamp=1000.0,
            sf=sf,
            freq_offset_hz=0.0,
            freq_offset_bin=0,
            snr_db=20.0,
            sample_offset=0,
        )

    def test_full_round_trip_sf7_cr1(self):
        sf, cr_pkt, sync = 7, 1, 0x34
        payload = b"\xDE\xAD"
        pkt_iq = make_lora_packet(sf, payload, cr=cr_pkt, sync_byte=sync)
        cref = ChirpReference(SAMPLE_RATE, BW)
        ring = IQRingBuffer(len(pkt_iq) + 10000)
        ring.write(pkt_iq)

        ext = PacketExtractor(cref, known_sync_words=(sync,))
        syms = ext.try_extract(self._make_detection(sf), ring)
        assert syms is not None
        assert syms.sync_word == sync
        assert syms.payload_len == len(payload)
        assert syms.cr == cr_pkt
        assert syms.has_crc is True

        decoded = PacketExtractor.decode(syms)
        assert decoded.payload == payload
        assert decoded.crc_ok is True
        assert decoded.errors_corrected == 0

    def test_full_round_trip_sf7_cr4(self):
        sf, cr_pkt, sync = 7, 4, 0x34
        payload = b"Hello LoRa"
        pkt_iq = make_lora_packet(sf, payload, cr=cr_pkt, sync_byte=sync)
        cref = ChirpReference(SAMPLE_RATE, BW)
        ring = IQRingBuffer(len(pkt_iq) + 10000)
        ring.write(pkt_iq)

        ext = PacketExtractor(cref, known_sync_words=(sync,))
        syms = ext.try_extract(self._make_detection(sf), ring)
        assert syms is not None
        assert syms.cr == cr_pkt

        decoded = PacketExtractor.decode(syms)
        assert decoded.payload == payload
        assert decoded.crc_ok is True

    @pytest.mark.parametrize("sf", [7, 8, 9])
    def test_round_trip_across_sfs(self, sf: int):
        payload = b"\x01\x02\x03\x04"
        sync = 0x12
        pkt_iq = make_lora_packet(sf, payload, cr=1, sync_byte=sync)
        cref = ChirpReference(SAMPLE_RATE, BW)
        ring = IQRingBuffer(len(pkt_iq) + 10000)
        ring.write(pkt_iq)

        ext = PacketExtractor(cref, known_sync_words=(sync,))
        syms = ext.try_extract(self._make_detection(sf), ring)
        assert syms is not None

        decoded = PacketExtractor.decode(syms)
        assert decoded.payload == payload
        assert decoded.crc_ok is True

    def test_sync_word_mismatch_returns_none(self):
        sf, sync = 7, 0x34
        pkt_iq = make_lora_packet(sf, b"\x00", sync_byte=sync)
        cref = ChirpReference(SAMPLE_RATE, BW)
        ring = IQRingBuffer(len(pkt_iq) + 1000)
        ring.write(pkt_iq)

        ext = PacketExtractor(cref, known_sync_words=(0xFF,))
        assert ext.try_extract(self._make_detection(sf), ring) is None

    def test_insufficient_buffer_returns_none(self):
        sf = 7
        cref = ChirpReference(SAMPLE_RATE, BW)
        ring = IQRingBuffer(1000)
        ring.write(np.zeros(500, dtype=np.complex64))

        ext = PacketExtractor(cref)
        assert ext.try_extract(self._make_detection(sf), ring) is None

    def test_expired_data_returns_none(self):
        sf = 7
        pkt_iq = make_lora_packet(sf, b"\xAA")
        cref = ChirpReference(SAMPLE_RATE, BW)
        ring = IQRingBuffer(1000)
        ring.write(pkt_iq)
        # Overwrite the data
        ring.write(np.zeros(2000, dtype=np.complex64))

        ext = PacketExtractor(cref)
        assert ext.try_extract(self._make_detection(sf), ring) is None

    def test_private_sync_word(self):
        sf, sync = 7, 0x12
        payload = b"\xBE\xEF"
        pkt_iq = make_lora_packet(sf, payload, sync_byte=sync)
        cref = ChirpReference(SAMPLE_RATE, BW)
        ring = IQRingBuffer(len(pkt_iq) + 10000)
        ring.write(pkt_iq)

        ext = PacketExtractor(cref)
        syms = ext.try_extract(self._make_detection(sf), ring)
        assert syms is not None
        assert syms.sync_word == sync

    def test_meshtastic_sync_word(self):
        sf, sync = 7, 0x2B
        payload = b"\xCA\xFE"
        pkt_iq = make_lora_packet(sf, payload, sync_byte=sync)
        cref = ChirpReference(SAMPLE_RATE, BW)
        ring = IQRingBuffer(len(pkt_iq) + 10000)
        ring.write(pkt_iq)

        ext = PacketExtractor(cref)
        syms = ext.try_extract(self._make_detection(sf), ring)
        assert syms is not None
        assert syms.sync_word == sync

    def test_header_fields_correct(self):
        sf, cr_pkt = 7, 3
        payload = b"AB"
        pkt_iq = make_lora_packet(sf, payload, cr=cr_pkt)
        cref = ChirpReference(SAMPLE_RATE, BW)
        ring = IQRingBuffer(len(pkt_iq) + 10000)
        ring.write(pkt_iq)

        ext = PacketExtractor(cref)
        syms = ext.try_extract(self._make_detection(sf), ring)
        assert syms is not None
        assert syms.payload_len == 2
        assert syms.cr == 3
        assert syms.has_crc is True
        assert syms.header_ok is True

    @pytest.mark.parametrize("snr_db", [15.0, 20.0])
    def test_full_round_trip_with_noise(self, snr_db: float):
        sf, cr_pkt, sync = 7, 1, 0x34
        payload = b"\xDE\xAD"
        pkt_iq = make_lora_packet(sf, payload, cr=cr_pkt, sync_byte=sync)
        sig_power = np.mean(np.abs(pkt_iq) ** 2)
        noise_power = sig_power / (10.0 ** (snr_db / 10.0))
        rng = np.random.default_rng(42)
        noise = np.sqrt(noise_power / 2) * (
            rng.standard_normal(len(pkt_iq)).astype(np.float32)
            + 1j * rng.standard_normal(len(pkt_iq)).astype(np.float32)
        )
        noisy_iq = (pkt_iq + noise).astype(np.complex64)

        cref = ChirpReference(SAMPLE_RATE, BW)
        ring = IQRingBuffer(len(noisy_iq) + 10000)
        ring.write(noisy_iq)

        ext = PacketExtractor(cref, known_sync_words=(sync,))
        syms = ext.try_extract(self._make_detection(sf), ring)
        assert syms is not None
        decoded = PacketExtractor.decode(syms)
        assert decoded.payload == payload
        assert decoded.crc_ok is True

    @pytest.mark.parametrize("cr_pkt", [2, 3])
    def test_full_round_trip_sf7_cr2_cr3(self, cr_pkt: int):
        sf, sync = 7, 0x34
        payload = b"\xCA\xFE"
        pkt_iq = make_lora_packet(sf, payload, cr=cr_pkt, sync_byte=sync)
        cref = ChirpReference(SAMPLE_RATE, BW)
        ring = IQRingBuffer(len(pkt_iq) + 10000)
        ring.write(pkt_iq)

        ext = PacketExtractor(cref, known_sync_words=(sync,))
        syms = ext.try_extract(self._make_detection(sf), ring)
        assert syms is not None
        assert syms.cr == cr_pkt
        decoded = PacketExtractor.decode(syms)
        assert decoded.payload == payload
        assert decoded.crc_ok is True

    def test_sync_word_tolerance(self):
        sf, sync = 7, 0x34
        payload = b"\xAA"
        pkt_iq = make_lora_packet(sf, payload, cr=1, sync_byte=sync)
        cref = ChirpReference(SAMPLE_RATE, BW)

        expected_bins = sync_word_bins(sync, sf)
        sym_len = cref.symbol_length(sf)
        preamble_end = 8 * sym_len

        offset_chirp0 = make_upchirp(sf, expected_bins[0] + 1)
        offset_chirp1 = make_upchirp(sf, expected_bins[1] - 1)
        pkt_iq[preamble_end:preamble_end + sym_len] = offset_chirp0
        pkt_iq[preamble_end + sym_len:preamble_end + 2 * sym_len] = offset_chirp1

        ring = IQRingBuffer(len(pkt_iq) + 10000)
        ring.write(pkt_iq)

        ext = PacketExtractor(cref, known_sync_words=(sync,), sync_tolerance=1)
        syms = ext.try_extract(self._make_detection(sf), ring)
        assert syms is not None
        assert syms.sync_word == sync

    def test_sync_word_tolerance_zero_rejects_offset(self):
        sf, sync = 7, 0x34
        payload = b"\xAA"
        pkt_iq = make_lora_packet(sf, payload, cr=1, sync_byte=sync)
        cref = ChirpReference(SAMPLE_RATE, BW)

        expected_bins = sync_word_bins(sync, sf)
        sym_len = cref.symbol_length(sf)
        preamble_end = 8 * sym_len

        offset_chirp0 = make_upchirp(sf, expected_bins[0] + 1)
        pkt_iq[preamble_end:preamble_end + sym_len] = offset_chirp0

        ring = IQRingBuffer(len(pkt_iq) + 10000)
        ring.write(pkt_iq)

        ext = PacketExtractor(cref, known_sync_words=(sync,), sync_tolerance=0)
        syms = ext.try_extract(self._make_detection(sf), ring)
        assert syms is None


# ===========================================================================
# max_packet_samples
# ===========================================================================


class TestMaxPacketSamples:
    def test_sf7_fits_actual_packet(self):
        """Capacity must be >= actual packet IQ length for any payload."""
        sf, cr = 7, 1
        payload = b"\xDE\xAD"
        pkt_iq = make_lora_packet(sf, payload, cr=cr)
        cap = max_packet_samples(sf, SAMPLE_RATE, BW, cr=cr, max_payload_len=len(payload))
        assert cap >= len(pkt_iq)

    def test_sf12_max_payload(self):
        cap = max_packet_samples(12, SAMPLE_RATE, BW, cr=4, max_payload_len=255)
        assert cap > SAMPLE_RATE * 2, "SF12 max packet must exceed 2s buffer"

    @pytest.mark.parametrize("sf", [7, 8, 9, 10, 11, 12])
    def test_monotonic_with_sf(self, sf: int):
        """Higher SF → more samples (at same payload/CR)."""
        cap = max_packet_samples(sf, SAMPLE_RATE, BW)
        cap_prev = max_packet_samples(max(7, sf - 1), SAMPLE_RATE, BW)
        if sf > 7:
            assert cap > cap_prev

    def test_sf12_ring_buffer_holds_packet(self):
        sf, cr = 12, 1
        payload = b"\xAB\xCD"
        pkt_iq = make_lora_packet(sf, payload, cr=cr)
        cap = max_packet_samples(sf, SAMPLE_RATE, BW, cr=cr, max_payload_len=len(payload))
        ring = IQRingBuffer(cap)
        ring.write(pkt_iq)
        result = ring.read(0, len(pkt_iq))
        assert result is not None
        np.testing.assert_array_equal(result, pkt_iq)


# ===========================================================================
# CFO compensation
# ===========================================================================


def _apply_freq_offset(iq: np.ndarray, offset_hz: float, sample_rate: int) -> np.ndarray:
    """Shift IQ by a constant frequency offset (simulates RTL-SDR drift)."""
    t = np.arange(len(iq), dtype=np.float64) / sample_rate
    return (iq * np.exp(2j * np.pi * offset_hz * t)).astype(np.complex64)


class TestCFOCompensation:
    def _extract_with_offset(
        self,
        sf: int,
        payload: bytes,
        offset_hz: float,
        cr: int = 1,
        sync: int = 0x34,
    ) -> DecodedPacket | None:
        pkt_iq = make_lora_packet(sf, payload, cr=cr, sync_byte=sync)
        shifted = _apply_freq_offset(pkt_iq, offset_hz, SAMPLE_RATE)

        cref = ChirpReference(SAMPLE_RATE, BW)
        ring = IQRingBuffer(len(shifted) + 10000)
        ring.write(shifted)

        tracker = PreambleTracker(
            cref, sfs=(sf,), preamble_len=8,
            snr_threshold_db=0.0, snr_floor_db=0.0,
        )
        dets = tracker.feed_chunk(shifted, timestamp=1000.0)
        if not dets:
            return None

        ext = PacketExtractor(cref, known_sync_words=(sync,))
        syms = ext.try_extract(dets[0], ring)
        if syms is None:
            return None
        return PacketExtractor.decode(syms)

    def test_zero_offset_still_works(self):
        decoded = self._extract_with_offset(7, b"\xDE\xAD", 0.0)
        assert decoded is not None
        assert decoded.payload == b"\xDE\xAD"
        assert decoded.crc_ok is True

    @pytest.mark.parametrize("offset_hz", [500.0, 1000.0, 2000.0, 4000.0])
    def test_sf7_recovers_with_moderate_offset(self, offset_hz: float):
        decoded = self._extract_with_offset(7, b"\xCA\xFE", offset_hz)
        assert decoded is not None
        assert decoded.payload == b"\xCA\xFE"
        assert decoded.crc_ok is True

    @pytest.mark.parametrize("offset_hz", [-1000.0, -3000.0])
    def test_negative_offset(self, offset_hz: float):
        decoded = self._extract_with_offset(7, b"\xAB", offset_hz)
        assert decoded is not None
        assert decoded.payload == b"\xAB"
        assert decoded.crc_ok is True

    def test_sf9_with_large_offset(self):
        """SF9 has 512 bins — 4.5 kHz offset is ~18 bins, would fail without CFO."""
        decoded = self._extract_with_offset(9, b"\x01\x02", 4000.0)
        assert decoded is not None
        assert decoded.crc_ok is True


# ===========================================================================
# ChannelFilter
# ===========================================================================


class TestChannelFilter:
    def test_preserves_in_band_signal(self):
        """A LoRa preamble within BW should pass through unharmed."""
        filt = ChannelFilter(SAMPLE_RATE, BW)
        preamble = make_preamble(7, n_symbols=8)
        filtered = filt.apply(preamble)
        assert filtered.shape == preamble.shape
        assert filtered.dtype == np.complex64
        # Signal power should be mostly preserved (> 90%)
        ratio = np.mean(np.abs(filtered) ** 2) / np.mean(np.abs(preamble) ** 2)
        assert ratio > 0.9

    def test_attenuates_out_of_band_noise(self):
        """Noise outside BW should be reduced."""
        filt = ChannelFilter(SAMPLE_RATE, BW)
        rng = np.random.default_rng(42)
        n = 10000
        # Broadband noise
        noise = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
        filtered = filt.apply(noise)
        # Filtered noise should have less power (filter passes BW/SR = 50% of band)
        ratio = np.mean(np.abs(filtered) ** 2) / np.mean(np.abs(noise) ** 2)
        assert ratio < 0.7

    def test_improves_detection_at_low_snr(self):
        """Filter should enable preamble detection at SNR that fails without it."""
        sf = 7
        cr = ChirpReference(SAMPLE_RATE, BW)
        filt = ChannelFilter(SAMPLE_RATE, BW)

        preamble = make_preamble(sf, n_symbols=8, snr_db=6.0)

        # Without filter — may or may not detect
        tracker_raw = PreambleTracker(
            cr, sfs=(sf,), preamble_len=8, snr_threshold_db=8.0,
        )
        dets_raw = tracker_raw.feed_chunk(preamble, timestamp=1000.0)

        # With filter — should detect due to reduced noise
        tracker_filt = PreambleTracker(
            cr, sfs=(sf,), preamble_len=8, snr_threshold_db=8.0,
        )
        filtered = filt.apply(preamble)
        dets_filt = tracker_filt.feed_chunk(filtered, timestamp=1000.0)

        assert len(dets_filt) >= len(dets_raw)
        assert len(dets_filt) >= 1

    def test_kernel_cached(self):
        f1 = ChannelFilter(SAMPLE_RATE, BW)
        f2 = ChannelFilter(SAMPLE_RATE, BW)
        assert f1._kernel is f2._kernel

    def test_short_input_passthrough(self):
        filt = ChannelFilter(SAMPLE_RATE, BW, num_taps=31)
        short = np.ones(10, dtype=np.complex64)
        result = filt.apply(short)
        np.testing.assert_array_equal(result, short)


# ===========================================================================
# Adaptive SNR threshold
# ===========================================================================


class TestAdaptiveSNRThreshold:
    def test_weak_signal_detected_in_quiet_environment(self):
        """An 8 dB preamble should be detected when the noise floor is low,
        even though 8 dB is below the default fixed threshold of 12 dB."""
        sf = 7
        cr = ChirpReference(SAMPLE_RATE, BW)

        # Prime the tracker with noise-only chunks so noise_ema stabilises
        rng = np.random.default_rng(99)
        tracker = PreambleTracker(
            cr, sfs=(sf,), preamble_len=8,
            snr_threshold_db=12.0,
            snr_margin_db=10.0,
            snr_floor_db=5.0,
        )
        sym_len = cr.symbol_length(sf)
        noise_chunk = (rng.standard_normal(sym_len * 20).astype(np.float32) * 0.01
                       + 1j * rng.standard_normal(sym_len * 20).astype(np.float32) * 0.01)
        tracker.feed_chunk(noise_chunk.astype(np.complex64), timestamp=999.0)

        # Now feed a preamble at 8 dB SNR
        preamble = make_preamble(sf, n_symbols=8, snr_db=8.0)
        dets = tracker.feed_chunk(preamble, timestamp=1000.0)
        assert len(dets) >= 1, "8 dB preamble should be detected in quiet noise"

    def test_noise_only_no_false_detection(self):
        """Pure noise should never trigger a detection."""
        sf = 7
        cr = ChirpReference(SAMPLE_RATE, BW)
        tracker = PreambleTracker(
            cr, sfs=(sf,), preamble_len=8,
            snr_threshold_db=12.0,
            snr_margin_db=10.0,
            snr_floor_db=5.0,
        )
        rng = np.random.default_rng(77)
        sym_len = cr.symbol_length(sf)
        for i in range(10):
            noise = (rng.standard_normal(sym_len * 16).astype(np.float32)
                     + 1j * rng.standard_normal(sym_len * 16).astype(np.float32))
            dets = tracker.feed_chunk(noise.astype(np.complex64), timestamp=1000.0 + i)
            assert len(dets) == 0, f"False detection in noise chunk {i}"

    def test_floor_prevents_absurdly_weak_detection(self):
        """Even in dead silence, SNR below snr_floor_db should be rejected.
        Post-dechirp SNR for a 3 dB input preamble at SF7 is ~28 dB
        (dechirp gain ≈ 10·log10(128) ≈ 21 dB), so the floor must be
        set above that to reject."""
        sf = 7
        cr = ChirpReference(SAMPLE_RATE, BW)
        tracker = PreambleTracker(
            cr, sfs=(sf,), preamble_len=8,
            snr_threshold_db=12.0,
            snr_margin_db=10.0,
            snr_floor_db=35.0,  # above the ~28 dB post-dechirp SNR
        )
        preamble = make_preamble(sf, n_symbols=8, snr_db=3.0)
        dets = tracker.feed_chunk(preamble, timestamp=1000.0)
        assert len(dets) == 0, "Post-dechirp ~28 dB should be rejected by 35 dB floor"


# ===========================================================================
# DC offset rejection
# ===========================================================================


class TestDCOffsetRejection:
    def test_dc_offset_does_not_trigger_detection(self):
        """A strong DC offset (RTL-SDR bias) should not produce false preambles."""
        sf = 11
        cr = ChirpReference(SAMPLE_RATE, BW)
        tracker = PreambleTracker(
            cr, sfs=(sf,), preamble_len=8, snr_threshold_db=6.0,
            snr_floor_db=3.0,
        )
        sym_len = cr.symbol_length(sf)
        rng = np.random.default_rng(42)
        noise = (rng.standard_normal(sym_len * 20).astype(np.float32) * 0.01
                 + 1j * rng.standard_normal(sym_len * 20).astype(np.float32) * 0.01)
        dc_offset = np.complex64(10.0 + 5.0j)
        iq = noise.astype(np.complex64) + dc_offset
        dets = tracker.feed_chunk(iq, timestamp=1000.0)
        assert len(dets) == 0, "DC offset should not trigger false preamble"

    def test_signal_still_detected_with_dc_offset(self):
        """A real preamble with a DC offset should still be detected."""
        sf = 7
        cr = ChirpReference(SAMPLE_RATE, BW)
        tracker = PreambleTracker(
            cr, sfs=(sf,), preamble_len=8, snr_threshold_db=0.0,
        )
        preamble = make_preamble(sf, n_symbols=8)
        dc_offset = np.complex64(5.0 + 3.0j)
        iq = preamble + dc_offset
        dets = tracker.feed_chunk(iq, timestamp=1000.0)
        assert len(dets) == 1, "Real preamble should survive DC removal"
        assert dets[0].sf == sf

    @pytest.mark.parametrize("sf", [7, 10, 12])
    def test_dc_rejection_across_sfs(self, sf: int):
        """DC-only input must not fire at any spreading factor."""
        cr = ChirpReference(SAMPLE_RATE, BW)
        tracker = PreambleTracker(
            cr, sfs=(sf,), preamble_len=8, snr_threshold_db=6.0,
            snr_floor_db=3.0,
        )
        sym_len = cr.symbol_length(sf)
        rng = np.random.default_rng(42)
        noise_power = 0.01
        noise = np.sqrt(noise_power / 2) * (
            rng.standard_normal(sym_len * 20).astype(np.float32)
            + 1j * rng.standard_normal(sym_len * 20).astype(np.float32)
        )
        iq = noise.astype(np.complex64) + np.complex64(8.0 + 8.0j)
        dets = tracker.feed_chunk(iq, timestamp=1000.0)
        assert len(dets) == 0, f"DC-only should not trigger at SF{sf}"


# ===========================================================================
# Bin spread quality check
# ===========================================================================


class TestBinSpreadRejection:
    def test_tight_bins_accepted(self):
        """A clean preamble with zero bin spread should always pass."""
        sf = 7
        cr = ChirpReference(SAMPLE_RATE, BW)
        tracker = PreambleTracker(
            cr, sfs=(sf,), preamble_len=8, snr_threshold_db=0.0,
            max_bin_spread=0.6,
        )
        iq = make_preamble(sf, n_symbols=8, symbol=42)
        dets = tracker.feed_chunk(iq, timestamp=1000.0)
        assert len(dets) == 1

    def test_high_spread_rejected(self):
        """A very tight spread threshold should reject even mild alternation."""
        sf = 7
        cr = ChirpReference(SAMPLE_RATE, BW)
        tracker = PreambleTracker(
            cr, sfs=(sf,), preamble_len=8, snr_threshold_db=0.0,
            bin_tolerance=1, max_bin_spread=0.05,
        )
        chirps = [make_upchirp(sf, symbol=10 + (i % 2)) for i in range(8)]
        iq = np.concatenate(chirps)
        dets = tracker.feed_chunk(iq, timestamp=1000.0)
        assert len(dets) == 0, "Alternating bins with tight spread should be rejected"
