"""Low-cardinality storage timing metrics for Cloud performance audits."""

from __future__ import annotations

import contextvars
import json
import logging
import time
from contextlib import contextmanager
from typing import Any, Iterator


_CURRENT_METRICS: contextvars.ContextVar["StorageOperationMetrics | None"] = (
    contextvars.ContextVar("storage_operation_metrics", default=None)
)


def _json_default(value: Any) -> Any:
    keys = getattr(value, "keys", None)
    if callable(keys):
        return {str(key): value[key] for key in keys()}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"byte_length": len(value)}
    return str(value)


def value_bytes(value: Any) -> int:
    """Return a size estimate without retaining or logging the value."""

    try:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                default=_json_default,
            ).encode("utf-8")
        )
    except (TypeError, ValueError):
        return 0


class StorageOperationMetrics:
    def __init__(self, operation: str, fields: dict[str, Any]) -> None:
        self.operation = operation
        self.fields = dict(fields)
        self.started = time.perf_counter()
        self.connection_count = 0
        self.connect_seconds = 0.0
        self.sql_count = 0
        self.sql_seconds = 0.0
        self.commit_seconds = 0.0
        self.request_parameter_bytes = 0
        self.fetched_result_bytes = 0
        self.snapshot_read_bytes = 0
        self.snapshot_write_bytes = 0
        self.fetch_seconds = 0.0

    def record_connection(self, elapsed: float) -> None:
        self.connection_count += 1
        self.connect_seconds += elapsed

    def record_sql(self, parameters: Any, elapsed: float) -> None:
        self.sql_count += 1
        self.sql_seconds += elapsed
        if parameters is not None:
            self.request_parameter_bytes += value_bytes(parameters)

    def record_result(self, value: Any) -> None:
        if value is not None:
            self.fetched_result_bytes += value_bytes(value)

    def record_fetch(self, elapsed: float) -> None:
        """Include result fetching in the round-trip time measurement."""

        self.fetch_seconds += elapsed
        self.sql_seconds += elapsed

    def add_snapshot_read_bytes(self, value: Any) -> None:
        if value is not None:
            self.snapshot_read_bytes += value_bytes(value)

    def add_snapshot_write_bytes(self, value: Any) -> None:
        self.snapshot_write_bytes += value_bytes(value)

    def set(self, **fields: Any) -> None:
        self.fields.update(fields)

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "connection_count": self.connection_count,
            "connect_seconds": round(self.connect_seconds, 6),
            "sql_count": self.sql_count,
            "sql_seconds": round(self.sql_seconds, 6),
            "fetch_seconds": round(self.fetch_seconds, 6),
            "commit_seconds": round(self.commit_seconds, 6),
            "request_parameter_bytes": self.request_parameter_bytes,
            "fetched_result_bytes": self.fetched_result_bytes,
            "snapshot_read_bytes": self.snapshot_read_bytes,
            "snapshot_write_bytes": self.snapshot_write_bytes,
            "rerender_seconds": 0.0,
            "project_count": 0,
            "response_count": 0,
            "session_count": 0,
            "assignment_count": 0,
            "total_seconds": round(time.perf_counter() - self.started, 6),
            **self.fields,
        }


def current_metrics() -> StorageOperationMetrics | None:
    return _CURRENT_METRICS.get()


def log_storage_event(
    logger: logging.Logger,
    operation: str,
    **fields: Any,
) -> None:
    """Emit the same safe schema for timings outside a storage connection."""

    payload: dict[str, Any] = {
        "operation": operation,
        "connection_count": 0,
        "connect_seconds": 0.0,
        "sql_count": 0,
        "sql_seconds": 0.0,
        "fetch_seconds": 0.0,
        "commit_seconds": 0.0,
        "request_parameter_bytes": 0,
        "fetched_result_bytes": 0,
        "snapshot_read_bytes": 0,
        "snapshot_write_bytes": 0,
        "rerender_seconds": 0.0,
        "project_count": 0,
        "response_count": 0,
        "session_count": 0,
        "assignment_count": 0,
        "total_seconds": 0.0,
    }
    payload.update(fields)
    logger.info(
        "storage_performance %s",
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


@contextmanager
def measure_storage_operation(
    operation: str,
    *,
    logger: logging.Logger,
    **fields: Any,
) -> Iterator[StorageOperationMetrics]:
    metrics = StorageOperationMetrics(operation, fields)
    token = _CURRENT_METRICS.set(metrics)
    try:
        yield metrics
    finally:
        try:
            logger.info(
                "storage_performance %s",
                json.dumps(
                    metrics.as_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        finally:
            _CURRENT_METRICS.reset(token)
