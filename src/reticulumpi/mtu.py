"""UTF-8-safe truncation helpers for mesh transport MTUs.

Meshtastic and MeshCore packets are capped at fixed byte budgets on the wire.
These helpers truncate outgoing text to fit without splitting multi-byte
characters.
"""

from __future__ import annotations

# Meshtastic payload limit (bytes). Matches mesh_pb2.Constants.DATA_PAYLOAD_LEN.
MESHTASTIC_MTU = 233

# MeshCore text payload limit (bytes).
MESHCORE_MTU = 240


def truncate_bytes(text: str, max_bytes: int) -> str:
    """Truncate *text* so its UTF-8 encoding fits within *max_bytes*.

    Never splits a multi-byte character. Returns *text* unchanged if it
    already fits.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def truncate_for_mtu(header: str, body: str, mtu: int) -> str:
    """Build ``header + body`` truncating body UTF-8-safely to fit within *mtu*.

    If the combined length fits, the original strings are concatenated. If
    the body must be cut, a ``" ..."`` ellipsis is appended so recipients
    can see that truncation occurred. If the header alone exceeds the MTU,
    the header is returned as-is (the radio will truncate further).
    """
    header_bytes = len(header.encode("utf-8"))
    max_body_bytes = mtu - header_bytes
    if max_body_bytes <= 0:
        return header

    body_encoded = body.encode("utf-8")
    if len(body_encoded) <= max_body_bytes:
        return header + body

    ellipsis = " ..."
    target = max_body_bytes - len(ellipsis.encode("utf-8"))
    if target <= 0:
        return header + ellipsis

    truncated = body.encode("utf-8")[:target].decode("utf-8", errors="ignore")

    return header + truncated + ellipsis
