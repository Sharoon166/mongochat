"""The tool registry: one definition per tool, exported to OpenAI and MCP.

Every tool is read-only. Arguments are validated through the tool's Pydantic params
model before the handler runs, and failures come back as ``ToolResult(is_error=True)``
so the model can correct itself instead of crashing the loop.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mongochat.core.executor import execute
from mongochat.core.schema_index import redact_document
from mongochat.core.validator import MAX_PIPELINE_STAGES, validate_query
from mongochat.types import (
    SchemaIndex,
    Tool,
    ToolArgumentError,
    ToolCall,
    ToolResult,
    UnsafeQueryError,
    to_jsonable,
    truncate_text,
)

logger = logging.getLogger("mongochat.tools")

#: Rough character budget for a single tool result going back to the model.
MAX_TOOL_RESULT_CHARS = 24_000


# --------------------------------------------------------------------------- #
# Parameter models
# --------------------------------------------------------------------------- #


class NoParams(BaseModel):
    """Empty parameter object for tools that take no arguments."""


class CollectionParam(BaseModel):
    """A single collection argument."""

    model_config = ConfigDict(extra="forbid")

    collection: str = Field(description="Name of the collection, exactly as listed.")


class SampleParams(CollectionParam):
    """How many sample documents to return (max 5)."""

    limit: int = Field(default=3, ge=1, le=5, description="How many documents to return, 1-5.")


class RunQueryParams(BaseModel):
    """One read-only query against a single collection."""

    model_config = ConfigDict(extra="forbid")

    collection: str = Field(description="Collection to query; must exist in the schema index.")
    operation: Literal["find", "aggregate", "count", "distinct"] = Field(
        description="Which read operation to run."
    )
    filter: dict[str, Any] | None = Field(
        default=None, description="Query filter for find/count/distinct or a pre-stage filter."
    )
    pipeline: list[dict[str, Any]] | None = Field(
        default=None,
        description=f"Aggregation stages for operation=aggregate, max {MAX_PIPELINE_STAGES}.",
    )
    field: str | None = Field(
        default=None, description="Field path, required for operation=distinct."
    )

    @field_validator("pipeline")
    @classmethod
    def _check_pipeline(cls, value: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if value is not None and not value:
            raise ValueError("pipeline must not be empty")
        return value


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


class ToolRegistry:
    """Builds the tool list once and dispatches calls to the handlers."""

    def __init__(
        self,
        *,
        db: Any,
        get_index: Callable[[], SchemaIndex],
        max_rows: int = 50,
    ) -> None:
        self.db = db
        self.get_index = get_index
        self.max_rows = max_rows
        self._tools: dict[str, Tool] = {}
        self._register()

    # -- construction -------------------------------------------------------- #

    def _register(self) -> None:
        self.add(
            Tool(
                name="list_collections",
                description=(
                    "List every collection in the database with its document count. "
                    "Call this first when you do not know the schema."
                ),
                params=NoParams,
                handler=self._list_collections,
            )
        )
        self.add(
            Tool(
                name="describe_collection",
                description=(
                    "Describe one collection: attribute paths, BSON types, indexes, "
                    "cardinality estimates and min/max for numeric and date fields."
                ),
                params=CollectionParam,
                handler=self._describe_collection,
            )
        )
        self.add(
            Tool(
                name="sample_documents",
                description=(
                    "Return a few redacted sample documents to show the shape of the data. "
                    "Values are redacted (emails and tokens masked, long strings truncated)."
                ),
                params=SampleParams,
                handler=self._sample_documents,
            )
        )
        self.add(
            Tool(
                name="run_query",
                description=(
                    "Run one read-only query: operation is find, aggregate, count or distinct. "
                    "Filters use the MongoDB query language but only an allowlist of operators. "
                    "Writes, $out, $merge, $where, $function and $accumulator are rejected. "
                    f"Results are capped at {self.max_rows} rows."
                ),
                params=RunQueryParams,
                handler=self._run_query,
            )
        )
        self.add(
            Tool(
                name="get_time",
                description=(
                    "Get the current local date and time. Use this for relative questions "
                    "such as 'last month' or 'yesterday'."
                ),
                params=NoParams,
                handler=self._get_time,
            )
        )

    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    # -- exports -------------------------------------------------------------- #

    @property
    def tools(self) -> list[Tool]:
        return list(self._tools.values())

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def openai_tools(self) -> list[dict[str, Any]]:
        return [tool.openai_schema() for tool in self._tools.values()]

    def mcp_tools(self) -> list[dict[str, Any]]:
        return [tool.mcp_schema() for tool in self._tools.values()]

    # -- dispatch ------------------------------------------------------------- #

    async def dispatch(self, call: ToolCall) -> ToolResult:
        """Validate and run one tool call. Never raises for tool-level failures."""
        return await self.call(call.name, call.arguments, tool_call_id=call.id)

    async def call(
        self,
        name: str,
        arguments: Any,
        *,
        tool_call_id: str = "call_0",
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            known = ", ".join(sorted(self._tools))
            return ToolResult(
                tool_call_id=tool_call_id,
                content=f"Unknown tool {name!r}. Available tools: {known}.",
                is_error=True,
            )
        try:
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                raise ToolArgumentError(
                    f"Arguments must be a JSON object, got {type(arguments).__name__}."
                )
            params = tool.params.model_validate(arguments)
        except ValidationError as exc:
            return ToolResult(
                tool_call_id=tool_call_id,
                content=_format_validation_error(tool.name, exc),
                is_error=True,
            )
        except ToolArgumentError as exc:
            return ToolResult(tool_call_id=tool_call_id, content=str(exc), is_error=True)

        try:
            payload = await tool.handler(params)
        except (UnsafeQueryError, ToolArgumentError) as exc:
            return ToolResult(tool_call_id=tool_call_id, content=str(exc), is_error=True)
        except Exception as exc:
            logger.debug("Tool %s failed", name, exc_info=True)
            return ToolResult(
                tool_call_id=tool_call_id,
                content=f"Tool {name} failed: {type(exc).__name__}: {exc}",
                is_error=True,
            )

        content = payload if isinstance(payload, str) else _dumps(payload)
        return ToolResult(
            tool_call_id=tool_call_id,
            content=truncate_text(content, MAX_TOOL_RESULT_CHARS),
            is_error=False,
        )

    # -- handlers -------------------------------------------------------------- #

    async def _list_collections(self, params: NoParams) -> dict[str, Any]:
        index = self.get_index()
        collections = [
            {"name": c.name, "document_count": c.document_count} for c in index.collections
        ]
        if not collections:
            return {
                "database": index.database,
                "collections": [],
                "note": "This database has no collections.",
            }
        return {"database": index.database, "collections": collections}

    async def _describe_collection(self, params: CollectionParam) -> dict[str, Any]:
        index = self.get_index()
        coll = _require_collection(index, params.collection)
        return {
            "name": coll.name,
            "document_count": coll.document_count,
            "avg_doc_size": coll.avg_doc_size,
            "indexes": coll.indexes,
            "attributes": [a.model_dump(mode="json") for a in coll.attributes],
        }

    async def _sample_documents(self, params: SampleParams) -> dict[str, Any]:
        index = self.get_index()
        coll = _require_collection(index, params.collection)
        if coll.sample_documents:
            samples = coll.sample_documents[: params.limit]
        else:
            cursor = self.db[coll.name].find({}).limit(params.limit)
            docs: list[dict[str, Any]] = []
            async for doc in cursor:
                docs.append(doc)
            samples = [to_jsonable(redact_document(d)) for d in docs]
        return {
            "collection": coll.name,
            "document_count": coll.document_count,
            "samples": samples,
            "note": "Sample values are redacted: emails and tokens are masked.",
        }

    async def _run_query(self, params: RunQueryParams) -> dict[str, Any]:
        index = self.get_index()
        query = validate_query(
            index=index,
            collection=params.collection,
            operation=params.operation,
            filter=params.filter,
            pipeline=params.pipeline,
            field=params.field,
        )
        result = await execute(self.db, query, max_rows=self.max_rows)
        return {
            "rows": result.rows,
            "row_count": result.row_count,
            "truncated": result.truncated,
            "elapsed_ms": result.elapsed_ms,
            "executed_query": result.executed_query,
        }

    async def _get_time(self, params: NoParams) -> dict[str, Any]:
        now = datetime.now().astimezone()
        return {
            "local_time": now.isoformat(timespec="seconds"),
            "date": now.date().isoformat(),
            "weekday": now.strftime("%A"),
        }


def _require_collection(index: SchemaIndex, name: str) -> Any:
    coll = index.find_collection(name)
    if coll is None:
        known = ", ".join(index.collection_names[:15]) or "(none)"
        raise ToolArgumentError(f"Collection {name!r} does not exist. Known collections: {known}.")
    return coll


def _format_validation_error(tool_name: str, exc: ValidationError) -> str:
    problems = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "arguments"
        problems.append(f"{loc}: {err['msg']}")
    return (
        f"Invalid arguments for {tool_name}: " + "; ".join(problems) + ". "
        "Fix the arguments and call the tool again."
    )


def _dumps(payload: Any) -> str:
    return json.dumps(payload, default=str, ensure_ascii=False)


def describe_tools(registry: ToolRegistry) -> Sequence[dict[str, Any]]:
    """Schema descriptions for `mongochat tools`."""
    return registry.mcp_tools()
