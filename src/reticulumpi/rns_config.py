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
from collections.abc import Collection
from dataclasses import dataclass, field

from RNS.vendor.configobj import ConfigObj, ConfigObjError, Section

_SECTION_RE = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")
_SUBSECTION_RE = re.compile(r"^\s*\[\[([^\[\]]+)\]\]\s*(?:#.*)?$")
_NESTED_SUBSECTION_RE = re.compile(r"^\s*\[{3,}.*\]{3,}\s*(?:#.*)?$")
_KV_RE = re.compile(r"^\s*(\w[\w\s]*\w|\w)\s*=\s*(.*?)\s*$")
_ENABLED_TRUE = {"yes", "true", "1", "on"}


@dataclass
class InterfaceEntry:
    """Parsed representation of one ``[[InterfaceName]]`` block."""

    name: str
    iface_type: str = ""
    enabled: bool = False
    start_line: int = 0
    enabled_line: int = -1
    properties: dict[str, str] = field(default_factory=dict)
    enabled_lines: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class RNSSerialInterfaceConfig:
    """One enabled built-in RNS interface with an exact serial endpoint."""

    name: str
    iface_type: str
    port: str


class RNSConfigError(ValueError):
    """A syntactically valid-looking RNS config is unsafe to reserve from."""


def parse_rns_config(path: str) -> tuple[list[str], list[InterfaceEntry]]:
    """Read and parse a Reticulum config file.

    Returns ``(lines, interfaces)`` where *lines* is every raw line of the
    file (for later round-trip writing) and *interfaces* is the list of
    parsed ``[[InterfaceName]]`` blocks found under ``[interfaces]``.
    """
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    return parse_rns_config_from_lines(lines)


def parse_rns_config_from_lines(
    lines: list[str],
) -> tuple[list[str], list[InterfaceEntry]]:
    """Like :func:`parse_rns_config` but operates on an in-memory line list."""
    interfaces: list[InterfaceEntry] = []
    in_interfaces_section = False
    in_nested_subsection = False
    current: InterfaceEntry | None = None

    for idx, raw in enumerate(lines):
        line = raw.rstrip("\n\r")
        m = _SECTION_RE.match(line)
        if m:
            _finish(current, interfaces)
            current = None
            in_nested_subsection = False
            in_interfaces_section = _parse_configobj_scalar(m.group(1)).casefold() == "interfaces"
            continue
        if not in_interfaces_section:
            continue
        m = _SUBSECTION_RE.match(line)
        if m:
            _finish(current, interfaces)
            current = InterfaceEntry(name=_parse_configobj_scalar(m.group(1)), start_line=idx)
            in_nested_subsection = False
            continue
        if _NESTED_SUBSECTION_RE.match(line):
            # ConfigObj uses three-or-more bracket levels for child sections.
            # Their keys belong to the parent interface's subconfiguration,
            # not to the top-level interface itself.
            in_nested_subsection = True
            continue
        if current is not None and not in_nested_subsection:
            m = _KV_RE.match(line)
            if m:
                key = m.group(1).strip().lower()
                val = _parse_configobj_scalar(m.group(2))
                current.properties[key] = val
                if key == "type":
                    current.iface_type = val
                elif key in {"enabled", "interface_enabled"}:
                    if val.lower() in _ENABLED_TRUE:
                        current.enabled = True
                    current.enabled_lines.append(idx)
                    if current.enabled_line < 0 or key == "enabled":
                        current.enabled_line = idx
    _finish(current, interfaces)
    return lines, interfaces


def parse_enabled_rns_serial_interfaces(
    path: str,
    serial_interface_types: Collection[str],
) -> list[RNSSerialInterfaceConfig]:
    """Parse enabled built-in serial interfaces with RNS 1.3.8 semantics.

    The editor parser above deliberately retains raw lines. Reservation safety
    needs the values RNS will actually consume, including ConfigObj quoting,
    comments, interpolation, list handling, and the ``interface_enabled`` alias.
    Invalid enable flags or missing/non-scalar ports on an enabled serial
    interface raise instead of leaving a plugin/RNS ownership gap.
    """

    interface_types = frozenset(serial_interface_types)
    if not interface_types or any(
        not isinstance(value, str) or not value for value in interface_types
    ):
        raise ValueError("serial_interface_types must contain non-empty strings")

    # ConfigObj raises a generic OSError for a missing file. Preserve the
    # caller-visible FileNotFoundError distinction used for RNS's optional
    # default config, while any later disappearance remains a fail-closed race.
    os.stat(path)
    try:
        config = ConfigObj(path, file_error=True)
    except ConfigObjError as exc:
        raise RNSConfigError(f"Could not parse RNS config {path!r}: {exc}") from exc

    interfaces = config.get("interfaces")
    if interfaces is None:
        return []
    if not isinstance(interfaces, Section):
        raise RNSConfigError("RNS [interfaces] must be a section")

    serial_interfaces: list[RNSSerialInterfaceConfig] = []
    for name in interfaces:
        section = interfaces[name]
        if not isinstance(section, Section):
            continue
        try:
            iface_type = section.get("type")
        except (ConfigObjError, TypeError) as exc:
            raise RNSConfigError(f"Could not resolve RNS interface {name!r} type") from exc
        if not isinstance(iface_type, str) or iface_type not in interface_types:
            continue

        try:
            enabled = _configobj_interface_enabled(section)
        except (ConfigObjError, ValueError) as exc:
            raise RNSConfigError(
                f"RNS serial interface {name!r} has an invalid enabled flag"
            ) from exc
        if not enabled:
            continue

        try:
            port = section.get("port")
        except (ConfigObjError, TypeError) as exc:
            raise RNSConfigError(
                f"Enabled RNS serial interface {name!r} has an unresolved port"
            ) from exc
        if not isinstance(port, str) or not port or "\x00" in port:
            raise RNSConfigError(
                f"Enabled RNS serial interface {name!r} must have one non-empty scalar port"
            )
        serial_interfaces.append(RNSSerialInterfaceConfig(str(name), iface_type, port))

    return serial_interfaces


def set_interface_enabled(lines: list[str], entry: InterfaceEntry, enabled: bool) -> list[str]:
    """Toggle an interface's ``enabled`` flag in *lines*, returning a new list."""
    lines = list(lines)  # shallow copy
    val = "yes" if enabled else "no"
    enabled_lines = list(dict.fromkeys(entry.enabled_lines))
    if not enabled_lines and entry.enabled_line >= 0:
        enabled_lines = [entry.enabled_line]
    if enabled_lines:
        # RNS enables when either ``enabled`` or ``interface_enabled`` is true,
        # so update every existing alias to make disabling unambiguous.
        for line_number in enabled_lines:
            lines[line_number] = _replace_property_value(lines[line_number], val)
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
    for idx in _direct_property_lines(lines, entry.start_line, end_line):
        m = _KV_RE.match(lines[idx])
        if m and m.group(1).strip().lower() == key_lower:
            lines[idx] = _replace_property_value(lines[idx], value)
            return lines

    # Key not found — insert after last property line
    indent = _detect_indent(lines, entry.start_line)
    insert_at = entry.start_line + 1
    for idx in _direct_property_lines(lines, entry.start_line, end_line):
        if _KV_RE.match(lines[idx]):
            insert_at = idx + 1
    lines.insert(insert_at, f"{indent}{key} = {value}\n")
    return lines


def remove_interface_property(lines: list[str], entry: InterfaceEntry, key: str) -> list[str]:
    """Remove a property line from an interface block, returning a new list."""
    lines = list(lines)
    key_lower = key.lower()
    end_line = _interface_end_line(lines, entry.start_line)
    for idx in _direct_property_lines(lines, entry.start_line, end_line):
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
    """Atomically write *lines* back to *path*, preserving original permissions."""
    dir_name = os.path.dirname(path) or "."
    # Preserve original file permissions if the file exists
    orig_mode = None
    try:
        orig_mode = os.stat(path).st_mode
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=dir_name, prefix=".rns_config_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        os.replace(tmp, path)
        if orig_mode is not None:
            os.chmod(path, orig_mode)
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


def _parse_configobj_scalar(raw: str) -> str:
    """Return ConfigObj's scalar interpretation without changing source lines."""

    try:
        parsed = ConfigObj([f"__reticulumpi_value__ = {raw}"], interpolation=False)
        value = parsed["__reticulumpi_value__"]
    except (ConfigObjError, KeyError):
        return raw.strip()
    # Lists are not meaningful for the editor's scalar property model. Keeping
    # their source spelling also prevents a list-valued type/flag from being
    # mistaken for a valid RNS scalar.
    return value if isinstance(value, str) else raw.strip()


def _configobj_interface_enabled(section: Section) -> bool:
    """Mirror the short-circuit enable predicate in RNS 1.3.8 exactly."""

    if "interface_enabled" in section and section.as_bool("interface_enabled") is True:
        return True
    return "enabled" in section and section.as_bool("enabled") is True


def _direct_property_lines(lines: list[str], start_line: int, end_line: int):
    """Yield direct property lines, excluding ConfigObj child sections."""

    for idx in range(start_line + 1, end_line):
        if _NESTED_SUBSECTION_RE.match(lines[idx]):
            break
        if _KV_RE.match(lines[idx]):
            yield idx


def _replace_property_value(line: str, value: str) -> str:
    """Replace a ConfigObj value while preserving spacing and inline comments."""

    equals = line.find("=")
    if equals < 0:
        return line
    value_start = equals + 1
    while value_start < len(line) and line[value_start] in " \t":
        value_start += 1

    quote: str | None = None
    escaped = False
    comment_start: int | None = None
    for idx in range(value_start, len(line)):
        character = line[idx]
        if character in "\r\n":
            break
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote is not None:
            escaped = True
            continue
        if quote is not None:
            if character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
        elif character == "#":
            comment_start = idx
            break

    content_end = comment_start if comment_start is not None else len(line.rstrip("\r\n"))
    while content_end > value_start and line[content_end - 1] in " \t":
        content_end -= 1
    return f"{line[:value_start]}{value}{line[content_end:]}"


def _detect_indent(lines: list[str], after_line: int) -> str:
    """Return the indentation used for keys near *after_line*."""
    end_line = _interface_end_line(lines, after_line)
    for i in _direct_property_lines(lines, after_line, min(end_line, after_line + 5)):
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
