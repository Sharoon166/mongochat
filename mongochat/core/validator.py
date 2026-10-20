"""Security-critical query validation.

Pure functions: no I/O, no driver, no mutable module state. Everything that reaches
the executor passes through :func:`validate_query` first.

The rules:
  * operations are limited to find / aggregate / count / distinct
  * the collection must exist in the schema index
  * ``$out``, ``$merge``, ``$function``, ``$accumulator``, ``$where``, ``$eval`` are
    rejected at every depth, including inside ``$facet``, ``$lookup`` sub-pipelines
    and ``$expr``
  * ``$lookup`` / ``$unionWith`` targets must be collections present in the index
  * filters and ``$match`` use an allowlist of query operators; unknown ``$`` keys fail
  * pipelines are capped at 20 stages and 6 levels of nesting
"""

from __future__ import annotations

from typing import Any, Literal, cast

from pydantic import BaseModel, Field

from mongochat.types import SchemaIndex, UnsafeQueryError

Operation = Literal["find", "aggregate", "count", "distinct"]

MAX_PIPELINE_STAGES = 20
MAX_NESTING_DEPTH = 6

#: Always forbidden, at any depth, in any context.
FORBIDDEN_OPERATORS: frozenset[str] = frozenset(
    {
        "$out",
        "$merge",
        "$function",
        "$accumulator",
        "$where",
        "$eval",
        "$jsonSchema",
        "$currentOp",
        "$indexStats",
        "$collStats",
        "$planCacheStats",
        "$sampleRate",
        "$text",
        "$geoNear",
    }
)

#: Aggregation stages allowed in a read-only pipeline.
ALLOWED_STAGES: frozenset[str] = frozenset(
    {
        "$match",
        "$project",
        "$group",
        "$sort",
        "$skip",
        "$limit",
        "$unwind",
        "$count",
        "$addFields",
        "$set",
        "$unset",
        "$replaceRoot",
        "$replaceWith",
        "$bucket",
        "$bucketAuto",
        "$facet",
        "$graphLookup",
        "$lookup",
        "$sample",
        "$sortByCount",
        "$unionWith",
        "$densify",
        "$fill",
        "$setWindowFields",
        "$redact",
    }
)

#: Query operators permitted in filters, ``$match`` bodies and expressions.
ALLOWED_FILTER_OPERATORS: frozenset[str] = frozenset(
    {
        # comparison
        "$eq",
        "$ne",
        "$gt",
        "$gte",
        "$lt",
        "$lte",
        "$in",
        "$nin",
        # logical
        "$and",
        "$or",
        "$nor",
        "$not",
        # element
        "$exists",
        "$type",
        # evaluation / array
        "$regex",
        "$options",
        "$mod",
        "$all",
        "$size",
        "$elemMatch",
        "$slice",
        "$meta",
        # aggregation expressions (used inside $expr, $project, $group)
        "$cond",
        "$ifNull",
        "$switch",
        "$let",
        "$map",
        "$filter",
        "$reduce",
        "$add",
        "$subtract",
        "$multiply",
        "$divide",
        "$abs",
        "$ceil",
        "$floor",
        "$round",
        "$sqrt",
        "$pow",
        "$avg",
        "$sum",
        "$min",
        "$max",
        "$first",
        "$last",
        "$push",
        "$addToSet",
        "$literal",
        "$range",
        "$concat",
        "$substr",
        "$substrCP",
        "$toLower",
        "$toUpper",
        "$trim",
        "$split",
        "$strLenCP",
        "$indexOfCP",
        "$replaceAll",
        "$regexMatch",
        "$regexFind",
        "$regexFindAll",
        "$year",
        "$month",
        "$dayOfMonth",
        "$dayOfWeek",
        "$hour",
        "$minute",
        "$second",
        "$week",
        "$isoWeek",
        "$isoYear",
        "$dateToString",
        "$dateFromString",
        "$dateAdd",
        "$dateDiff",
        "$toDate",
        "$toString",
        "$toDouble",
        "$toInt",
        "$toLong",
        "$toDecimal",
        "$convert",
        "$isArray",
        "$isNumber",
        "$objectToArray",
        "$arrayToObject",
        "$zip",
        "$concatArrays",
        "$indexOfArray",
        "$getField",
        "$setField",
        "$unsetField",
        "$bsonSize",
        # query-level operators
        "$expr",
        "$comment",
    }
)

#: Commands that write, run JavaScript, or are administrative.
WRITE_COMMANDS: frozenset[str] = frozenset(
    {
        "insert",
        "update",
        "delete",
        "drop",
        "dropdatabase",
        "create",
        "createindexes",
        "dropindexes",
        "renamecollection",
        "collmod",
        "converttocapped",
        "emptycapped",
        "findandmodify",
        "eval",
        "clone",
        "clonecollection",
        "clonecollectionascapped",
        "compact",
        "repaircursor",
        "repairdatabase",
        "copydb",
        "copydbfrom",
        "shutdown",
        "replset",
        "authenticate",
        "saslstart",
        "saslcontinue",
        "profile",
        "logrotate",
        "setdefaultrwconcern",
        "top",
        "killsession",
        "aborttransaction",
        "committransaction",
        "mapreduce",
    }
)


class ValidatedQuery(BaseModel):
    """A query that has passed validation and is safe to execute."""

    collection: str
    operation: Operation
    filter: dict[str, Any] = Field(default_factory=dict)
    pipeline: list[dict[str, Any]] = Field(default_factory=list)
    field: str | None = None


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def validate_query(
    *,
    index: SchemaIndex,
    collection: str,
    operation: str,
    filter: dict[str, Any] | None = None,
    pipeline: list[dict[str, Any]] | None = None,
    field: str | None = None,
) -> ValidatedQuery:
    """Validate one read-only query.

    Raises :class:`UnsafeQueryError` with a specific, model-actionable reason.
    """
    if not isinstance(operation, str) or operation.lower() not in {
        "find",
        "aggregate",
        "count",
        "distinct",
    }:
        raise UnsafeQueryError(
            f"Unknown operation {operation!r}. Use one of: find, aggregate, count, distinct."
        )
    op = operation.lower()

    if not isinstance(collection, str) or not collection.strip():
        raise UnsafeQueryError("A collection name is required.")
    if index.find_collection(collection) is None:
        known = ", ".join(index.collection_names[:15]) or "(none)"
        raise UnsafeQueryError(
            f"Collection {collection!r} does not exist in the index. Known collections: {known}."
        )

    filt = _as_object(filter, "filter")
    if filt:
        _validate_filter(filt, depth=0, path="filter")

    if op == "aggregate":
        if not isinstance(pipeline, list) or not pipeline:
            raise UnsafeQueryError(
                "aggregate requires a non-empty `pipeline` array of stage objects."
            )
        if len(pipeline) > MAX_PIPELINE_STAGES:
            raise UnsafeQueryError(
                f"Pipeline has {len(pipeline)} stages; the maximum is {MAX_PIPELINE_STAGES}."
            )
        for i, stage in enumerate(pipeline):
            if not isinstance(stage, dict) or len(stage) != 1:
                raise UnsafeQueryError(
                    f"Stage {i} must be a single-key object like {{'$match': {{...}}}}, "
                    f"got {repr(stage)[:120]}."
                )
            _validate_stage(stage, index, depth=0, path=f"pipeline[{i}]")
        first_stage = next(iter(pipeline[0]))
        if first_stage == "$lookup":
            raise UnsafeQueryError(
                "$lookup must not be the first stage; begin with $match, $limit or $sample."
            )
    elif pipeline:
        raise UnsafeQueryError(f"`pipeline` is only valid for the aggregate operation, not {op!r}.")

    if op == "distinct":
        if not isinstance(field, str) or not field.strip():
            raise UnsafeQueryError("distinct requires a `field` argument naming a field path.")
        if field.startswith("$"):
            raise UnsafeQueryError(
                f"distinct `field` must be a plain field path, not an expression ({field!r})."
            )
    elif field is not None:
        raise UnsafeQueryError(f"`field` is only valid for the distinct operation, not {op!r}.")

    return ValidatedQuery(
        collection=collection,
        operation=cast(Operation, op),
        filter=filt,
        pipeline=pipeline or [],
        field=field,
    )


def validate_command(name: str) -> None:
    """Reject anything that is not an explicitly read-only command."""
    lowered = (name or "").lower().lstrip("$")
    if lowered in WRITE_COMMANDS:
        raise UnsafeQueryError(
            f"The command {name!r} is not allowed: mongochat only runs read-only queries."
        )


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _as_object(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise UnsafeQueryError(f"`{label}` must be a JSON object, got {type(value).__name__}.")
    return value


def _validate_filter(node: Any, *, depth: int, path: str) -> None:
    if depth > MAX_NESTING_DEPTH:
        raise UnsafeQueryError(f"Query nests deeper than {MAX_NESTING_DEPTH} levels at {path}.")
    if isinstance(node, dict):
        for key, value in node.items():
            if not isinstance(key, str):
                raise UnsafeQueryError(f"Object keys must be strings at {path}.")
            if key.startswith("$"):
                _validate_operator_value(key, value, depth=depth, path=f"{path}.{key}")
            else:
                _validate_filter(value, depth=depth + 1, path=f"{path}.{key}")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            if i > 500:
                break
            _validate_filter(item, depth=depth + 1, path=f"{path}[{i}]")


def _validate_operator_value(key: str, value: Any, *, depth: int, path: str) -> None:
    if key in FORBIDDEN_OPERATORS:
        raise UnsafeQueryError(
            f"Forbidden operator {key} at {path}. mongochat only runs read-only queries; "
            "remove it and try again."
        )
    if key not in ALLOWED_FILTER_OPERATORS:
        raise UnsafeQueryError(
            f"Operator {key} at {path} is not in the allowlist. "
            f"Allowed operators: {', '.join(sorted(ALLOWED_FILTER_OPERATORS))}."
        )
    if key in {"$and", "$or", "$nor"}:
        if not isinstance(value, list):
            raise UnsafeQueryError(f"{key} at {path} must be an array of objects.")
        for i, sub in enumerate(value):
            _validate_filter(sub, depth=depth + 1, path=f"{path}[{i}]")
        return
    if key in {"$not", "$expr", "$elemMatch"}:
        _validate_filter(value, depth=depth + 1, path=path)
        return
    if isinstance(value, dict):
        _validate_filter(value, depth=depth + 1, path=path)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _validate_filter(item, depth=depth + 1, path=f"{path}[{i}]")


def _validate_stage(stage: dict[str, Any], index: SchemaIndex, *, depth: int, path: str) -> None:
    if depth > MAX_NESTING_DEPTH:
        raise UnsafeQueryError(f"Pipeline nests deeper than {MAX_NESTING_DEPTH} levels at {path}.")
    name = next(iter(stage))
    body = stage[name]

    if name in FORBIDDEN_OPERATORS or name in {"$out", "$merge"}:
        raise UnsafeQueryError(
            f"Stage {name} at {path} writes to the database or runs JavaScript and is not allowed."
        )
    if name not in ALLOWED_STAGES:
        allowed = ", ".join(sorted(s for s in ALLOWED_STAGES))
        raise UnsafeQueryError(f"Stage {name} at {path} is not allowed. Allowed stages: {allowed}.")

    sub = f"{path}{name}"
    if name == "$facet":
        if not isinstance(body, dict):
            raise UnsafeQueryError(f"$facet at {path} must be an object of sub-pipelines.")
        for key, pipe in body.items():
            _validate_subpipeline(pipe, index, depth=depth + 1, path=f"{path}.$facet.{key}")
    elif name == "$lookup":
        _validate_lookup(body, index, depth=depth + 1, path=f"{path}.$lookup")
    elif name == "$unionWith":
        _validate_union_with(body, index, depth=depth + 1, path=f"{path}.$unionWith")
    elif name == "$graphLookup":
        if not isinstance(body, dict):
            raise UnsafeQueryError(f"$graphLookup at {path} must be an object.")
        target = body.get("from")
        if not isinstance(target, str) or index.find_collection(target) is None:
            raise UnsafeQueryError(
                f"$graphLookup target {target!r} at {path} is not in the schema index."
            )
        if "pipeline" in body:
            _validate_subpipeline(
                body["pipeline"], index, depth=depth + 1, path=f"{path}.$graphLookup.pipeline"
            )
        _validate_filter(body, depth=depth + 1, path=f"{path}.$graphLookup")
    elif name == "$unset":
        if not isinstance(body, (str, list)):
            raise UnsafeQueryError(f"$unset at {path} must be a string or array of strings.")
    elif name == "$replaceRoot" or name == "$replaceWith":
        if isinstance(body, dict) and "newRoot" in body:
            _validate_filter(body, depth=depth + 1, path=sub)
        elif isinstance(body, str) and body.startswith("$"):
            return
        elif not isinstance(body, (dict, str)):
            raise UnsafeQueryError(f"{name} at {path} must be an object or field path.")
    elif name in {"$count", "$sortByCount"}:
        if not isinstance(body, str):
            raise UnsafeQueryError(f"{name} at {path} must be a plain field name string.")
    elif name == "$sample":
        if not isinstance(body, dict) or "size" not in body:
            raise UnsafeQueryError(f"$sample at {path} requires an object like {{'size': 5}}.")
    elif isinstance(body, dict):
        _validate_filter(body, depth=depth + 1, path=sub)
    elif isinstance(body, list):
        for i, item in enumerate(body):
            _validate_filter(item, depth=depth + 1, path=f"{sub}[{i}]")
    elif isinstance(body, (int, float, str)):
        return
    else:
        raise UnsafeQueryError(f"Stage {name} at {path} has an unsupported value.")


def _validate_subpipeline(body: Any, index: SchemaIndex, *, depth: int, path: str) -> None:
    if not isinstance(body, list):
        raise UnsafeQueryError(f"Expected an array of stages at {path}.")
    if len(body) > MAX_PIPELINE_STAGES:
        raise UnsafeQueryError(
            f"Sub-pipeline at {path} has {len(body)} stages; the maximum is {MAX_PIPELINE_STAGES}."
        )
    if depth > MAX_NESTING_DEPTH:
        raise UnsafeQueryError(f"Pipeline nests deeper than {MAX_NESTING_DEPTH} levels at {path}.")
    for i, stage in enumerate(body):
        if not isinstance(stage, dict) or len(stage) != 1:
            raise UnsafeQueryError(f"Stage {path}[{i}] must be a single-key object.")
        _validate_stage(stage, index, depth=depth + 1, path=f"{path}[{i}]")


def _validate_lookup(body: Any, index: SchemaIndex, *, depth: int, path: str) -> None:
    if isinstance(body, str):
        target = body
        return _require_known(target, index, path, "$lookup")
    if not isinstance(body, dict):
        raise UnsafeQueryError(f"$lookup at {path} must be an object with a `from` field.")
    from_coll = body.get("from")
    if not isinstance(from_coll, str) or not from_coll:
        raise UnsafeQueryError(f"$lookup at {path} requires a `from` collection name.")
    _require_known(from_coll, index, path, "$lookup")
    if "pipeline" in body:
        _validate_subpipeline(body["pipeline"], index, depth=depth + 1, path=f"{path}.pipeline")
    for key in ("localField", "foreignField", "as"):
        value = body.get(key)
        if value is not None and not isinstance(value, str):
            raise UnsafeQueryError(f"$lookup `{key}` at {path} must be a string.")
    unknown = set(body) - {"from", "localField", "foreignField", "as", "pipeline"}
    if unknown:
        raise UnsafeQueryError(f"$lookup has unsupported keys {sorted(unknown)} at {path}.")


def _validate_union_with(body: Any, index: SchemaIndex, *, depth: int, path: str) -> None:
    if isinstance(body, str):
        return _require_known(body, index, path, "$unionWith")
    if not isinstance(body, dict):
        raise UnsafeQueryError(f"$unionWith at {path} must be an object with a `coll` field.")
    target = body.get("coll")
    if not isinstance(target, str) or not target:
        raise UnsafeQueryError(f"$unionWith at {path} requires a `coll` collection name.")
    _require_known(target, index, path, "$unionWith")
    if "pipeline" in body:
        _validate_subpipeline(body["pipeline"], index, depth=depth + 1, path=f"{path}.pipeline")


def _require_known(target: str, index: SchemaIndex, path: str, stage: str) -> None:
    if index.find_collection(target) is None:
        raise UnsafeQueryError(
            f"{stage} target {target!r} at {path} is not in the schema index. "
            f"Known collections: {', '.join(index.collection_names[:15]) or '(none)'}."
        )


__all__ = [
    "ALLOWED_FILTER_OPERATORS",
    "ALLOWED_STAGES",
    "FORBIDDEN_OPERATORS",
    "MAX_NESTING_DEPTH",
    "MAX_PIPELINE_STAGES",
    "ValidatedQuery",
    "validate_command",
    "validate_query",
]
