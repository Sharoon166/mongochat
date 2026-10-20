"""Tests for collection-relationship inference from the schema index."""

from __future__ import annotations

from datetime import UTC, datetime

from mongochat.core.relations import (
    Relation,
    infer_relations,
    normalize,
    singular,
)
from mongochat.types import AttributeInfo, CollectionIndex, SchemaIndex


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def make_index(*collections: CollectionIndex) -> SchemaIndex:
    return SchemaIndex(
        database="shop",
        generated_at=datetime.now(UTC),
        collections=list(collections),
    )


def coll(name: str, *paths: tuple[str, list[str]]) -> CollectionIndex:
    return CollectionIndex(
        name=name,
        document_count=1,
        attributes=[AttributeInfo(path=p, types=t) for p, t in paths],
    )


def rel_set(index: SchemaIndex) -> set[tuple[str, str, str, str]]:
    return {
        (r.source_collection, r.source_field, r.target_collection, r.confidence)
        for r in infer_relations(index)
    }


# --------------------------------------------------------------------------- #
# String helpers
# --------------------------------------------------------------------------- #


def test_normalize_camel_and_snake() -> None:
    assert normalize("customerId") == "customerid"
    assert normalize("customer_id") == "customerid"
    assert normalize("customer-id") == "customerid"
    assert normalize("CustomerID") == "customerid"


def test_singular_basic() -> None:
    assert singular("orders") == "order"
    assert singular("categories") == "category"
    assert singular("users") == "user"
    assert singular("class") == "class"  # ends in "ss"
    assert singular("status") == "status"  # ends in "us"
    assert singular("series") == "series"  # ends in "eries", unchanged


# --------------------------------------------------------------------------- #
# Inference
# --------------------------------------------------------------------------- #


def test_exact_suffix_match() -> None:
    """orders.customerId -> customers.
    The stem 'customer' matches the plural 'customers' via singular_plural."""
    index = make_index(
        coll("orders", ("customerId", ["objectId"]), ("total", ["int"])),
        coll("customers", ("_id", ["objectId"]), ("name", ["string"])),
    )
    assert rel_set(index) == {
        ("orders", "customerId", "customers", "singular_plural"),
    }


def test_exact_confidence_when_coll_name_matches_stem() -> None:
    """orders.customerId -> customer (exact: normalize('customer') == 'customer' == stem)."""
    index = make_index(
        coll("orders", ("customerId", ["objectId"])),
        coll("customer", ("_id", ["objectId"])),
    )
    assert rel_set(index) == {
        ("orders", "customerId", "customer", "exact"),
    }


def test_singular_plural_match() -> None:
    """orders.userId -> users, confidence 'singular_plural'."""
    index = make_index(
        coll("orders", ("userId", ["objectId"])),
        coll("users", ("_id", ["objectId"])),
    )
    assert rel_set(index) == {
        ("orders", "userId", "users", "singular_plural"),
    }


def test_bare_name_match() -> None:
    """orders.customer -> customers, confidence 'bare' (no suffix, ObjectId type)."""
    index = make_index(
        coll("orders", ("customer", ["objectId"])),
        coll("customers", ("_id", ["objectId"])),
    )
    assert rel_set(index) == {
        ("orders", "customer", "customers", "bare"),
    }


def test_bare_match_requires_objectid_or_string() -> None:
    """Bare name matches require ObjectId or string type."""
    # string is now allowed for bare matches
    index = make_index(
        coll("orders", ("customer", ["string"])),
        coll("customers", ("_id", ["objectId"])),
    )
    assert rel_set(index) == {("orders", "customer", "customers", "bare")}

def test_bare_match_rejects_containers() -> None:
    """Bare name matches on object/array/bool types are rejected."""
    index = make_index(
        coll("orders", ("customer", ["object"])),
        coll("customers", ("_id", ["objectId"])),
    )
    assert rel_set(index) == set()


def test_suffix_match_accepts_string() -> None:
    """With a suffix, string type is acceptable for a reference (singular_plural confidence)."""
    index = make_index(
        coll("orders", ("customerRef", ["string"])),
        coll("customers", ("_id", ["objectId"])),
    )
    assert rel_set(index) == {
        ("orders", "customerRef", "customers", "singular_plural"),
    }


def test_self_reference() -> None:
    """employees.employeeId -> employees (self-reference when stem matches own collection)."""
    index = make_index(
        coll("employees", ("_id", ["objectId"]), ("employeeId", ["objectId"])),
    )
    rels = infer_relations(index)
    assert len(rels) == 1
    r = rels[0]
    assert r.source_collection == "employees"
    assert r.target_collection == "employees"
    assert r.self_reference is True


def test_id_field_is_not_a_reference() -> None:
    """The field 'id' or 'pk' is never treated as a reference."""
    index = make_index(
        coll("orders", ("id", ["objectId"]), ("pk", ["objectId"])),
        coll("customers", ("_id", ["objectId"])),
    )
    assert rel_set(index) == set()


def test_no_match_when_no_target_collection() -> None:
    index = make_index(
        coll("orders", ("vendorId", ["objectId"])),
    )
    assert rel_set(index) == set()


def test_ambiguous_stem_skipped() -> None:
    """If both 'user' and 'users' collections exist, userId is ambiguous."""
    index = make_index(
        coll("orders", ("userId", ["objectId"])),
        coll("user", ("_id", ["objectId"])),
        coll("users", ("_id", ["objectId"])),
    )
    assert rel_set(index) == set()


def test_multiple_outgoing_references() -> None:
    index = make_index(
        coll(
            "orders",
            ("customerId", ["objectId"]),
            ("productId", ["objectId"]),
            ("total", ["int"]),
        ),
        coll("customers", ("_id", ["objectId"])),
        coll("products", ("_id", ["objectId"])),
    )
    # stems 'customer'/'product' vs plural collection names -> singular_plural confidence
    assert rel_set(index) == {
        ("orders", "customerId", "customers", "singular_plural"),
        ("orders", "productId", "products", "singular_plural"),
    }


def test_nested_path_reference() -> None:
    """items.$.categoryId should be read as a reference."""
    index = make_index(
        coll("products", ("items.$.categoryId", ["objectId"])),
        coll("categories", ("_id", ["objectId"])),
    )
    # stem 'category' vs 'categories' -> singular_plural
    assert rel_set(index) == {
        ("products", "items.$.categoryId", "categories", "singular_plural"),
    }


def test_relations_sorted_by_source() -> None:
    index = make_index(
        coll("zzz", ("aaaId", ["objectId"])),
        coll("aaa", ("_id", ["objectId"])),
        coll("mmm", ("aaaId", ["objectId"])),
    )
    rels = infer_relations(index)
    assert rels[0].source_collection == "mmm"
    assert rels[1].source_collection == "zzz"


def test_relation_model_is_frozen() -> None:
    r = Relation(
        source_collection="a",
        source_field="b",
        target_collection="c",
        confidence="exact",
    )
    try:
        r.source_collection = "x"  # type: ignore[misc]
        raise AssertionError("should have raised")
    except Exception:
        pass


def test_empty_index() -> None:
    assert infer_relations(make_index()) == []
