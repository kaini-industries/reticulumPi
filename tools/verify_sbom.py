#!/usr/bin/env python3
"""Fail-closed validation for the release CycloneDX SBOM."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


class SbomValidationError(ValueError):
    """Raised when an SBOM is malformed or does not describe ReticulumPi."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SbomValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _component_tree(document: dict[str, Any]) -> list[dict[str, Any]]:
    pending: list[Any] = list(document.get("components", []))
    metadata = document.get("metadata", {})
    if isinstance(metadata, dict) and metadata.get("component") is not None:
        pending.append(metadata["component"])

    components: list[dict[str, Any]] = []
    while pending:
        component = pending.pop()
        if not isinstance(component, dict):
            raise SbomValidationError("every CycloneDX component must be an object")
        components.append(component)
        children = component.get("components", [])
        if not isinstance(children, list):
            raise SbomValidationError("nested CycloneDX components must be a list")
        pending.extend(children)
    return components


def validate_sbom(path: Path) -> tuple[str, int]:
    """Validate *path* and return its specification version and component count."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SbomValidationError(f"cannot read CycloneDX JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise SbomValidationError("CycloneDX document root must be an object")
    if document.get("bomFormat") != "CycloneDX":
        raise SbomValidationError("SBOM bomFormat must be CycloneDX")

    specification = document.get("specVersion")
    match = re.fullmatch(r"1\.(\d+)", specification if isinstance(specification, str) else "")
    if match is None or int(match.group(1)) < 4:
        raise SbomValidationError("CycloneDX specVersion must be 1.4 or newer")
    if not isinstance(document.get("version"), int) or document["version"] < 1:
        raise SbomValidationError("CycloneDX document version must be a positive integer")

    declared = document.get("components", [])
    if not isinstance(declared, list):
        raise SbomValidationError("CycloneDX components must be a list")
    components = _component_tree(document)
    if not components:
        raise SbomValidationError("CycloneDX SBOM contains no components")
    for component in components:
        if not isinstance(component.get("name"), str) or not component["name"].strip():
            raise SbomValidationError("every CycloneDX component must have a name")
    if not any(component["name"].casefold() == "reticulumpi" for component in components):
        raise SbomValidationError("CycloneDX SBOM does not identify the reticulumpi package")
    return specification, len(components)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sbom", type=Path, help="CycloneDX JSON file to validate")
    args = parser.parse_args(argv)
    try:
        specification, component_count = validate_sbom(args.sbom)
    except SbomValidationError as exc:
        parser.error(str(exc))
    print(f"Verified CycloneDX {specification} SBOM with {component_count} components")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
