#!/usr/bin/env python3
"""Generate deterministic documentation summaries directly from source code."""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REFERENCE_PATH = ROOT / "docs/generated-code-reference.md"
CONFIG_SOURCE = ROOT / "src/reticulumpi/config.py"
EVENT_SOURCE = ROOT / "src/reticulumpi/events.py"
PLUGIN_ROOT = ROOT / "src/reticulumpi/builtin_plugins"
DASHBOARD_ROOT = PLUGIN_ROOT / "web_dashboard"


@dataclass(frozen=True, order=True)
class PluginRecord:
    name: str
    version: str
    description: str
    source: str


@dataclass(frozen=True, order=True)
class EventRecord:
    constant: str
    event: str


@dataclass(frozen=True, order=True)
class RouteRecord:
    path: str
    method: str
    source: str


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _literal_assignments(node: ast.ClassDef) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for statement in node.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            value = statement.value
        elif isinstance(statement, ast.AnnAssign):
            target = statement.target
            value = statement.value
        if not isinstance(target, ast.Name) or value is None:
            continue
        try:
            values[target.id] = ast.literal_eval(value)
        except (TypeError, ValueError):
            continue
    return values


def core_defaults() -> dict[str, Any]:
    """Return the literal ``DEFAULT_CONFIG`` mapping without importing runtime code."""

    for statement in _parse(CONFIG_SOURCE).body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            value = statement.value
        elif isinstance(statement, ast.AnnAssign):
            target = statement.target
            value = statement.value
        if isinstance(target, ast.Name) and target.id == "DEFAULT_CONFIG" and value is not None:
            result = ast.literal_eval(value)
            if not isinstance(result, dict):
                raise ValueError("DEFAULT_CONFIG must be a literal mapping")
            return result
    raise ValueError(f"DEFAULT_CONFIG not found in {CONFIG_SOURCE}")


def _flatten_defaults(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict) and value:
        flattened: list[tuple[str, Any]] = []
        for key in sorted(value):
            child = f"{prefix}.{key}" if prefix else str(key)
            flattened.extend(_flatten_defaults(value[key], child))
        return flattened
    return [(prefix, value)]


def builtin_plugins() -> tuple[PluginRecord, ...]:
    """Extract discoverable built-in plugin metadata from class literals."""

    records: list[PluginRecord] = []
    for path in sorted(PLUGIN_ROOT.rglob("*.py")):
        for node in _parse(path).body:
            if not isinstance(node, ast.ClassDef):
                continue
            values = _literal_assignments(node)
            name = values.get("plugin_name")
            if not isinstance(name, str) or not name or name == "unnamed":
                continue
            version = values.get("plugin_version", "0.0.0")
            description = values.get("plugin_description", "No description")
            if not isinstance(version, str) or not isinstance(description, str):
                raise ValueError(f"plugin metadata must be strings: {path}:{node.lineno}")
            records.append(
                PluginRecord(
                    name=name,
                    version=version,
                    description=description,
                    source=path.relative_to(ROOT).as_posix(),
                )
            )
    names = [record.name for record in records]
    duplicates = sorted(name for name in set(names) if names.count(name) > 1)
    if duplicates:
        raise ValueError("duplicate built-in plugin names: " + ", ".join(duplicates))
    return tuple(sorted(records))


def event_constants() -> tuple[EventRecord, ...]:
    """Extract public event constants from ``reticulumpi.events``."""

    records: list[EventRecord] = []
    for statement in _parse(EVENT_SOURCE).body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(statement, ast.Assign):
            targets = list(statement.targets)
            value = statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
            value = statement.value
        if value is None:
            continue
        try:
            event = ast.literal_eval(value)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, str):
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id.isupper():
                records.append(EventRecord(target.id, event))
    event_names = [record.event for record in records]
    duplicates = sorted(name for name in set(event_names) if event_names.count(name) > 1)
    if duplicates:
        raise ValueError("duplicate event names: " + ", ".join(duplicates))
    return tuple(sorted(records))


def dashboard_routes() -> tuple[RouteRecord, ...]:
    """Extract constant aiohttp route registrations from dashboard source."""

    records: list[RouteRecord] = []
    for path in sorted(DASHBOARD_ROOT.rglob("*.py")):
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            router = node.func.value
            if (
                not node.func.attr.startswith("add_")
                or not isinstance(router, ast.Attribute)
                or router.attr != "router"
                or not node.args
            ):
                continue
            method = node.func.attr.removeprefix("add_").upper()
            if method not in {"DELETE", "GET", "PATCH", "POST", "PUT"}:
                continue
            try:
                route_path = ast.literal_eval(node.args[0])
            except (TypeError, ValueError):
                continue
            if not isinstance(route_path, str):
                continue
            records.append(
                RouteRecord(
                    path=route_path,
                    method=method,
                    source=path.relative_to(ROOT).as_posix(),
                )
            )
    identities = [(record.method, record.path) for record in records]
    duplicates = sorted(identity for identity in set(identities) if identities.count(identity) > 1)
    if duplicates:
        rendered = ", ".join(f"{method} {path}" for method, path in duplicates)
        raise ValueError(f"duplicate dashboard routes: {rendered}")
    return tuple(sorted(records))


def _cell(value: str) -> str:
    return value.replace("|", r"\|").replace("\n", " ")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def render_reference() -> str:
    """Render the complete generated Markdown reference."""

    defaults = _flatten_defaults(core_defaults())
    plugins = builtin_plugins()
    events = event_constants()
    routes = dashboard_routes()
    lines = [
        "# Generated Code Reference",
        "",
        "> This file is generated by `python tools/generate_docs_reference.py`. Do not edit",
        "> it manually; documentation CI compares it with the source tree.",
        "",
        "## Core configuration defaults",
        "",
        f"{len(defaults)} effective defaults are declared by `reticulumpi.config.DEFAULT_CONFIG`.",
        "Plugin-specific defaults remain documented with their plugin because they may depend on",
        "runtime or hardware context.",
        "",
        "| Key | Default |",
        "|---|---|",
    ]
    lines.extend(f"| `{key}` | `{_cell(_json(value))}` |" for key, value in defaults)
    lines.extend(
        [
            "",
            "## Built-in plugins",
            "",
            f"{len(plugins)} plugin classes declare built-in discovery metadata.",
            "",
            "| Plugin | Version | Description | Source |",
            "|---|---:|---|---|",
        ]
    )
    lines.extend(
        "| `{name}` | `{version}` | {description} | [`{source}`](../{source}) |".format(
            name=_cell(plugin.name),
            version=_cell(plugin.version),
            description=_cell(plugin.description),
            source=plugin.source,
        )
        for plugin in plugins
    )
    lines.extend(
        [
            "",
            "## Event bus constants",
            "",
            f"{len(events)} unique public event names are declared in `reticulumpi.events`.",
            "",
            "| Constant | Event name |",
            "|---|---|",
        ]
    )
    lines.extend(f"| `{event.constant}` | `{event.event}` |" for event in events)
    lines.extend(
        [
            "",
            "## Dashboard routes",
            "",
            f"{len(routes)} unique HTTP/WebSocket registrations are present in dashboard source.",
            "Configuration-conditional routes are included because they are still part of the",
            "supported route contract.",
            "",
            "| Method | Path | Registration source |",
            "|---|---|---|",
        ]
    )
    lines.extend(
        "| `{method}` | `{path}` | [`{source}`](../{source}) |".format(
            method=route.method,
            path=_cell(route.path),
            source=route.source,
        )
        for route in routes
    )
    return "\n".join(lines) + "\n"


def write_reference(path: Path = REFERENCE_PATH) -> None:
    """Atomically replace the generated reference."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(render_reference())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def reference_diff(path: Path = REFERENCE_PATH) -> str | None:
    """Return a unified diff when the generated reference is missing or stale."""

    actual = render_reference()
    expected = path.read_text(encoding="utf-8") if path.is_file() else ""
    if expected == actual:
        return None
    try:
        display_path = path.relative_to(ROOT)
    except ValueError:
        display_path = path
    return "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile=str(display_path),
            tofile="current source-derived reference",
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail instead of rewriting when the generated reference is stale",
    )
    arguments = parser.parse_args(argv)
    if arguments.check:
        difference = reference_diff()
        if difference is not None:
            print(difference, end="")
            return 1
        print(f"Verified {REFERENCE_PATH.relative_to(ROOT)}")
        return 0
    write_reference()
    print(f"Generated {REFERENCE_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
