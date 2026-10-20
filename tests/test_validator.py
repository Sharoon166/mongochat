"""Tests for the read-only query validator."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mongochat.core.validator import (
    MAX_NESTING_DEPTH,
    MAX_PIPELINE_STAGES,
    validate_command,
    validate_query,
)
from mongochat.types import AttributeInfo, CollectionIndex, SchemaIndex, UnsafeQueryError


def make_index(*names: str) -> SchemaIndex:
    return SchemaIndex(
        database="test",
        generated_at=datetime.now(UTC),
        collections=[
            CollectionIndex(
                name=name,
                document_count=10,
                attributes=[AttributeInfo(path="_id", types=["objectId"])],
            )
            for name in names
        ],
    )


INDEX = make_index("orders", "users", "items")


def valid(operation: str, **kwargs: object) -> None:
    validate_query(index=INDEX, operation=operation, **kwargs)  # type: ignore[arg-type]


def rejected(operation: str, **kwargs: object) -> str:
    with pytest.raises(UnsafeQueryError) as exc:
        validate_query(index=INDEX, operation=operation, **kwargs)  # type: ignore[arg-type]
    return str(exc.value)


# --------------------------------------------------------------------------- #
# Forbidden operators at every depth
# --------------------------------------------------------------------------- #

FORBIDDEN = ["$out", "$merge", "$function", "$accumulator", "$where", "$eval"]


@pytest.mark.parametrize("op", FORBIDDEN)
def test_forbidden_operator_in_first_stage(op: str) -> None:
    message = rejected("aggregate", collection="orders", pipeline=[{op: {}}])
    assert op in message


@pytest.mark.parametrize("op", FORBIDDEN)
def test_forbidden_operator_deep_in_pipeline(op: str) -> None:
    message = rejected(
        "aggregate",
        collection="orders",
        pipeline=[
            {"$match": {}},
            {"$addFields": {"x": {"$literal": 1}}},
            {"$project": {"y": {op: "nope"}}},
        ],
    )
    assert op in message


@pytest.mark.parametrize("op", FORBIDDEN)
def test_forbidden_operator_inside_facet(op: str) -> None:
    message = rejected(
        "aggregate",
        collection="orders",
        pipeline=[{"$facet": {"a": [{"$project": {"z": {op: 1}}}]}}],
    )
    assert op in message


@pytest.mark.parametrize("op", FORBIDDEN)
def test_forbidden_operator_inside_lookup_subpipeline(op: str) -> None:
    message = rejected(
        "aggregate",
        collection="orders",
        pipeline=[
            {"$lookup": {"from": "users", "pipeline": [{"$project": {"q": {op: 1}}}], "as": "u"}},
        ],
    )
    assert op in message


@pytest.mark.parametrize("op", FORBIDDEN)
def test_forbidden_operator_inside_expr(op: str) -> None:
    message = rejected(
        "aggregate",
        collection="orders",
        pipeline=[{"$match": {"$expr": {"$eq": [{"$literal": {op: 1}}, 1]}}}],
    )
    assert op in message


@pytest.mark.parametrize("op", FORBIDDEN)
def test_forbidden_operator_in_find_filter(op: str) -> None:
    message = rejected("find", collection="orders", filter={"a": {op: 1}})
    assert op in message


def test_out_and_merge_stages_rejected() -> None:
    assert "$out" in rejected("aggregate", collection="orders", pipeline=[{"$out": "copy"}])
    assert "$merge" in rejected("aggregate", collection="orders", pipeline=[{"$merge": {}}])


def test_where_stage_rejected() -> None:
    assert "$where" in rejected(
        "aggregate", collection="orders", pipeline=[{"$match": {"$where": "this.a > 1"}}]
    )


# --------------------------------------------------------------------------- #
# Collections
# --------------------------------------------------------------------------- #


def test_unknown_collection_rejected() -> None:
    message = rejected("find", collection="nope", filter={})
    assert "nope" in message
    assert "orders" in message  # lists what does exist


def test_empty_collection_name_rejected() -> None:
    assert "required" in rejected("find", collection="", filter={})


def test_lookup_target_must_be_in_index() -> None:
    message = rejected(
        "aggregate",
        collection="orders",
        pipeline=[
            {"$lookup": {"from": "ghosts", "localField": "u", "foreignField": "_id", "as": "x"}}
        ],
    )
    assert "ghosts" in message


def test_lookup_known_target_allowed() -> None:
    valid(
        "aggregate",
        collection="orders",
        pipeline=[
            {"$match": {}},
            {"$lookup": {"from": "users", "localField": "u", "foreignField": "_id", "as": "x"}},
        ],
    )


def test_lookup_deep_target_rejected() -> None:
    message = rejected(
        "aggregate",
        collection="orders",
        pipeline=[
            {"$match": {}},
            {
                "$lookup": {
                    "from": "users",
                    "pipeline": [
                        {
                            "$lookup": {
                                "from": "ghosts",
                                "localField": "a",
                                "foreignField": "b",
                                "as": "y",
                            }
                        }
                    ],
                    "as": "x",
                }
            },
        ],
    )
    assert "ghosts" in message


def test_union_with_target_must_exist() -> None:
    assert "ghosts" in rejected(
        "aggregate", collection="orders", pipeline=[{"$unionWith": "ghosts"}]
    )
    valid("aggregate", collection="orders", pipeline=[{"$unionWith": "items"}])


def test_graph_lookup_target_must_exist() -> None:
    assert "ghosts" in rejected(
        "aggregate",
        collection="orders",
        pipeline=[
            {
                "$graphLookup": {
                    "from": "ghosts",
                    "startWith": "$p",
                    "connectFromField": "p",
                    "connectToField": "_id",
                    "as": "tree",
                }
            }
        ],
    )


# --------------------------------------------------------------------------- #
# Allowlists
# --------------------------------------------------------------------------- #


def test_unknown_dollar_operator_in_filter_rejected() -> None:
    message = rejected("find", collection="orders", filter={"a": {"$blah": 1}})
    assert "$blah" in message
    assert "allowlist" in message


def test_allowed_filter_operators_pass() -> None:
    valid(
        "find",
        collection="orders",
        filter={
            "$and": [
                {"total": {"$gte": 10, "$lt": 100}},
                {"status": {"$in": ["paid", "shipped"]}},
                {"email": {"$regex": "@example\\.com$"}},
                {"tags": {"$all": ["a", "b"]}},
                {"items": {"$elemMatch": {"qty": {"$gt": 0}}}},
            ]
        },
    )


def test_regex_without_allowlisted_operator_rejected() -> None:
    assert "$regex2" in rejected("find", collection="orders", filter={"a": {"$regex2": "/"}})


def test_unknown_operator_in_match_rejected() -> None:
    message = rejected(
        "aggregate", collection="orders", pipeline=[{"$match": {"a": {"$where": 1}}}]
    )
    assert "allowlist" in message or "$where" in message


# --------------------------------------------------------------------------- #
# Limits
# --------------------------------------------------------------------------- #


def test_pipeline_too_long_rejected() -> None:
    pipeline = [{"$match": {}}] * (MAX_PIPELINE_STAGES + 1)
    message = rejected("aggregate", collection="orders", pipeline=pipeline)
    assert str(MAX_PIPELINE_STAGES) in message


def test_pipeline_at_limit_allowed() -> None:
    valid("aggregate", collection="orders", pipeline=[{"$match": {}}] * MAX_PIPELINE_STAGES)


def _deep_pipeline(depth: int) -> list[dict[str, object]]:
    return [{"$facet": {"a": _deep_inner(depth)}}]


def _deep_inner(depth: int) -> list[dict[str, object]]:
    if depth <= 0:
        return [{"$match": {}}]
    return [{"$facet": {"b": _deep_inner(depth - 1)}}]


def test_nesting_depth_limit_holds() -> None:
    message = rejected(
        "aggregate", collection="orders", pipeline=_deep_pipeline(MAX_NESTING_DEPTH + 2)
    )
    assert "deeper than" in message or "depth" in message.lower()


def test_must_be_single_key_stages() -> None:
    message = rejected("aggregate", collection="orders", pipeline=[{"$match": {}, "$limit": 1}])
    assert "single-key" in message


def test_lookup_cannot_be_first_stage() -> None:
    message = rejected(
        "aggregate",
        collection="orders",
        pipeline=[
            {"$lookup": {"from": "users", "localField": "a", "foreignField": "b", "as": "c"}}
        ],
    )
    assert "first stage" in message


# --------------------------------------------------------------------------- #
# Operation argument rules
# --------------------------------------------------------------------------- #


def test_unknown_operation_rejected() -> None:
    assert "Unknown operation" in rejected("delete", collection="orders")


def test_aggregate_requires_pipeline() -> None:
    assert "pipeline" in rejected("aggregate", collection="orders")


def test_pipeline_rejected_for_find() -> None:
    assert "aggregate" in rejected("find", collection="orders", pipeline=[{"$match": {}}])


def test_distinct_requires_field() -> None:
    assert "field" in rejected("distinct", collection="orders")


def test_distinct_field_must_be_plain_path() -> None:
    assert "expression" in rejected("distinct", collection="orders", field="$total")


def test_field_only_for_distinct() -> None:
    assert "distinct" in rejected("find", collection="orders", field="status")


def test_filter_must_be_object() -> None:
    assert "object" in rejected("find", collection="orders", filter=[1, 2])  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["insert", "drop", "createIndexes", "update", "delete", "eval"])
def test_write_commands_rejected(name: str) -> None:
    with pytest.raises(UnsafeQueryError):
        validate_command(name)


def test_read_command_allowed() -> None:
    validate_command("find")
    validate_command("aggregate")
