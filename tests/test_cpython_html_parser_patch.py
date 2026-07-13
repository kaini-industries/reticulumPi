"""Regression tests for the audited CPython HTMLParser security backport."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PATCH_SCRIPT = ROOT / "docker/security/patch_cpython_html_parser.py"
VEX_DOCUMENT = ROOT / "docker/security/cve-2026-15308.openvex.json"

_SPEC = importlib.util.spec_from_file_location("cpython_html_parser_patch", PATCH_SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
patcher = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(patcher)


RESET_BEFORE = b"""\
        self._support_cdata = True
        self._escapable = True
        super().reset()
"""

RESET_AFTER = b"""\
        self._support_cdata = True
        self._escapable = True
        self._pending = []
        self._pending_len = 0
        self._parse_threshold = 1
        super().reset()
"""

FEED_BEFORE = b"""\
        self.rawdata = self.rawdata + data
        self.goahead(0)
"""

FEED_AFTER = b"""\
        # Accumulate new data in a list and only join and parse it once
        # enough has piled up.  Rescanning an unparsed buffer (e.g. an
        # unterminated tag) and concatenating onto it on every call would
        # both be quadratic in the input size.
        self._pending_len += len(data)
        if self._pending_len < self._parse_threshold:
            self._pending.append(data)
        else:
            if not self._pending:
                self.rawdata += data
            else:
                self._pending.append(data)
                self.rawdata += ''.join(self._pending)
                self._pending.clear()
            self._pending_len = 0
            n = len(self.rawdata)
            self.goahead(0)
            if len(self.rawdata) < n:
                # Some data was parsed; resume on the next call.
                self._parse_threshold = 1
            else:
                # Nothing was parsed; wait until the buffer doubles.
                self._parse_threshold = len(self.rawdata)
"""

CLOSE_BEFORE = b"""\
    def close(self):
        \"\"\"Handle any buffered data.\"\"\"
        self.goahead(1)
"""

CLOSE_AFTER = b"""\
    def close(self):
        \"\"\"Handle any buffered data.\"\"\"
        if self._pending:
            self.rawdata += ''.join(self._pending)
            self._pending.clear()
            self._pending_len = 0
        self.goahead(1)
"""


def _sha256(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def test_backport_reproduces_the_exact_verified_upstream_hunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = b"prefix\n" + RESET_BEFORE + FEED_BEFORE + CLOSE_BEFORE + b"suffix\n"
    expected = b"prefix\n" + RESET_AFTER + FEED_AFTER + CLOSE_AFTER + b"suffix\n"
    monkeypatch.setattr(patcher, "EXPECTED_BEFORE_SHA256", _sha256(source))
    monkeypatch.setattr(patcher, "EXPECTED_AFTER_SHA256", _sha256(expected))

    assert patcher.build_patched_parser(source) == expected
    with pytest.raises(patcher.PatchError, match="input is already patched"):
        patcher.build_patched_parser(expected)


def test_audited_python_3146_full_file_hashes_are_pinned() -> None:
    assert patcher.EXPECTED_BEFORE_SHA256 == (
        "b8393a95226ab2d01024e5c9f78e3a83cf0b97b22d5be48f90ef6c0fc1bbb80b"
    )
    assert patcher.EXPECTED_AFTER_SHA256 == (
        "951b46301862483dbcb3debbbd39b4cef3b85ebe488f86cc2ff667f834dfe523"
    )


def test_openvex_statement_matches_the_audited_backport() -> None:
    document = json.loads(VEX_DOCUMENT.read_text(encoding="utf-8"))
    statement = document["statements"][0]

    assert document["@context"] == "https://openvex.dev/ns/v0.2.0"
    assert statement["vulnerability"]["name"] == "CVE-2026-15308"
    assert statement["products"] == [{"@id": "pkg:generic/python@3.14.6"}]
    assert statement["status"] == "fixed"
    assert patcher.UPSTREAM_COMMIT in statement["status_notes"]
    assert patcher.EXPECTED_BEFORE_SHA256 in statement["status_notes"]
    assert patcher.EXPECTED_AFTER_SHA256 in statement["status_notes"]
