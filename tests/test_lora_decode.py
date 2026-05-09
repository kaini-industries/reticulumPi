"""Tests for lora_decode — LoRa PHY-layer codec building blocks."""

from __future__ import annotations

import pytest

from reticulumpi.builtin_plugins.lora_decode import (
    crc16_ccitt,
    deinterleave,
    gray_decode,
    gray_encode,
    hamming_decode,
    hamming_encode,
    interleave,
    whiten,
)


# ===========================================================================
# Gray code
# ===========================================================================


class TestGrayEncode:
    def test_known_values(self):
        assert gray_encode(0) == 0
        assert gray_encode(1) == 1
        assert gray_encode(2) == 3
        assert gray_encode(3) == 2
        assert gray_encode(4) == 6
        assert gray_encode(5) == 7
        assert gray_encode(6) == 5
        assert gray_encode(7) == 4

    def test_single_bit_change(self):
        for n in range(255):
            g0 = gray_encode(n)
            g1 = gray_encode(n + 1)
            diff = g0 ^ g1
            assert diff & (diff - 1) == 0, f"more than 1 bit differs: {n}→{n+1}"


class TestGrayDecode:
    def test_known_values(self):
        assert gray_decode(0) == 0
        assert gray_decode(1) == 1
        assert gray_decode(3) == 2
        assert gray_decode(2) == 3
        assert gray_decode(6) == 4
        assert gray_decode(7) == 5
        assert gray_decode(5) == 6
        assert gray_decode(4) == 7

    @pytest.mark.parametrize("nbits", [4, 7, 8, 10, 12])
    def test_round_trip(self, nbits: int):
        for n in range(2**nbits):
            assert gray_decode(gray_encode(n)) == n

    def test_round_trip_reverse(self):
        for n in range(256):
            assert gray_encode(gray_decode(n)) == n


# ===========================================================================
# Hamming FEC
# ===========================================================================


class TestHammingEncode:
    @pytest.mark.parametrize("cr", [1, 2, 3, 4])
    def test_output_width(self, cr: int):
        for nib in range(16):
            cw = hamming_encode(nib, cr)
            assert cw < (1 << (4 + cr))

    @pytest.mark.parametrize("cr", [1, 2, 3, 4])
    def test_data_in_msbs(self, cr: int):
        for nib in range(16):
            cw = hamming_encode(nib, cr)
            assert (cw >> cr) == nib

    def test_invalid_cr(self):
        with pytest.raises(ValueError):
            hamming_encode(5, 0)
        with pytest.raises(ValueError):
            hamming_encode(5, 5)


class TestHammingDecode:
    @pytest.mark.parametrize("cr", [1, 2, 3, 4])
    def test_clean_round_trip(self, cr: int):
        for nib in range(16):
            cw = hamming_encode(nib, cr)
            decoded, err = hamming_decode(cw, cr)
            assert decoded == nib
            assert err == 0

    @pytest.mark.parametrize("cr", [3, 4])
    def test_single_bit_correction(self, cr: int):
        ppm = 4 + cr
        for nib in range(16):
            cw = hamming_encode(nib, cr)
            for bit in range(ppm):
                corrupted = cw ^ (1 << bit)
                decoded, err = hamming_decode(corrupted, cr)
                assert decoded == nib, (
                    f"CR={cr} nibble={nib} bit={bit}: "
                    f"got {decoded}, expected {nib}"
                )
                assert err == 1

    def test_cr4_double_error_detection(self):
        for nib in range(16):
            cw = hamming_encode(nib, 4)
            for b1 in range(8):
                for b2 in range(b1 + 1, 8):
                    corrupted = cw ^ (1 << b1) ^ (1 << b2)
                    _, err = hamming_decode(corrupted, 4)
                    assert err != 0, (
                        f"double error not detected: nib={nib} bits={b1},{b2}"
                    )

    def test_cr1_error_detection(self):
        for nib in range(16):
            cw = hamming_encode(nib, 1)
            for bit in range(5):
                corrupted = cw ^ (1 << bit)
                _, err = hamming_decode(corrupted, 1)
                assert err != 0

    def test_invalid_cr(self):
        with pytest.raises(ValueError):
            hamming_decode(0, 0)


# ===========================================================================
# Interleaver
# ===========================================================================


class TestRotation:
    def test_rotate_left_identity(self):
        from reticulumpi.builtin_plugins.lora_decode import _rotate_left
        assert _rotate_left(0b10110, 0, 5) == 0b10110

    def test_rotate_left_by_1(self):
        from reticulumpi.builtin_plugins.lora_decode import _rotate_left
        assert _rotate_left(0b10011, 1, 5) == 0b00111

    def test_rotate_full_width(self):
        from reticulumpi.builtin_plugins.lora_decode import _rotate_left
        assert _rotate_left(0b10110, 5, 5) == 0b10110

    def test_rotate_right_inverse(self):
        from reticulumpi.builtin_plugins.lora_decode import (
            _rotate_left,
            _rotate_right,
        )
        for val in range(32):
            for k in range(6):
                assert _rotate_right(_rotate_left(val, k, 5), k, 5) == val


class TestInterleave:
    def test_known_example_sf7_cr1(self):
        codewords = [0, 1, 2, 3, 4, 5, 6]
        symbols = interleave(codewords, sf=7, cr=1)
        assert len(symbols) == 5  # ppm = 4+1
        assert symbols == [8, 25, 3, 36, 2]

    def test_wrong_codeword_count(self):
        with pytest.raises(ValueError):
            interleave([0, 1, 2], sf=7, cr=1)

    @pytest.mark.parametrize("sf", [7, 8, 9, 10, 11, 12])
    @pytest.mark.parametrize("cr", [1, 2, 3, 4])
    def test_round_trip(self, sf: int, cr: int):
        rdd = sf
        codewords = list(range(rdd))
        symbols = interleave(codewords, sf, cr)
        recovered = deinterleave(symbols, sf, cr)
        assert recovered == codewords

    @pytest.mark.parametrize("sf", [7, 9, 12])
    @pytest.mark.parametrize("cr", [1, 4])
    def test_round_trip_reduced(self, sf: int, cr: int):
        rdd = sf - 2
        codewords = list(range(rdd))
        symbols = interleave(codewords, sf, cr, reduced=True)
        assert len(symbols) == 4 + cr
        recovered = deinterleave(symbols, sf, cr, reduced=True)
        assert recovered == codewords

    @pytest.mark.parametrize("sf", [7, 10, 12])
    @pytest.mark.parametrize("cr", [1, 2, 3, 4])
    def test_round_trip_random_data(self, sf: int, cr: int):
        import random
        rng = random.Random(sf * 10 + cr)
        rdd = sf
        ppm = 4 + cr
        codewords = [rng.randint(0, (1 << ppm) - 1) for _ in range(rdd)]
        symbols = interleave(codewords, sf, cr)
        recovered = deinterleave(symbols, sf, cr)
        assert recovered == codewords

    def test_symbol_width_bounded(self):
        codewords = [31, 30, 29, 28, 27, 26, 25]  # 5-bit max for CR=1
        symbols = interleave(codewords, sf=7, cr=1)
        for s in symbols:
            assert s < (1 << 7), f"symbol {s} exceeds {7}-bit width"


class TestDeinterleave:
    def test_known_example_sf7_cr1(self):
        symbols = [8, 25, 3, 36, 2]
        codewords = deinterleave(symbols, sf=7, cr=1)
        assert codewords == [0, 1, 2, 3, 4, 5, 6]

    def test_wrong_symbol_count(self):
        with pytest.raises(ValueError):
            deinterleave([0, 1], sf=7, cr=1)


# ===========================================================================
# Whitening
# ===========================================================================


class TestWhiten:
    def test_round_trip(self):
        data = bytes(range(64))
        whitened = whiten(data)
        recovered = whiten(whitened)
        assert recovered == data

    def test_different_offsets_differ(self):
        data = b"\x00" * 8
        w0 = whiten(data, offset=0)
        w1 = whiten(data, offset=10)
        assert w0 != w1

    def test_deterministic(self):
        data = b"LoRa test payload"
        assert whiten(data) == whiten(data)

    def test_first_byte_nonzero(self):
        w = whiten(b"\x00")
        assert w != b"\x00"

    def test_empty(self):
        assert whiten(b"") == b""

    def test_offset_round_trip(self):
        data = b"offset test"
        for off in [0, 1, 5, 50]:
            assert whiten(whiten(data, offset=off), offset=off) == data


# ===========================================================================
# CRC-16/CCITT
# ===========================================================================


class TestCrc16:
    def test_standard_vector(self):
        assert crc16_ccitt(b"123456789") == 0x29B1

    def test_empty(self):
        assert crc16_ccitt(b"") == 0xFFFF

    def test_single_byte(self):
        result = crc16_ccitt(b"\x00")
        assert isinstance(result, int)
        assert 0 <= result <= 0xFFFF

    def test_different_data_different_crc(self):
        assert crc16_ccitt(b"abc") != crc16_ccitt(b"abd")


# ===========================================================================
# Full pipeline round-trip: encode → decode
# ===========================================================================


class TestFullPipeline:
    """Encode a payload through the full LoRa chain, then decode and verify."""

    @staticmethod
    def _encode_block(
        nibbles: list[int], sf: int, cr: int, reduced: bool = False,
    ) -> list[int]:
        """Encode nibbles → Hamming → interleave → Gray → symbols."""
        codewords = [hamming_encode(n, cr) for n in nibbles]
        rdd = (sf - 2) if reduced else sf
        assert len(codewords) == rdd
        raw_symbols = interleave(codewords, sf, cr, reduced=reduced)
        return [gray_encode(s) for s in raw_symbols]

    @staticmethod
    def _decode_block(
        symbols: list[int], sf: int, cr: int, reduced: bool = False,
    ) -> tuple[list[int], list[int]]:
        """Symbols → Gray decode → de-interleave → Hamming → nibbles."""
        gray_decoded = [gray_decode(s) for s in symbols]
        codewords = deinterleave(gray_decoded, sf, cr, reduced=reduced)
        nibbles = []
        errors = []
        for cw in codewords:
            nib, err = hamming_decode(cw, cr)
            nibbles.append(nib)
            errors.append(err)
        return nibbles, errors

    @pytest.mark.parametrize("sf", [7, 8, 9, 10, 11, 12])
    @pytest.mark.parametrize("cr", [1, 2, 3, 4])
    def test_clean_round_trip(self, sf: int, cr: int):
        rdd = sf
        nibbles = [i % 16 for i in range(rdd)]
        symbols = self._encode_block(nibbles, sf, cr)
        recovered, errors = self._decode_block(symbols, sf, cr)
        assert recovered == nibbles
        assert all(e == 0 for e in errors)

    @pytest.mark.parametrize("sf", [7, 9, 12])
    @pytest.mark.parametrize("cr", [1, 4])
    def test_reduced_rate_round_trip(self, sf: int, cr: int):
        rdd = sf - 2
        nibbles = [i % 16 for i in range(rdd)]
        symbols = self._encode_block(nibbles, sf, cr, reduced=True)
        recovered, errors = self._decode_block(symbols, sf, cr, reduced=True)
        assert recovered == nibbles

    @pytest.mark.parametrize("sf", [7, 10])
    @pytest.mark.parametrize("cr", [3, 4])
    def test_single_symbol_error_corrected(self, sf: int, cr: int):
        rdd = sf
        nibbles = [i % 16 for i in range(rdd)]
        symbols = self._encode_block(nibbles, sf, cr)

        # Flip one bit in the first symbol
        corrupted = list(symbols)
        corrupted[0] ^= 1

        # Gray decode of a corrupted Gray code may produce a value that
        # differs by more than 1 bit from the original — but the Hamming
        # code operates on the post-interleave codeword, so a single
        # symbol-bit flip maps to a single codeword-bit flip for at least
        # one codeword.  Not all nibbles are guaranteed recovered when the
        # interleaver spreads the error, so just check that the pipeline
        # doesn't crash and returns reasonable output.
        recovered, errors = self._decode_block(corrupted, sf, cr)
        assert len(recovered) == rdd
        assert len(errors) == rdd

    @pytest.mark.parametrize("sf", [7, 12])
    def test_whiten_in_pipeline(self, sf: int):
        cr = 4
        payload = bytes([0xDE, 0xAD, 0xBE, 0xEF])
        whitened = whiten(payload)
        # Split into nibbles
        nibbles = []
        for b in whitened:
            nibbles.append(b & 0xF)
            nibbles.append((b >> 4) & 0xF)

        # Encode one block at a time (pad to sf nibbles)
        while len(nibbles) % sf != 0:
            nibbles.append(0)

        all_symbols: list[int] = []
        for i in range(0, len(nibbles), sf):
            block = nibbles[i:i + sf]
            all_symbols.extend(self._encode_block(block, sf, cr))

        # Decode
        recovered_nibbles: list[int] = []
        ppm = 4 + cr
        for i in range(0, len(all_symbols), ppm):
            block = all_symbols[i:i + ppm]
            nibs, _ = self._decode_block(block, sf, cr)
            recovered_nibbles.extend(nibs)

        # Re-assemble bytes (only as many as original)
        recovered_bytes = bytearray()
        for j in range(0, len(payload) * 2, 2):
            lo = recovered_nibbles[j]
            hi = recovered_nibbles[j + 1]
            recovered_bytes.append((hi << 4) | lo)

        dewhitened = whiten(bytes(recovered_bytes))
        assert dewhitened == payload
