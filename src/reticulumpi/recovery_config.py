"""Fail-closed, dependency-free projection of migration configuration.

This is intentionally not a general YAML parser.  The recovery administrator
needs only the enabled state and database path fields for the five built-in
plugins with SQLite migrations.  Unsupported YAML features that could change
that projection are rejected instead of being approximated.
"""

from __future__ import annotations

import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from reticulumpi.migration_catalog import MIGRATION_PLUGIN_NAMES


MAX_CONFIG_BYTES = 1024 * 1024


class RecoveryConfigError(ValueError):
    """Raised when migration configuration cannot be projected safely."""


@dataclass(frozen=True)
class _Line:
    number: int
    indentation: int
    text: str


def _read_config(path: Path, *, require_trusted: bool) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RecoveryConfigError(f"configuration is unavailable or unsafe: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RecoveryConfigError(f"configuration must be a regular file: {path}")
        if metadata.st_size > MAX_CONFIG_BYTES:
            raise RecoveryConfigError("configuration exceeds the 1 MiB recovery limit")
        if require_trusted and (metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022):
            raise RecoveryConfigError(
                "production configuration must be root-owned and not group/world-writable"
            )
        chunks: list[bytes] = []
        remaining = MAX_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    if len(payload) > MAX_CONFIG_BYTES:
        raise RecoveryConfigError("configuration exceeds the 1 MiB recovery limit")
    try:
        return payload.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise RecoveryConfigError("configuration is not valid UTF-8") from exc


def _strip_comment(raw: str, number: int) -> str:
    quote = ""
    escaped = False
    index = 0
    while index < len(raw):
        character = raw[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if quote == '"' and character == "\\":
            escaped = True
            index += 1
            continue
        if quote:
            if quote == "'" and character == "'" and raw[index : index + 2] == "''":
                index += 2
                continue
            if character == quote:
                quote = ""
            index += 1
            continue
        if character in {'"', "'"}:
            quote = character
        elif character == "#" and (index == 0 or raw[index - 1] == " "):
            return raw[:index].rstrip(" ")
        index += 1
    if quote:
        raise RecoveryConfigError(f"line {number}: unterminated quoted scalar")
    return raw.rstrip(" ")


def _unquoted_projection(raw: str) -> str:
    projected: list[str] = []
    quote = ""
    escaped = False
    for character in raw:
        if escaped:
            projected.append(" ")
            escaped = False
            continue
        if quote == '"' and character == "\\":
            projected.append(" ")
            escaped = True
            continue
        if quote:
            projected.append(" ")
            if character == quote:
                quote = ""
            continue
        if character in {'"', "'"}:
            quote = character
            projected.append(" ")
        else:
            projected.append(character)
    return "".join(projected)


def _active_lines(text: str) -> list[_Line]:
    active: list[_Line] = []
    for number, raw in enumerate(text.splitlines(), 1):
        if "\t" in _unquoted_projection(raw):
            raise RecoveryConfigError(f"line {number}: unquoted tabs are unsupported")
        content = _strip_comment(raw, number)
        if not content.strip(" "):
            continue
        indentation = len(content) - len(content.lstrip(" "))
        if content[:indentation] != " " * indentation:
            raise RecoveryConfigError(f"line {number}: unsupported indentation")
        plain = _unquoted_projection(content)
        stripped = plain.strip(" ")
        if stripped in {"---", "..."} or stripped.startswith("%YAML"):
            raise RecoveryConfigError(f"line {number}: YAML directives/documents are unsupported")
        if re.search(r"(^|[\s:\[\]{},-])(?:[&*!])[^\s\[\]{},]+", plain):
            raise RecoveryConfigError(
                f"line {number}: YAML anchors, aliases, and tags are unsupported"
            )
        if re.match(r"^\s*<<\s*:", plain):
            raise RecoveryConfigError(f"line {number}: YAML merge keys are unsupported")
        if re.search(r":\s*[|>]\s*$", plain):
            raise RecoveryConfigError(f"line {number}: block scalars are unsupported")
        active.append(_Line(number, indentation, content))
    return active


def _header(line: _Line, key: str) -> str | None:
    entry = _mapping_entry(line)
    if entry is None:
        raise RecoveryConfigError(f"line {line.number}: malformed block mapping")
    if entry[0] != key:
        return None
    return entry[1]


def _quoted_string(value: str, *, label: str, line: int) -> str:
    quote = value[0]
    output: list[str] = []
    index = 1
    simple_escapes = {
        "0": "\0",
        "a": "\a",
        "b": "\b",
        "t": "\t",
        "n": "\n",
        "v": "\v",
        "f": "\f",
        "r": "\r",
        "e": "\x1b",
        " ": " ",
        '"': '"',
        "/": "/",
        "\\": "\\",
        "N": "\x85",
        "_": "\xa0",
        "L": "\u2028",
        "P": "\u2029",
    }
    hexadecimal_lengths = {"x": 2, "u": 4, "U": 8}
    while index < len(value):
        character = value[index]
        if character == quote:
            if quote == "'" and value[index : index + 2] == "''":
                output.append("'")
                index += 2
                continue
            if value[index + 1 :].strip(" "):
                raise RecoveryConfigError(f"line {line}: {label} has trailing content")
            parsed = "".join(output)
            if not parsed:
                raise RecoveryConfigError(f"line {line}: {label} requires a non-empty string")
            return parsed
        if quote == '"' and character == "\\":
            index += 1
            if index >= len(value):
                break
            escape = value[index]
            if escape in simple_escapes:
                output.append(simple_escapes[escape])
                index += 1
                continue
            digits = hexadecimal_lengths.get(escape)
            if digits is not None:
                encoded = value[index + 1 : index + 1 + digits]
                if len(encoded) != digits or re.fullmatch(r"[0-9A-Fa-f]+", encoded) is None:
                    raise RecoveryConfigError(
                        f"line {line}: {label} has an invalid hexadecimal escape"
                    )
                codepoint = int(encoded, 16)
                if codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
                    raise RecoveryConfigError(f"line {line}: {label} has an invalid Unicode escape")
                output.append(chr(codepoint))
                index += digits + 1
                continue
            raise RecoveryConfigError(f"line {line}: {label} has an invalid YAML escape")
        output.append(character)
        index += 1
    raise RecoveryConfigError(f"line {line}: {label} has an unterminated quoted value")


def _mapping_entry(line: _Line) -> tuple[str, str] | None:
    text = line.text.strip(" ")
    quote = ""
    escaped = False
    delimiter = -1
    index = 0
    while index < len(text):
        character = text[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if quote == '"' and character == "\\":
            escaped = True
            index += 1
            continue
        if quote:
            if quote == "'" and character == "'" and text[index : index + 2] == "''":
                index += 2
                continue
            if character == quote:
                quote = ""
            index += 1
            continue
        if character in {'"', "'"}:
            quote = character
        elif character == ":":
            delimiter = index
            break
        index += 1
    if delimiter < 0:
        return None
    if delimiter + 1 < len(text) and text[delimiter + 1] != " ":
        return None
    raw_key = text[:delimiter].strip(" ")
    if not raw_key:
        return None
    if raw_key[0] in {'"', "'"}:
        key = _quoted_string(raw_key, label="mapping key", line=line.number)
    elif re.fullmatch(r"[A-Za-z0-9_-]+", raw_key) is not None:
        key = raw_key
    else:
        return None
    return key, text[delimiter + 1 :].strip(" ")


def _children(lines: list[_Line], start: _Line, *, boundary: int | None = None) -> list[_Line]:
    selected: list[_Line] = []
    for line in lines:
        if line.number <= start.number:
            continue
        if boundary is not None and line.number >= boundary:
            break
        if line.indentation <= start.indentation:
            break
        selected.append(line)
    return selected


def _direct_mapping_children(lines: list[_Line], *, relevant_keys: frozenset[str]) -> list[_Line]:
    """Return direct mapping entries while skipping safe irrelevant sequences.

    YAML permits a block sequence used as a mapping value to be indentationless,
    so its ``-`` indicators can have the same indentation as the owning key.  A
    sequence is irrelevant to the recovery projection only when it immediately
    follows an empty-valued, projection-irrelevant mapping entry.  Everything
    else remains fail-closed.
    """

    if not lines:
        return []
    direct_indentation = min(line.indentation for line in lines)
    mappings: list[_Line] = []
    sequence_owner: str | None = None
    sequence_started = False
    for line in lines:
        if line.indentation > direct_indentation:
            if sequence_owner is not None and not sequence_started:
                sequence_owner = None
            continue
        if line.indentation < direct_indentation:
            raise RecoveryConfigError(f"line {line.number}: malformed block mapping")
        entry = _mapping_entry(line)
        if entry is not None:
            key, raw = entry
            mappings.append(line)
            sequence_owner = key if key not in relevant_keys and not raw.strip(" ") else None
            sequence_started = False
            continue
        if sequence_owner is not None and re.match(r"^-(?: |$)", line.text.strip(" ")):
            sequence_started = True
            continue
        raise RecoveryConfigError(f"line {line.number}: malformed block mapping")
    return mappings


def _one_field(lines: list[_Line], key: str, label: str) -> tuple[_Line, str] | None:
    values = [(line, value) for line in lines if (value := _header(line, key)) is not None]
    if len(values) > 1:
        raise RecoveryConfigError(f"{label}.{key} is configured more than once")
    return values[0] if values else None


def _require_scalar_leaf(block: list[_Line], line: _Line, label: str) -> None:
    if _children(block, line):
        raise RecoveryConfigError(
            f"line {line.number}: {label} may not have nested or continued content"
        )


def _plain_scalar_is_non_string(value: str) -> bool:
    lowered = value.lower()
    if lowered in {
        "yes",
        "no",
        "true",
        "false",
        "on",
        "off",
        "null",
        "~",
        ".nan",
        ".inf",
        "+.inf",
        "-.inf",
    }:
        return True
    if re.fullmatch(
        r"[-+]?(?:0b[01_]+|0x[0-9a-fA-F_]+|0o?[0-7_]+|[0-9][0-9_]*)",
        value,
    ):
        return True
    if re.fullmatch(
        r"[-+]?(?:(?:(?:[0-9][0-9_]*)?\.[0-9_]+|[0-9][0-9_]*\.[0-9_]*)"
        r"(?:[eE][-+]?[0-9]+)?|[0-9][0-9_]*[eE][-+]?[0-9]+)",
        value,
    ):
        return True
    if re.fullmatch(r"[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+(?:\.[0-9_]*)?", value):
        return True
    return re.fullmatch(r"[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:[Tt ]\S.*)?", value) is not None


def _validate_scalar_string(value: str, *, label: str, line: int) -> str:
    if any(
        unicodedata.category(character) in {"Cc", "Cs"} or character in {"\u2028", "\u2029"}
        for character in value
    ):
        raise RecoveryConfigError(f"line {line}: {label} contains an unsafe control character")
    return value


def _scalar_string(raw: str, *, label: str, line: int) -> str:
    value = raw.strip(" ")
    if not value:
        raise RecoveryConfigError(f"line {line}: {label} requires a scalar string")
    if value[0] in {'"', "'"}:
        return _validate_scalar_string(
            _quoted_string(value, label=label, line=line),
            label=label,
            line=line,
        )
    if (
        any(character.isspace() for character in value)
        or value[:1] in "[,]{#}&*!|>%@`"
        or value in {"<<", "="}
    ):
        raise RecoveryConfigError(f"line {line}: {label} is not a simple scalar string")
    if _plain_scalar_is_non_string(value):
        raise RecoveryConfigError(f"line {line}: {label} must be a YAML string scalar")
    return _validate_scalar_string(value, label=label, line=line)


def _enabled(raw: str, *, label: str, line: int) -> bool:
    value = raw.strip(" ")
    if value == "true":
        return True
    if value == "false":
        return False
    raise RecoveryConfigError(f"line {line}: {label}.enabled must be true or false")


def _plugin_config(block: list[_Line], name: str) -> dict[str, object] | None:
    relevant_keys = (
        frozenset({"enabled", "storage"})
        if name == "sensor_framework"
        else frozenset({"enabled", "db_path"})
    )
    direct = _direct_mapping_children(block, relevant_keys=relevant_keys)
    enabled_field = _one_field(direct, "enabled", name)
    if enabled_field is None:
        return None
    enabled_line, enabled_raw = enabled_field
    _require_scalar_leaf(block, enabled_line, f"{name}.enabled")
    if not _enabled(enabled_raw, label=name, line=enabled_line.number):
        return None
    config: dict[str, object] = {}
    if name != "sensor_framework":
        path_field = _one_field(direct, "db_path", name)
        if path_field is not None:
            path_line, path_raw = path_field
            _require_scalar_leaf(block, path_line, f"{name}.db_path")
            config["db_path"] = _scalar_string(
                path_raw,
                label=f"{name}.db_path",
                line=path_line.number,
            )
        return config

    storage_field = _one_field(direct, "storage", name)
    if storage_field is None:
        return config
    storage_line, storage_raw = storage_field
    if storage_raw.strip(" ") == "{}":
        config["storage"] = {}
        return config
    if storage_raw.strip(" "):
        raise RecoveryConfigError(
            f"line {storage_line.number}: sensor_framework.storage must be a block mapping"
        )
    storage_block = _children(block, storage_line)
    nested = _direct_mapping_children(storage_block, relevant_keys=frozenset({"type", "path"}))
    storage: dict[str, object] = {}
    for key in ("type", "path"):
        field = _one_field(nested, key, "sensor_framework.storage")
        if field is None:
            continue
        field_line, field_raw = field
        _require_scalar_leaf(storage_block, field_line, f"sensor_framework.storage.{key}")
        storage[key] = _scalar_string(
            field_raw,
            label=f"sensor_framework.storage.{key}",
            line=field_line.number,
        )
    config["storage"] = storage
    return config


def load_migration_plugin_configs(
    path: str | os.PathLike[str],
    *,
    require_trusted: bool = False,
) -> dict[str, dict[str, object]]:
    """Return only enabled built-in migration-plugin configuration."""

    config_path = Path(path)
    lines = _active_lines(_read_config(config_path, require_trusted=require_trusted))
    roots = [
        line for line in lines if line.indentation == 0 and _header(line, "reticulumpi") is not None
    ]
    if len(roots) != 1:
        raise RecoveryConfigError("configuration must contain one top-level reticulumpi mapping")
    root = roots[0]
    if _header(root, "reticulumpi"):
        raise RecoveryConfigError("reticulumpi must be a block mapping")
    root_children = _children(lines, root)
    direct_root = _direct_mapping_children(root_children, relevant_keys=frozenset({"plugins"}))
    plugin_fields = [
        (line, value) for line in direct_root if (value := _header(line, "plugins")) is not None
    ]
    if len(plugin_fields) > 1:
        raise RecoveryConfigError("reticulumpi.plugins is configured more than once")
    if not plugin_fields:
        return {}
    plugins_line, plugins_raw = plugin_fields[0]
    if plugins_raw.strip(" ") == "{}":
        return {}
    if plugins_raw.strip(" "):
        raise RecoveryConfigError("reticulumpi.plugins must be a block mapping")
    plugin_children = _children(root_children, plugins_line)
    direct_plugins = _direct_mapping_children(
        plugin_children, relevant_keys=frozenset(MIGRATION_PLUGIN_NAMES)
    )
    blocks: dict[str, list[_Line]] = {}
    for index, line in enumerate(direct_plugins):
        entry = _mapping_entry(line)
        if entry is None:
            raise RecoveryConfigError(f"line {line.number}: malformed plugin mapping")
        name, raw = entry
        if name not in MIGRATION_PLUGIN_NAMES:
            continue
        if name in blocks:
            raise RecoveryConfigError(f"plugin {name!r} is configured more than once")
        if raw:
            raise RecoveryConfigError(
                f"line {line.number}: plugin {name!r} must be a block mapping"
            )
        boundary = direct_plugins[index + 1].number if index + 1 < len(direct_plugins) else None
        blocks[name] = _children(plugin_children, line, boundary=boundary)

    projected: dict[str, dict[str, object]] = {}
    for name in sorted(blocks):
        value = _plugin_config(blocks[name], name)
        if value is not None:
            projected[name] = value
    return projected
