"""Tests for schema index discovery, redaction, and caching."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from mongomock_motor import AsyncMongoMockClient

from mongochat.core.schema_index import (
    INDEX_TTL_HOURS,
    SchemaIndexBuilder,
    redact_document,
    redact_value,
)


@pytest.fixture
def db() -> Any:
    return AsyncMongoMockClient()["shop"]


def builder(db: Any, tmp_path: Path) -> SchemaIndexBuilder:
    return SchemaIndexBuilder(
        db, uri="mongodb://localhost:27017", database="shop", cache_dir=tmp_path
    )


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #


def test_redacts_email() -> None:
    assert redact_value("jo+work@example.com") == "[email redacted]"
    doc = redact_document({"contact": "jo@example.com", "name": "Jo"})
    assert doc["contact"] == "[email redacted]"
    assert doc["name"] == "Jo"


def test_redacts_token_like_strings() -> None:
    assert redact_value("sk-abcdef1234567890abcd") == "[token redacted]"
    assert redact_value("Bearer abcdefghijklmnop") == "[token redacted]"
    long_hex = "a" * 48
    assert redact_value(long_hex) == "[token redacted]"


def test_truncates_long_strings() -> None:
    long_text = "the quick brown fox jumps over the lazy dog. " * 8
    out = redact_value(long_text)
    assert len(out) < 100
    assert out.endswith("[truncated]")


def test_redaction_inside_nested_structures() -> None:
    doc = {"a": {"b": ["user@example.com", "short"]}}
    out = redact_document(doc)
    assert out["a"]["b"][0] == "[email redacted]"
    assert out["a"]["b"][1] == "short"


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


async def test_discovers_nested_paths_and_arrays(db: Any, tmp_path: Path) -> None:
    await db.orders.insert_many(
        [
            {
                "customer": {"name": "Ada", "email": "ada@example.com"},
                "items": [{"sku": "a", "qty": 2}, {"sku": "b", "qty": 3}],
                "total": 42,
                "placed_at": datetime.now(UTC),
            },
            {
                "customer": {"name": "Bob", "email": "bob@example.com"},
                "items": [{"sku": "c", "qty": 1}],
                "total": 7,
                "placed_at": datetime.now(UTC) - timedelta(days=3),
            },
        ]
    )
    index = await builder(db, tmp_path).load_or_build()
    coll = index.find_collection("orders")
    assert coll is not None
    paths = {a.path for a in coll.attributes}
    assert "customer.name" in paths
    assert "customer.email" in paths
    assert "items.$" in paths
    assert "items.$.sku" in paths
    assert "total" in paths

    total = coll.find_attribute("total")
    assert total is not None
    assert total.min == 7
    assert total.max == 42
    assert total.cardinality_estimate == 2

    email = coll.find_attribute("customer.email")
    assert email is not None
    # both addresses redact to the same placeholder, so only one unique sample
    assert email.sample_values == ["[email redacted]"]
    assert "email" in email.types or "string" in email.types


async def test_depth_cap_stops_very_deep_paths(db: Any, tmp_path: Path) -> None:
    deep = {"l1": {"l2": {"l3": {"l4": {"l5": {"l6": "too deep"}}}}}}
    await db.things.insert_one(deep)
    index = await builder(db, tmp_path).load_or_build()
    coll = index.find_collection("things")
    assert coll is not None
    paths = {a.path for a in coll.attributes}
    assert "l1.l2.l3.l4" in paths
    assert "l1.l2.l3.l4.l5.l6" not in paths


async def test_indexes_and_counts_are_recorded(db: Any, tmp_path: Path) -> None:
    await db.orders.insert_many([{"a": i} for i in range(5)])
    await db.orders.create_index("a")
    index = await builder(db, tmp_path).load_or_build()
    coll = index.find_collection("orders")
    assert coll is not None
    assert coll.document_count == 5
    assert "_id_" in coll.indexes
    assert "a_1" in coll.indexes
    a = coll.find_attribute("a")
    assert a is not None
    assert a.indexed is True


async def test_sample_documents_are_redacted(db: Any, tmp_path: Path) -> None:
    await db.users.insert_many(
        [{"name": "Ada", "email": "ada@example.com", "bio": "z" * 400} for _ in range(3)]
    )
    index = await builder(db, tmp_path).load_or_build()
    coll = index.find_collection("users")
    assert coll is not None
    assert len(coll.sample_documents) <= 2
    doc = coll.sample_documents[0]
    assert doc["email"] == "[email redacted]"
    assert len(str(doc["bio"])) < 120


async def test_empty_database_yields_empty_index(db: Any, tmp_path: Path) -> None:
    index = await builder(db, tmp_path).load_or_build()
    assert index.collections == []
    assert "no collections" in index.to_compact_prompt()


def test_compact_prompt_mentions_collections(db: Any, tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from mongochat.types import AttributeInfo, CollectionIndex, SchemaIndex

    index = SchemaIndex(
        database="shop",
        generated_at=datetime.now(UTC),
        collections=[
            CollectionIndex(
                name="orders",
                document_count=3,
                attributes=[
                    AttributeInfo(path="total", types=["int"], cardinality_estimate=3, indexed=True)
                ],
            )
        ],
    )
    text = index.to_compact_prompt()
    assert "collection orders" in text
    assert "total" in text
    assert "3 distinct" in text


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #


async def test_cache_is_written_and_reused(db: Any, tmp_path: Path) -> None:
    await db.orders.insert_one({"a": 1})
    b = builder(db, tmp_path)
    first = await b.load_or_build()
    assert b.cache_path.is_file()
    assert b.cache_path.read_text(encoding="utf-8")

    # second builder sees the file and loads instead of rebuilding
    second = await builder(db, tmp_path).load_or_build()
    assert second.generated_at == first.generated_at


async def test_cache_ttl_expiry(db: Any, tmp_path: Path) -> None:
    await db.orders.insert_one({"a": 1})
    b = builder(db, tmp_path)
    index = await b.build()
    b.save(index)

    # age the file beyond the TTL
    stale = index.model_copy(
        update={"generated_at": datetime.now(UTC) - timedelta(hours=INDEX_TTL_HOURS + 1)}
    )
    b.save(stale)
    assert b.load() is None


async def test_cache_fresh_within_ttl(db: Any, tmp_path: Path) -> None:
    b = builder(db, tmp_path)
    index = await b.build()
    b.save(index)
    assert b.load() is not None


async def test_reset_deletes_cache(db: Any, tmp_path: Path) -> None:
    b = builder(db, tmp_path)
    index = await b.build()
    b.save(index)
    assert b.cache_path.exists()
    assert b.reset() is True
    assert not b.cache_path.exists()
    assert b.reset() is False  # already gone


async def test_corrupt_cache_is_ignored(db: Any, tmp_path: Path) -> None:
    b = builder(db, tmp_path)
    b.cache_path.parent.mkdir(parents=True, exist_ok=True)
    b.cache_path.write_text("{not json", encoding="utf-8")
    assert b.load() is None
    index = await b.load_or_build()
    assert index.database == "shop"


async def test_refresh_rebuilds(db: Any, tmp_path: Path) -> None:
    b = builder(db, tmp_path)
    await b.build()
    b.save(await b.build())
    await db.orders.insert_one({"fresh": True})
    refreshed = await b.refresh()
    assert refreshed.find_collection("orders") is not None


async def test_show_summary(db: Any, tmp_path: Path) -> None:
    await db.orders.insert_one({"a": 1})
    b = builder(db, tmp_path)
    index = await b.build()
    summary = b.show(index)
    assert summary["database"] == "shop"
    assert summary["collections"][0]["name"] == "orders"
    assert summary["collections"][0]["document_count"] == 1
