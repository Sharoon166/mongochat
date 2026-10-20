"""Tests for the agent loop, driven by a fake LLMClient."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from mongomock_motor import AsyncMongoMockClient

from mongochat.core.agent import Agent, build_system_prompt, prune_history, set_debug
from mongochat.core.tools import ToolRegistry
from mongochat.types import (
    AgentResult,
    AttributeInfo,
    CollectionIndex,
    LLMResponseError,
    SchemaIndex,
)


def make_index() -> SchemaIndex:
    return SchemaIndex(
        database="shop",
        generated_at=datetime.now(UTC),
        collections=[
            CollectionIndex(
                name="orders",
                document_count=2,
                attributes=[AttributeInfo(path="total", types=["int"], cardinality_estimate=2)],
                indexes=["_id_"],
            )
        ],
    )


def tool_call(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def assistant(
    content: str | None = None, calls: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return message


class FakeLLMClient:
    """Scripted stand-in for LLMClient: pops one reply per chat() call."""

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict[str, Any]]] = []
        self.tools_seen: list[list[dict[str, Any]] | None] = []
        self.model = "fake-model"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        self.calls.append(list(messages))
        self.tools_seen.append(tools)
        if not self.replies:
            raise AssertionError("FakeLLMClient ran out of scripted replies")
        return self.replies.pop(0)


@pytest.fixture
def db() -> Any:
    return AsyncMongoMockClient()["shop"]


@pytest.fixture
def registry(db: Any) -> ToolRegistry:
    index = make_index()
    return ToolRegistry(db=db, get_index=lambda: index)


def agent(fake: FakeLLMClient, registry: ToolRegistry, *, max_steps: int = 4) -> Agent:
    return Agent(
        llm=fake,  # type: ignore[arg-type]
        registry=registry,
        index=make_index(),
        max_steps=max_steps,
    )


# --------------------------------------------------------------------------- #
# Single tool call then answer
# --------------------------------------------------------------------------- #


async def test_single_tool_call_then_answer(registry: ToolRegistry) -> None:
    fake = FakeLLMClient(
        [
            assistant(calls=[tool_call("list_collections", {})]),
            assistant("The shop database has 1 collection: orders. (orders)"),
        ]
    )
    result = await agent(fake, registry).run("what is in this database?")

    assert isinstance(result, AgentResult)
    assert "orders" in result.answer
    assert len(result.steps) == 1
    assert result.steps[0].tool_name == "list_collections"
    assert result.steps[0].error is False
    assert result.elapsed_ms >= 0

    # the tool result went back to the model before the final answer
    final_messages = fake.calls[-1]
    tool_messages = [m for m in final_messages if m["role"] == "tool"]
    assert len(tool_messages) == 1
    assert "orders" in tool_messages[0]["content"]


async def test_tools_are_passed_to_the_model(registry: ToolRegistry) -> None:
    fake = FakeLLMClient([assistant("no tools needed")])
    await agent(fake, registry).run("hi")
    assert fake.tools_seen[0] is not None
    names = {t["function"]["name"] for t in fake.tools_seen[0]}  # type: ignore[union-attr]
    assert names == {
        "list_collections",
        "describe_collection",
        "sample_documents",
        "run_query",
        "get_time",
    }


# --------------------------------------------------------------------------- #
# Multiple tool calls in one turn
# --------------------------------------------------------------------------- #


async def test_multiple_tool_calls_in_one_turn(db: Any) -> None:
    await db.orders.insert_many([{"_id": 1, "total": 5}, {"_id": 2, "total": 7}])
    index = make_index()
    registry = ToolRegistry(db=db, get_index=lambda: index)
    fake = FakeLLMClient(
        [
            assistant(
                calls=[
                    tool_call("describe_collection", {"collection": "orders"}, "call_a"),
                    tool_call(
                        "run_query",
                        {"collection": "orders", "operation": "find", "filter": {}},
                        "call_b",
                    ),
                ]
            ),
            assistant("Two orders totalling 12. (orders)"),
        ]
    )
    result = await agent(fake, registry).run("how many orders?")

    assert len(result.steps) == 2
    assert [s.tool_name for s in result.steps] == ["describe_collection", "run_query"]
    assert result.steps[1].row_count == 2
    assert result.executed_queries
    assert result.executed_queries[0]["operation"] == "find"

    # both tool messages are present, in call order, before the answering turn
    tool_messages = [m for m in fake.calls[-1] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["call_a", "call_b"]


# --------------------------------------------------------------------------- #
# Tool error and recovery
# --------------------------------------------------------------------------- #


async def test_tool_error_is_recovered_from(registry: ToolRegistry) -> None:
    fake = FakeLLMClient(
        [
            assistant(calls=[tool_call("describe_collection", {"collection": "ghosts"})]),
            assistant(calls=[tool_call("describe_collection", {"collection": "orders"})]),
            assistant("orders has 2 documents. (orders)"),
        ]
    )
    result = await agent(fake, registry).run("describe ghosts")

    assert len(result.steps) == 2
    assert result.steps[0].error is True
    assert result.steps[1].error is False
    assert "does not exist" in result.steps[0].result_summary

    # the model saw the error as a tool message and corrected the call
    tool_messages = [m for m in fake.calls[1] if m["role"] == "tool"]
    assert tool_messages and "ghosts" in tool_messages[0]["content"]


async def test_malformed_tool_arguments_do_not_raise(registry: ToolRegistry) -> None:
    fake = FakeLLMClient(
        [
            assistant(
                calls=[
                    {
                        "id": "bad",
                        "type": "function",
                        "function": {"name": "run_query", "arguments": "{not json"},
                    }
                ]
            ),
            assistant("I could not run that query. (orders)"),
        ]
    )
    result = await agent(fake, registry).run("query")
    assert len(result.steps) == 1
    assert result.steps[0].error is True


# --------------------------------------------------------------------------- #
# Step limit
# --------------------------------------------------------------------------- #


async def test_step_limit_returns_partial_answer(registry: ToolRegistry) -> None:
    loop_call = assistant(calls=[tool_call("list_collections", {})])
    fake = FakeLLMClient(
        [loop_call, loop_call, loop_call, assistant("Partial answer from what I have.")]
    )
    result = await agent(fake, registry, max_steps=3).run("keep going")

    assert len(result.steps) == 3
    assert "step limit" in result.answer or "maximum" in result.answer
    assert "Partial answer" in result.answer


async def test_step_limit_with_no_final_answer(registry: ToolRegistry) -> None:
    loop_call = assistant(calls=[tool_call("get_time", {})])
    fake = FakeLLMClient([loop_call, loop_call, assistant("")])
    result = await agent(fake, registry, max_steps=2).run("never ends")

    assert len(result.steps) == 2
    assert "step limit" in result.answer.lower()
    assert "--max-steps" in result.answer


# --------------------------------------------------------------------------- #
# Off-topic / no-tool answers
# --------------------------------------------------------------------------- #


async def test_off_topic_answer_without_tools(registry: ToolRegistry) -> None:
    fake = FakeLLMClient([assistant("This database has no information about the weather.")])
    result = await agent(fake, registry).run("what is the weather today?")

    assert result.steps == []
    assert "no information" in result.answer
    assert fake.tools_seen[0] is not None  # tools were offered, the model declined


async def test_empty_answer_raises_helpful_error(registry: ToolRegistry) -> None:
    fake = FakeLLMClient([assistant("")])
    with pytest.raises(LLMResponseError) as exc:
        await agent(fake, registry).run("hi")
    assert "tool-capable" in str(exc.value)


# --------------------------------------------------------------------------- #
# Prompt and history
# --------------------------------------------------------------------------- #


def test_system_prompt_contains_rules_and_schema() -> None:
    prompt = build_system_prompt(make_index())
    assert "Cite the collections" in prompt
    assert "row count" in prompt
    assert "Never invent" in prompt
    assert "collection orders" in prompt


def test_prune_history_keeps_last_ten_turns() -> None:
    messages: list[dict[str, Any]] = [{"role": "system", "content": "sys"}]
    for i in range(15):
        messages.append({"role": "user", "content": f"q{i}"})
        messages.append(
            assistant(
                f"a{i}",
                [tool_call("list_collections", {}, f"call_{i}")],
            )
        )
        messages.append({"role": "tool", "tool_call_id": f"call_{i}", "content": "data"})

    pruned = prune_history(messages, turns=10)
    users = [m for m in pruned if m["role"] == "user"]
    assert len(users) == 10
    assert users[0]["content"] == "q5"
    # tool messages are gone from stored history, answers are kept
    assert not any(m["role"] == "tool" for m in pruned)
    assert pruned[0]["role"] == "system"
    assert any(m["role"] == "assistant" for m in pruned)


def test_prune_history_short_history_untouched() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    assert prune_history(messages) == messages


async def test_debug_logging_is_available(registry: ToolRegistry, caplog: Any) -> None:
    set_debug(True)
    try:
        with caplog.at_level("DEBUG", logger="mongochat.agent"):
            fake = FakeLLMClient(
                [
                    assistant(calls=[tool_call("list_collections", {})]),
                    assistant("done (orders)"),
                ]
            )
            await agent(fake, registry).run("q")
    finally:
        set_debug(False)
    text = " ".join(r.message for r in caplog.records)
    assert "list_collections" in text
    assert "rows=" in text
