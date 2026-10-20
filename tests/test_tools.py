"""Tests for the tool registry: schemas, argument validation, error results."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from mongomock_motor import AsyncMongoMockClient

from mongochat.core.tools import ToolRegistry
from mongochat.types import (
    AttributeInfo,
    CollectionIndex,
    SchemaIndex,
    ToolCall,
    ToolResult,
)


def make_index() -> SchemaIndex:
    return SchemaIndex(
        database="shop",
        generated_at=datetime.now(UTC),
        collections=[
            CollectionIndex(
                name="orders",
                document_count=3,
                attributes=[AttributeInfo(path="total", types=["int"], min=1, max=9)],
                indexes=["_id_"],
                sample_documents=[{"_id": "1", "total": 5}],
            )
        ],
    )


class FakeIndexHolder:
    def __init__(self, index: SchemaIndex) -> None:
        self.index = index

    def __call__(self) -> SchemaIndex:
        return self.index


@pytest.fixture
def db() -> Any:
    client = AsyncMongoMockClient()
    return client["shop"]


@pytest.fixture
def registry(db: Any) -> ToolRegistry:
    return ToolRegistry(db=db, get_index=FakeIndexHolder(make_index()))


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


def test_openai_schema_shape(registry: ToolRegistry) -> None:
    schemas = registry.openai_tools()
    assert len(schemas) == 5
    for schema in schemas:
        assert schema["type"] == "function"
        fn = schema["function"]
        assert isinstance(fn["name"], str)
        assert isinstance(fn["description"], str)
        assert fn["description"]
        assert isinstance(fn["parameters"], dict)
        assert "properties" in fn["parameters"]


def test_openai_schema_has_query_params(registry: ToolRegistry) -> None:
    by_name = {s["function"]["name"]: s for s in registry.openai_tools()}
    props = by_name["run_query"]["function"]["parameters"]["properties"]
    assert set(props) >= {"collection", "operation", "filter", "pipeline", "field"}
    assert set(by_name["sample_documents"]["function"]["parameters"]["properties"]) == {
        "collection",
        "limit",
    }
    # empty params still serialize as an object schema
    assert by_name["get_time"]["function"]["parameters"]["properties"] == {}


def test_mcp_schema_shape(registry: ToolRegistry) -> None:
    schemas = registry.mcp_tools()
    names = [s["name"] for s in schemas]
    assert names == [
        "list_collections",
        "describe_collection",
        "sample_documents",
        "run_query",
        "get_time",
    ]
    for schema in schemas:
        assert schema["description"]
        assert schema["inputSchema"]["type"] == "object"
        assert "title" not in schema["inputSchema"]


def test_schemas_match_per_tool_definition(registry: ToolRegistry) -> None:
    for tool in registry.tools:
        openai_props = tool.openai_schema()["function"]["parameters"]
        mcp_props = tool.mcp_schema()["inputSchema"]
        assert openai_props["properties"].keys() == mcp_props["properties"].keys()


# --------------------------------------------------------------------------- #
# Argument validation
# --------------------------------------------------------------------------- #


async def test_bad_arguments_are_error_results(registry: ToolRegistry) -> None:
    result = await registry.dispatch(
        ToolCall(id="c1", name="describe_collection", arguments={"wrong": "x"})
    )
    assert isinstance(result, ToolResult)
    assert result.is_error
    assert "describe_collection" in result.content
    assert "collection" in result.content


async def test_sample_limit_bounds(registry: ToolRegistry) -> None:
    ok = await registry.dispatch(
        ToolCall(id="c2", name="sample_documents", arguments={"collection": "orders", "limit": 5})
    )
    assert not ok.is_error
    too_many = await registry.dispatch(
        ToolCall(id="c3", name="sample_documents", arguments={"collection": "orders", "limit": 9})
    )
    assert too_many.is_error


async def test_unknown_tool_is_error(registry: ToolRegistry) -> None:
    result = await registry.dispatch(ToolCall(id="c4", name="drop_everything", arguments={}))
    assert result.is_error
    assert "Unknown tool" in result.content


async def test_arguments_must_be_object(registry: ToolRegistry) -> None:
    result = await registry.call("list_collections", "not-a-dict")  # type: ignore[arg-type]
    assert result.is_error
    assert "JSON object" in result.content


# --------------------------------------------------------------------------- #
# Behaviour
# --------------------------------------------------------------------------- #


async def test_list_collections(registry: ToolRegistry) -> None:
    result = await registry.dispatch(ToolCall(id="c6", name="list_collections", arguments={}))
    assert not result.is_error
    assert "orders" in result.content
    assert "3" in result.content


async def test_describe_collection(registry: ToolRegistry) -> None:
    result = await registry.dispatch(
        ToolCall(id="c7", name="describe_collection", arguments={"collection": "orders"})
    )
    assert not result.is_error
    assert "total" in result.content
    assert "_id_" in result.content


async def test_describe_unknown_collection_is_error(registry: ToolRegistry) -> None:
    result = await registry.dispatch(
        ToolCall(id="c8", name="describe_collection", arguments={"collection": "ghosts"})
    )
    assert result.is_error
    assert "orders" in result.content


async def test_run_query_rejects_bad_operation(db: Any, registry: ToolRegistry) -> None:
    result = await registry.dispatch(
        ToolCall(
            id="c9",
            name="run_query",
            arguments={"collection": "orders", "operation": "delete"},
        )
    )
    assert result.is_error
    assert "operation" in result.content


async def test_run_query_rejects_write_pipeline(registry: ToolRegistry) -> None:
    result = await registry.dispatch(
        ToolCall(
            id="c10",
            name="run_query",
            arguments={
                "collection": "orders",
                "operation": "aggregate",
                "pipeline": [{"$out": "copy"}],
            },
        )
    )
    assert result.is_error
    assert "$out" in result.content


async def test_run_query_returns_rows(db: Any, registry: ToolRegistry) -> None:
    await db.orders.insert_many([{"_id": 1, "total": 5}, {"_id": 2, "total": 7}])
    result = await registry.dispatch(
        ToolCall(
            id="c11",
            name="run_query",
            arguments={"collection": "orders", "operation": "find", "filter": {}},
        )
    )
    assert not result.is_error
    assert '"row_count": 2' in result.content
    assert "executed_query" in result.content


async def test_get_time(registry: ToolRegistry) -> None:
    result = await registry.dispatch(ToolCall(id="c12", name="get_time", arguments={}))
    assert not result.is_error
    assert "local_time" in result.content


async def test_handler_crash_becomes_error(db: Any) -> None:
    """A failing handler must surface as a ToolResult, not an exception."""
    from pydantic import BaseModel

    from mongochat.types import Tool

    async def boom(params: BaseModel) -> str:
        raise RuntimeError("kaput")

    registry = ToolRegistry(db=db, get_index=FakeIndexHolder(make_index()))
    registry.add(
        Tool(
            name="boom",
            description="always fails",
            params=type("P", (BaseModel,), {}),
            handler=boom,
        )
    )
    result = await registry.call("boom", {})
    assert result.is_error
    assert "kaput" in result.content
