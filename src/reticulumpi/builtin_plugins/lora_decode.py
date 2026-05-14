"""LoRa PHY-layer codec — Gray, interleave, Hamming FEC, whiten, CRC.

Pure-function building blocks for decoding LoRa symbols (from the demodulation
stage) into payload bytes.  Also provides matching encode functions for
round-trip testing.

Pipeline (decode direction):
  raw symbols → Gray decode → de-interleave → Hamming decode
    → de-whiten → assemble bytes → CRC verify

Each function is stateless and independently testable.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Gray code
# ---------------------------------------------------------------------------


def gray_encode(n: int) -> int:
    return n ^ (n >> 1)


def gray_decode(n: int) -> int:
    mask = n >> 1
    while mask:
        n ^= mask
        mask >>= 1
    return n


# ---------------------------------------------------------------------------
# Hamming FEC  (CR 1–4 → 4/5 through 4/8)
#
# Codeword layout: data nibble in the MSBs, parity in the LSBs.
#   CR=1 (5 bits): [d3 d2 d1 d0 | p0]
#   CR=2 (6 bits): [d3 d2 d1 d0 | p1 p0]
#   CR=3 (7 bits): [d3 d2 d1 d0 | p2 p1 p0]       Hamming(7,4)
#   CR=4 (8 bits): [d3 d2 d1 d0 | p3 p2 p1 p0]     extended Hamming(8,4)
# ---------------------------------------------------------------------------

_CR_RANGE = range(1, 5)


def hamming_encode(nibble: int, cr: int) -> int:
    """Encode a 4-bit nibble → (4+cr)-bit codeword."""
    if cr not in _CR_RANGE:
        raise ValueError(f"cr must be 1–4, got {cr}")
    d0 = (nibble >> 0) & 1
    d1 = (nibble >> 1) & 1
    d2 = (nibble >> 2) & 1
    d3 = (nibble >> 3) & 1

    if cr == 1:
        p0 = d0 ^ d1 ^ d2 ^ d3
        return (nibble << 1) | p0

    if cr == 2:
        p0 = d0 ^ d1 ^ d2
        p1 = d1 ^ d2 ^ d3
        return (nibble << 2) | (p1 << 1) | p0

    # CR 3 and 4 share the Hamming(7,4) parity bits
    p0 = d0 ^ d1 ^ d3
    p1 = d0 ^ d2 ^ d3
    p2 = d1 ^ d2 ^ d3

    if cr == 3:
        return (nibble << 3) | (p2 << 2) | (p1 << 1) | p0

    # CR == 4: extended Hamming — overall parity bit
    p3 = d0 ^ d1 ^ d2 ^ d3 ^ p0 ^ p1 ^ p2
    return (nibble << 4) | (p3 << 3) | (p2 << 2) | (p1 << 1) | p0


# Syndrome → codeword bit position for Hamming(7,4).
# Codeword bits: [d3(6) d2(5) d1(4) d0(3) p2(2) p1(1) p0(0)]
_HAMMING74_POS = {1: 0, 2: 1, 3: 3, 4: 2, 5: 4, 6: 5, 7: 6}

# Extended Hamming(8,4) — same Hamming core, p3 at bit 3.
# Codeword bits: [d3(7) d2(6) d1(5) d0(4) p3(3) p2(2) p1(1) p0(0)]
_HAMMING84_POS = {1: 0, 2: 1, 3: 4, 4: 2, 5: 5, 6: 6, 7: 7}


def hamming_decode(codeword: int, cr: int) -> tuple[int, int]:
    """Decode a (4+cr)-bit codeword → (nibble, error_count).

    error_count: 0 = clean, 1 = corrected, -1 = uncorrectable (detected).
    """
    if cr not in _CR_RANGE:
        raise ValueError(f"cr must be 1–4, got {cr}")
    nibble = (codeword >> cr) & 0xF
    d0 = (nibble >> 0) & 1
    d1 = (nibble >> 1) & 1
    d2 = (nibble >> 2) & 1
    d3 = (nibble >> 3) & 1

    if cr == 1:
        p0 = codeword & 1
        if p0 != (d0 ^ d1 ^ d2 ^ d3):
            return nibble, -1
        return nibble, 0

    if cr == 2:
        p0 = (codeword >> 0) & 1
        p1 = (codeword >> 1) & 1
        s0 = d0 ^ d1 ^ d2 ^ p0
        s1 = d1 ^ d2 ^ d3 ^ p1
        if s0 or s1:
            return nibble, -1
        return nibble, 0

    # CR 3 and 4: Hamming(7,4) syndrome
    p0 = (codeword >> 0) & 1
    p1 = (codeword >> 1) & 1
    p2 = (codeword >> 2) & 1
    s0 = d0 ^ d1 ^ d3 ^ p0
    s1 = d0 ^ d2 ^ d3 ^ p1
    s2 = d1 ^ d2 ^ d3 ^ p2
    syndrome = (s2 << 2) | (s1 << 1) | s0

    if cr == 3:
        if syndrome == 0:
            return nibble, 0
        pos = _HAMMING74_POS.get(syndrome)
        if pos is not None:
            corrected = codeword ^ (1 << pos)
            return (corrected >> 3) & 0xF, 1
        return nibble, -1

    # CR == 4: extended Hamming — overall parity check
    p3 = (codeword >> 3) & 1
    overall = d0 ^ d1 ^ d2 ^ d3 ^ p0 ^ p1 ^ p2 ^ p3
    if syndrome == 0 and overall == 0:
        return nibble, 0
    if syndrome == 0 and overall == 1:
        return nibble, 1  # only p3 flipped — data intact
    if syndrome != 0 and overall == 1:
        pos = _HAMMING84_POS.get(syndrome)
        if pos is not None:
            corrected = codeword ^ (1 << pos)
            return (corrected >> 4) & 0xF, 1
        return nibble, -1
    # syndrome != 0, overall == 0 → double error
    return nibble, -1


# ---------------------------------------------------------------------------
# Block interleaver
#
# A block of (4+cr) symbols, each rdd bits wide, carries rdd codewords
# of (4+cr) bits each.  rdd = SF for standard rate, SF-2 for reduced rate
# (header / low-data-rate).
#
# Encode: write codewords into rows of an rdd × ppm bit matrix, rotate
# each row i left by i positions, read columns as symbols.
# Decode: write symbols into columns, rotate each row right by i, read
# rows as codewords.
# ---------------------------------------------------------------------------


def _rotate_left(val: int, k: int, width: int) -> int:
    k %= width
    if k == 0:
        return val
    mask = (1 << width) - 1
    return ((val << k) | (val >> (width - k))) & mask


def _rotate_right(val: int, k: int, width: int) -> int:
    return _rotate_left(val, width - (k % width), width)


def interleave(
    codewords: list[int],
    sf: int,
    cr: int,
    reduced: bool = False,
) -> list[int]:
    """Interleave *rdd* codewords → *ppm* symbols."""
    rdd = (sf - 2) if reduced else sf
    ppm = 4 + cr
    if len(codewords) != rdd:
        raise ValueError(f"expected {rdd} codewords, got {len(codewords)}")

    # Rotate rows
    rotated = [_rotate_left(codewords[i], i, ppm) for i in range(rdd)]

    # Read columns (row 0 contributes MSB of each symbol)
    symbols: list[int] = []
    for col in range(ppm):
        sym = 0
        for row in range(rdd):
            bit = (rotated[row] >> (ppm - 1 - col)) & 1
            sym = (sym << 1) | bit
        symbols.append(sym)
    return symbols


def deinterleave(
    symbols: list[int],
    sf: int,
    cr: int,
    reduced: bool = False,
) -> list[int]:
    """De-interleave *ppm* symbols → *rdd* codewords."""
    rdd = (sf - 2) if reduced else sf
    ppm = 4 + cr
    if len(symbols) != ppm:
        raise ValueError(f"expected {ppm} symbols, got {len(symbols)}")

    # Write symbols into columns (MSB of symbol → row 0)
    rows: list[int] = [0] * rdd
    for col in range(ppm):
        for row in range(rdd):
            bit = (symbols[col] >> (rdd - 1 - row)) & 1
            rows[row] |= bit << (ppm - 1 - col)

    # Rotate each row right by its index
    return [_rotate_right(rows[i], i, ppm) for i in range(rdd)]


# ---------------------------------------------------------------------------
# Whitening — LFSR pseudo-random XOR sequence
#
# Uses a 9-bit LFSR (x^9 + x^4 + 1, primitive, period 511).  The exact
# polynomial may need adjustment when validating against real LoRa hardware
# — the round-trip property (whiten(whiten(x)) == x) holds regardless.
# ---------------------------------------------------------------------------

_LFSR_POLY_TAPS = (8, 3)  # bit positions for x^9 + x^4 + 1
_LFSR_INIT = 0x1FF


def _lfsr_bytes(n_bytes: int, init: int = _LFSR_INIT) -> bytes:
    """Generate *n_bytes* of LFSR pseudo-random sequence."""
    state = init
    out = bytearray(n_bytes)
    for i in range(n_bytes):
        byte = 0
        for _ in range(8):
            byte = (byte << 1) | ((state >> _LFSR_TAPS[0]) & 1)
            fb = ((state >> _LFSR_TAPS[0]) ^ (state >> _LFSR_TAPS[1])) & 1
            state = ((state << 1) | fb) & 0x1FF
        out[i] = byte
    return bytes(out)


# Tap indices used by _lfsr_bytes — module-level for clarity.
_LFSR_TAPS = _LFSR_POLY_TAPS


def whiten(data: bytes, offset: int = 0) -> bytes:
    """XOR *data* with the whitening sequence starting at byte *offset*."""
    seq = _lfsr_bytes(offset + len(data))
    return bytes(d ^ seq[offset + i] for i, d in enumerate(data))


# ---------------------------------------------------------------------------
# CRC-16/CCITT  (poly 0x1021, init 0xFFFF)
# ---------------------------------------------------------------------------


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc
