"""Tests for reticulumpi.mtu truncation helpers."""

from reticulumpi.mtu import truncate_bytes, truncate_for_mtu


class TestTruncateForMtu:
    def test_short_message_not_truncated(self):
        result = truncate_for_mtu("[LXMF] sender:\n", "Hi", 237)
        assert result == "[LXMF] sender:\nHi"
        assert " ..." not in result

    def test_exact_boundary_not_truncated(self):
        header = "H:"
        body = "x" * (237 - len(header.encode("utf-8")))
        result = truncate_for_mtu(header, body, 237)
        assert len(result.encode("utf-8")) == 237
        assert " ..." not in result

    def test_long_message_truncated_with_ellipsis(self):
        header = "H:\n"
        body = "A" * 300
        result = truncate_for_mtu(header, body, 237)
        assert len(result.encode("utf-8")) <= 237
        assert result.endswith(" ...")

    def test_truncation_respects_utf8(self):
        header = "H:"
        body = "\u00e9" * 200  # 2 bytes each in UTF-8
        result = truncate_for_mtu(header, body, 237)
        result.encode("utf-8")  # Should not raise
        assert len(result.encode("utf-8")) <= 237

    def test_header_exceeds_mtu(self):
        header = "X" * 250
        result = truncate_for_mtu(header, "body", 237)
        assert result == header


class TestTruncateBytes:
    def test_short_passes_through(self):
        assert truncate_bytes("hello", 233) == "hello"

    def test_at_exact_boundary(self):
        text = "A" * 233
        result = truncate_bytes(text, 233)
        assert result == text
        assert len(result.encode("utf-8")) == 233

    def test_splits_multibyte_safely(self):
        # "é" is 2 bytes; with max_bytes=5 the third "é" would straddle the
        # boundary and must be dropped, leaving 4 bytes.
        result = truncate_bytes("\u00e9\u00e9\u00e9", 5)
        assert result == "\u00e9\u00e9"
        assert len(result.encode("utf-8")) == 4

    def test_empty_string(self):
        assert truncate_bytes("", 233) == ""
