"""Regression tests for fixed-cardinality process-lifetime counters."""

from __future__ import annotations

import sqlite3

import pytest

from reticulumpi.runtime_metrics import (
    get_runtime_metrics,
    instrument_sqlite_class,
    record_hung_worker,
    record_process_restart,
)


def test_runtime_metrics_have_fixed_nonnegative_schema_and_are_monotonic():
    before = get_runtime_metrics()

    record_hung_worker()
    record_process_restart()

    after = get_runtime_metrics()
    assert set(after) == {
        "hung_workers_total",
        "process_restarts_total",
        "sqlite_failures_total",
    }
    assert after["hung_workers_total"] == before["hung_workers_total"] + 1
    assert after["process_restarts_total"] == before["process_restarts_total"] + 1
    assert all(isinstance(value, int) and value >= 0 for value in after.values())


def test_nested_sqlite_instrumentation_counts_one_propagated_exception():
    @instrument_sqlite_class
    class Store:
        def inner(self) -> None:
            raise sqlite3.OperationalError("database unavailable")

        def outer(self) -> None:
            self.inner()

    before = get_runtime_metrics()["sqlite_failures_total"]
    with pytest.raises(sqlite3.OperationalError, match="database unavailable"):
        Store().outer()

    assert get_runtime_metrics()["sqlite_failures_total"] == before + 1


def test_sqlite_instrumentation_ignores_non_sqlite_exceptions():
    @instrument_sqlite_class
    class Store:
        def fail(self) -> None:
            raise RuntimeError("not sqlite")

    before = get_runtime_metrics()["sqlite_failures_total"]
    with pytest.raises(RuntimeError, match="not sqlite"):
        Store().fail()

    assert get_runtime_metrics()["sqlite_failures_total"] == before
