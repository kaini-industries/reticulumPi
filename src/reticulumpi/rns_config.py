"""Line-preserving parser for Reticulum config files.

Reticulum uses an INI-like format with ``[[double bracket]]`` sub-sections
for interfaces.  Python's :mod:`configparser` cannot represent this nesting
and drops comments on round-trip, so we use a simple line-based parser that
preserves every byte of the original file except the specific values we
modify.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field

_SECTION_RE = re.compile(r"^\[([^\[\]]+)\]\s*$")
_SUBSECTION_RE = re.compile(r"^\s*\[\[(.+)\]\]\s*$")
_KV_RE = re.compile(r"^\s+(\w[\w\s]*\w|\w)\s*=\s*(.*?)\s*$")
_ENABLED_TRUE = {"yes", "true", "1", "on"}
_ENABLED_FALSE = {"no", "false", "0", "off"}


@dataclass
class InterfaceEntry:
    """Parsed representation of one ``[[InterfaceName]]`` block."""

    name: str
    iface_type: str = ""
    enabled: bool = True
    start_line: int = 0
    enabled_line: int = -1
    properties: dict[str, str] = field(default_factory=dict)


def parse_rns_config(path: str) -> tuple[list[str], list[InterfaceEntry]]:
    """Read and parse a Reticulum config file.

    Returns ``(lines, interfaces)`` where *lines* is every raw line of the
    file (for later round-trip writing) and *interfaces* is the list of
    parsed ``[[InterfaceName]]`` blocks found under ``[interfaces]``.
    """
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.readlines()

    interfaces: list[InterfaceEntry] = []
    in_interfaces_section = False
    current: InterfaceEntry | None = None

    for idx, raw in enumerate(lines):
        line = raw.rstrip("\n\r")

        # Top-level [section]
        m = _SECTION_RE.match(line)
        if m:
            _finish(current, interfaces)
            current = None
            in_interfaces_section = m.group(1).strip().lower() == "interfaces"
            continue

        if not in_interfaces_section:
            continue

        # [[SubSection]] inside [interfaces]
        m = _SUBSECTION_RE.match(line)
        if m:
            _finish(current, interfaces)
            current = InterfaceEntry(name=m.group(1).strip(), start_line=idx)
            continue

        # key = value inside a [[subsection]]
        if current is not None:
            m = _KV_RE.match(line)
            if m:
                key = m.group(1).strip().lower()
                val = m.group(2).strip()
                current.properties[key] = val
                if key == "type":
                    current.iface_type = val
                elif key == "enabled":
                    if val.lower() in _ENABLED_TRUE:
                        current.enabled = True
                    elif val.lower() in _ENABLED_FALSE:
                        current.enabled = False
                    current.enabled_line = idx

    _finish(current, interfaces)
    return lines, interfaces


def parse_rns_config_from_lines(
    lines: list[str],
) -> tuple[list[str], list[InterfaceEntry]]:
    """Like :func:`parse_rns_config` but operates on an in-memory line list."""
    interfaces: list[InterfaceEntry] = []
    in_interfaces_section = False
    current: InterfaceEntry | None = None

    for idx, raw in enumerate(lines):
        line = raw.rstrip("\n\r")
        m = _SECTION_RE.match(line)
        if m:
            _finish(current, interfaces)
            current = None
            in_interfaces_section = m.group(1).strip().lower() == "interfaces"
            continue
        if not in_interfaces_section:
            continue
        m = _SUBSECTION_RE.match(line)
        if m:
            _finish(current, interfaces)
            current = InterfaceEntry(name=m.group(1).strip(), start_line=idx)
            continue
        if current is not None:
            m = _KV_RE.match(line)
            if m:
                key = m.group(1).strip().lower()
                val = m.group(2).strip()
                current.properties[key] = val
                if key == "type":
                    current.iface_type = val
                elif key == "enabled":
                    if val.lower() in _ENABLED_TRUE:
                        current.enabled = True
                    elif val.lower() in _ENABLED_FALSE:
                        current.enabled = False
                    current.enabled_line = idx
    _finish(current, interfaces)
    return lines, interfaces


def set_interface_enabled(
    lines: list[str], entry: InterfaceEntry, enabled: bool
) -> list[str]:
    """Toggle an interface's ``enabled`` flag in *lines*, returning a new list."""
    lines = list(lines)  # shallow copy
    val = "yes" if enabled else "no"
    if entry.enabled_line >= 0:
        old = lines[entry.enabled_line]
        # Replace only the value portion, preserving indentation and key
        lines[entry.enabled_line] = re.sub(
            r"(enabled\s*=\s*)\S+", rf"\g<1>{val}", old, flags=re.IGNORECASE
        )
    else:
        # No enabled line exists — insert one after the [[Name]] line
        indent = _detect_indent(lines, entry.start_line)
        lines.insert(entry.start_line + 1, f"{indent}enabled = {val}\n")
    return lines


def set_interface_property(
    lines: list[str], entry: InterfaceEntry, key: str, value: str
) -> list[str]:
    """Set or update a property in an interface block, returning a new list.

    If the property already exists, its value is replaced in-place.
    If it does not exist, it is inserted after the last existing property.
    """
    lines = list(lines)
    key_lower = key.lower()

    # Search for existing key within this interface's line range
    end_line = _interface_end_line(lines, entry.start_line)
    for idx in range(entry.start_line + 1, end_line):
        m = _KV_RE.match(lines[idx])
        if m and m.group(1).strip().lower() == key_lower:
            # Replace value in-place, preserving indentation and key name
            indent = lines[idx][: lines[idx].index(m.group(1))]
            lines[idx] = f"{indent}{m.group(1).strip()} = {value}\n"
            return lines

    # Key not found — insert after last property line
    indent = _detect_indent(lines, entry.start_line)
    insert_at = entry.start_line + 1
    for idx in range(entry.start_line + 1, end_line):
        if _KV_RE.match(lines[idx]):
            insert_at = idx + 1
    lines.insert(insert_at, f"{indent}{key} = {value}\n")
    return lines


def remove_interface_property(
    lines: list[str], entry: InterfaceEntry, key: str
) -> list[str]:
    """Remove a property line from an interface block, returning a new list."""
    lines = list(lines)
    key_lower = key.lower()
    end_line = _interface_end_line(lines, entry.start_line)
    for idx in range(entry.start_line + 1, end_line):
        m = _KV_RE.match(lines[idx])
        if m and m.group(1).strip().lower() == key_lower:
            del lines[idx]
            return lines
    return lines


def _interface_end_line(lines: list[str], start_line: int) -> int:
    """Find where an interface block ends (next [[section]] or [section] or EOF)."""
    for idx in range(start_line + 1, len(lines)):
        if _SUBSECTION_RE.match(lines[idx]) or _SECTION_RE.match(lines[idx]):
            return idx
    return len(lines)


def add_interface_section(
    lines: list[str],
    name: str,
    iface_type: str,
    properties: dict[str, str],
) -> list[str]:
    """Append a new ``[[InterfaceName]]`` block at the end of ``[interfaces]``.

    Returns a new list of lines.
    """
    lines = list(lines)
    insert_idx = _find_interfaces_end(lines)

    block = ["\n", f"[[{name}]]\n", f"  type = {iface_type}\n", "  enabled = yes\n"]
    for key, val in properties.items():
        if key.lower() not in ("type", "enabled"):
            block.append(f"  {key} = {val}\n")

    for i, bline in enumerate(block):
        lines.insert(insert_idx + i, bline)
    return lines


def write_rns_config(path: str, lines: list[str]) -> None:
    """Atomically write *lines* back to *path*."""
    dir_name = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=dir_name, prefix=".rns_config_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── helpers ──────────────────────────────────────────────────────────


def _finish(entry: InterfaceEntry | None, out: list[InterfaceEntry]) -> None:
    if entry is not None:
        out.append(entry)


def _detect_indent(lines: list[str], after_line: int) -> str:
    """Return the indentation used for keys near *after_line*."""
    for i in range(after_line + 1, min(after_line + 5, len(lines))):
        m = _KV_RE.match(lines[i])
        if m:
            return lines[i][: lines[i].index(m.group(1))]
    return "    "


def _find_interfaces_end(lines: list[str]) -> int:
    """Find the line index where a new interface block should be inserted."""
    in_interfaces = False
    last_content = len(lines)
    for idx, raw in enumerate(lines):
        line = raw.rstrip("\n\r")
        m = _SECTION_RE.match(line)
        if m:
            if in_interfaces:
                return idx  # next top-level section starts here
            if m.group(1).strip().lower() == "interfaces":
                in_interfaces = True
                last_content = idx + 1
            continue
        if in_interfaces and line.strip():
            last_content = idx + 1
    return last_content
