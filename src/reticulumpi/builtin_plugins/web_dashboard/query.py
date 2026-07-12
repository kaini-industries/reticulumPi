"""Strict, reusable query-string validation for dashboard API handlers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class QueryValidationError(ValueError):
    """A query parameter is malformed or outside its documented bounds."""


def query_int(
    query: Mapping[str, Any],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
    clamp_maximum: bool = False,
) -> int:
    raw = query.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise QueryValidationError(f"{name} must be an integer") from exc
    if value < minimum:
        raise QueryValidationError(f"{name} must be between {minimum} and {maximum}")
    if value > maximum:
        if clamp_maximum:
            return maximum
        raise QueryValidationError(f"{name} must be between {minimum} and {maximum}")
    return value


def query_float(
    query: Mapping[str, Any],
    name: str,
    default: float | None,
    *,
    minimum: float,
    maximum: float,
    clamp_maximum: bool = False,
) -> float | None:
    raw = query.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise QueryValidationError(f"{name} must be numeric") from exc
    if value < minimum:
        raise QueryValidationError(f"{name} must be between {minimum:g} and {maximum:g}")
    if value > maximum:
        if clamp_maximum:
            return maximum
        raise QueryValidationError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value
