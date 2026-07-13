#!/usr/bin/env python3
"""Backport CPython's CVE-2026-15308 HTMLParser fix, or fail closed.

This script accepts only the pristine ``Lib/html/parser.py`` shipped with
CPython 3.14.6. It applies the parser changes from CPython commit
07efb08123ba9367a7107325adb9d5626dca1ca9 and verifies the complete output
before replacing the input file.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import stat
import tempfile


UPSTREAM_COMMIT = "07efb08123ba9367a7107325adb9d5626dca1ca9"
UPSTREAM_URL = f"https://github.com/python/cpython/commit/{UPSTREAM_COMMIT}"
EXPECTED_BEFORE_SHA256 = "b8393a95226ab2d01024e5c9f78e3a83cf0b97b22d5be48f90ef6c0fc1bbb80b"
EXPECTED_AFTER_SHA256 = "951b46301862483dbcb3debbbd39b4cef3b85ebe488f86cc2ff667f834dfe523"


_RESET_BEFORE = b"""\
        self._support_cdata = True
        self._escapable = True
        super().reset()
"""

_RESET_AFTER = b"""\
        self._support_cdata = True
        self._escapable = True
        self._pending = []
        self._pending_len = 0
        self._parse_threshold = 1
        super().reset()
"""

_FEED_BEFORE = b"""\
        self.rawdata = self.rawdata + data
        self.goahead(0)
"""

_FEED_AFTER = b"""\
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

_CLOSE_BEFORE = b"""\
    def close(self):
        \"\"\"Handle any buffered data.\"\"\"
        self.goahead(1)
"""

_CLOSE_AFTER = b"""\
    def close(self):
        \"\"\"Handle any buffered data.\"\"\"
        if self._pending:
            self.rawdata += ''.join(self._pending)
            self._pending.clear()
            self._pending_len = 0
        self.goahead(1)
"""


class PatchError(RuntimeError):
    """Raised when the input is not the exact supported CPython source."""


def _sha256(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def _replace_once(contents: bytes, before: bytes, after: bytes, label: str) -> bytes:
    occurrences = contents.count(before)
    if occurrences != 1:
        raise PatchError(f"expected exactly one {label} patch location; found {occurrences}")
    return contents.replace(before, after, 1)


def build_patched_parser(contents: bytes) -> bytes:
    """Return the verified postimage for the exact CPython 3.14.6 preimage."""
    actual_before = _sha256(contents)
    if actual_before != EXPECTED_BEFORE_SHA256:
        detail = (
            "input is already patched"
            if actual_before == EXPECTED_AFTER_SHA256
            else "input does not match pristine CPython 3.14.6"
        )
        raise PatchError(f"{detail}: expected SHA256 {EXPECTED_BEFORE_SHA256}, got {actual_before}")

    patched = _replace_once(contents, _RESET_BEFORE, _RESET_AFTER, "HTMLParser.reset")
    patched = _replace_once(patched, _FEED_BEFORE, _FEED_AFTER, "HTMLParser.feed")
    patched = _replace_once(patched, _CLOSE_BEFORE, _CLOSE_AFTER, "HTMLParser.close")

    actual_after = _sha256(patched)
    if actual_after != EXPECTED_AFTER_SHA256:
        raise PatchError(
            f"patched output hash mismatch: expected SHA256 "
            f"{EXPECTED_AFTER_SHA256}, got {actual_after}"
        )
    return patched


def _replace_atomically(target: Path, contents: bytes) -> None:
    mode = stat.S_IMODE(target.stat().st_mode)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    descriptor_open = True
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as output:
            descriptor_open = False
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        if descriptor_open:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def patch_file(target: Path) -> None:
    if target.is_symlink() or not target.is_file():
        raise PatchError(f"target must be an existing regular file: {target}")
    patched = build_patched_parser(target.read_bytes())
    _replace_atomically(target, patched)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Apply CPython's CVE-2026-15308 HTMLParser fix to the exact "
            "Python 3.14.6 Lib/html/parser.py preimage."
        )
    )
    parser.add_argument("parser_path", type=Path, help="path to Lib/html/parser.py")
    return parser


def main() -> int:
    parser = _argument_parser()
    arguments = parser.parse_args()
    try:
        patch_file(arguments.parser_path)
    except (OSError, PatchError) as error:
        parser.exit(1, f"error: {error}\n")
    print(
        f"patched {arguments.parser_path}: SHA256 {EXPECTED_BEFORE_SHA256} -> "
        f"{EXPECTED_AFTER_SHA256} ({UPSTREAM_URL})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
