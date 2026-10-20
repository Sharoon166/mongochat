"""Read-only query execution with hard limits.

Only ever called with a query that has passed :mod:`mongochat.core.validator`.
Nothing in this module inserts, updates, deletes, or creates indexes.
"""

from __future__ import annotations

import time
from typing import Any

from pymongo.errors import ExecutionTimeout, OperationFailure

from mongochat.core.validator import ValidatedQuery
from mongochat.types import ExecutionResult, QueryTimeoutError, UnsafeQueryError, to_jsonable

MAX_ROWS = 50
QUERY_TIMEOUT_MS = 10_000
MAX_DISTINCT_VALUES = 50


async def execute(
    db: Any,
    query: ValidatedQuery,
    *,
    max_rows: int = MAX_ROWS,
    max_time_ms: int = QUERY_TIMEOUT_MS,
) -> ExecutionResult:
    """Run a validated read-only query and return capped, JSON-safe rows."""
    started = time.perf_counter()
    executed: dict[str, Any] = {}
    coll = db[query.collection]

    try:
        if query.operation == "find":
            cursor = coll.find(query.filter or {}).limit(max_rows)
            raw_rows = [doc async for doc in cursor]
            truncated = len(raw_rows) >= max_rows
            executed = {"operation": "find", "filter": to_jsonable(query.filter), "limit": max_rows}

        elif query.operation == "aggregate":
            pipeline = list(query.pipeline)
            capped = not _has_earlier_limit(pipeline, max_rows)
            if capped:
                pipeline.append({"$limit": max_rows})
            cursor = coll.aggregate(pipeline, allowDiskUse=False, maxTimeMS=max_time_ms)
            raw_rows = [doc async for doc in cursor]
            truncated = capped and len(raw_rows) >= max_rows
            executed = {
                "operation": "aggregate",
                "pipeline": to_jsonable(pipeline),
                "allowDiskUse": False,
                "maxTimeMS": max_time_ms,
            }

        elif query.operation == "count":
            count = await coll.count_documents(query.filter or {}, maxTimeMS=max_time_ms)
            raw_rows = [{"count": count}]
            truncated = False
            executed = {
                "operation": "count_documents",
                "filter": to_jsonable(query.filter),
                "maxTimeMS": max_time_ms,
            }

        elif query.operation == "distinct":
            values = await coll.distinct(
                query.field or "", query.filter or {}, maxTimeMS=max_time_ms
            )
            truncated = len(values) > MAX_DISTINCT_VALUES
            values = values[:MAX_DISTINCT_VALUES]
            raw_rows = [{"value": to_jsonable(v)} for v in values]
            executed = {
                "operation": "distinct",
                "field": query.field,
                "filter": to_jsonable(query.filter),
                "values_capped_at": MAX_DISTINCT_VALUES,
            }
        else:  # pragma: no cover - validator rejects anything else
            raise UnsafeQueryError(f"Unsupported operation {query.operation!r}.")

    except ExecutionTimeout as exc:
        raise QueryTimeoutError(
            f"The query on {query.collection!r} exceeded {max_time_ms}ms. "
            "Add a narrower filter or a smaller $limit."
        ) from exc
    except OperationFailure as exc:
        code = getattr(exc, "code", None)
        message = str(exc)
        if code in {50, 262} or "maxTimeMS" in message or "ExceededTimeLimit" in message:
            raise QueryTimeoutError(
                f"The query on {query.collection!r} exceeded {max_time_ms}ms."
            ) from exc
        if "not authorized" in message or "code=13" in message:
            raise UnsafeQueryError(
                f"The database user is not allowed to read {query.collection!r}: {message}"
            ) from exc
        raise UnsafeQueryError(f"MongoDB rejected the query: {message}") from exc

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    rows = [
        to_jsonable(row) if isinstance(row, dict) else {"value": to_jsonable(row)}
        for row in raw_rows
    ]
    return ExecutionResult(
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        elapsed_ms=elapsed_ms,
        executed_query=executed,
    )


def _has_earlier_limit(pipeline: list[dict[str, Any]], max_rows: int) -> bool:
    """True if the pipeline already limits at or below `max_rows` before the end."""
    for stage in pipeline:
        if "$limit" in stage:
            value = stage["$limit"]
            if isinstance(value, (int, float)) and value <= max_rows:
                return True
    return False
