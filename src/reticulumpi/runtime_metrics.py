"""Process-lifetime, fixed-cardinality operational counters."""

from __future__ import annotations

import functools
import inspect
import sqlite3
import threading
from collections.abc import Callable
from typing import Any, TypeVar, cast


_MAX_COUNTER = (1 << 63) - 1
_lock = threading.Lock()
_counters = {
    "hung_workers_total": 0,
    "process_restarts_total": 0,
    "sqlite_failures_total": 0,
}
_Function = TypeVar("_Function", bound=Callable[..., Any])


def _increment(name: str) -> None:
    with _lock:
        _counters[name] = min(_MAX_COUNTER, _counters[name] + 1)


def record_hung_worker() -> None:
    """Record one detected worker timeout without a worker/plugin label."""

    _increment("hung_workers_total")


def record_process_restart() -> None:
    """Record one reserved restart attempt across every supervisor type."""

    _increment("process_restarts_total")


def record_sqlite_failure(error: BaseException | None = None) -> None:
    """Record one SQLite failure, deduplicating propagation of one exception."""

    marker = "_reticulumpi_sqlite_metric_recorded"
    if error is not None:
        if getattr(error, marker, False):
            return
        try:
            setattr(error, marker, True)
        except Exception:
            # Some third-party exception implementations may disallow custom
            # attributes. Counting is preferable to silently losing the event.
            pass
    _increment("sqlite_failures_total")


def get_runtime_metrics() -> dict[str, int]:
    """Return the bounded aggregate schema without names, paths, or payloads."""

    with _lock:
        return dict(_counters)


def count_sqlite_failures(function: _Function) -> _Function:
    """Count a SQLite exception once while preserving the original traceback."""

    @functools.wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except sqlite3.Error as exc:
            record_sqlite_failure(exc)
            raise

    return cast(_Function, wrapped)


def instrument_sqlite_class(cls: type[Any]) -> type[Any]:
    """Wrap class-defined methods so escaping SQLite errors are counted once."""

    for name, value in tuple(vars(cls).items()):
        if inspect.isfunction(value):
            setattr(cls, name, count_sqlite_failures(value))
    return cls
