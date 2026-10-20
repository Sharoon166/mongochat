"""Build, cache, load, refresh, reset, and summarize the schema index.

The index is what lets the model ask sensible questions without exploring blindly:
per collection we record counts, sizes, indexes, discovered attribute paths with
types/cardinality/min-max, and two redacted sample documents.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mongochat.types import (
    AttributeInfo,
    CollectionIndex,
    SchemaIndex,
    bson_type_name,
    to_jsonable,
)

logger = logging.getLogger("mongochat.schema_index")

INDEX_TTL_HOURS = 24
SAMPLE_SIZE = 1000
MAX_PATH_DEPTH = 4
MAX_ARRAY_ITEMS = 10
MAX_TYPES = 4
MAX_SAMPLE_VALUES = 4
MAX_CARDINALITY_BUCKETS = 512
STRING_TRUNCATE = 80
KEPT_SAMPLE_DOCS = 2

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_TOKEN_RE = re.compile(
    r"^(?:sk-[A-Za-z0-9_\-]{8,}|Bearer\s+\S+|ghp_\w+|xox[baprs]-\w+|eyJ[\w\-]+\.[\w\-]+\.[\w\-]+|"
    r"[A-Za-z0-9_\-]{32,})$",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #


def redact_value(value: Any) -> Any:
    """Mask emails and token-like strings, truncate long strings."""
    if isinstance(value, str):
        if _EMAIL_RE.search(value):
            return _EMAIL_RE.sub("[email redacted]", value)
        if _TOKEN_RE.match(value.strip()):
            return "[token redacted]"
        if len(value) > STRING_TRUNCATE:
            return value[:STRING_TRUNCATE] + "...[truncated]"
        return value
    if isinstance(value, dict):
        return {str(k): redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    return value


def redact_document(doc: dict[str, Any]) -> dict[str, Any]:
    """Redact one sample document."""
    return {str(k): redact_value(v) for k, v in doc.items()}


# --------------------------------------------------------------------------- #
# Path discovery
# --------------------------------------------------------------------------- #


class _AttributeAccumulator:
    __slots__ = ("cardinality", "cardinality_overflow", "max", "min", "path", "samples", "types")

    def __init__(self, path: str) -> None:
        self.path = path
        self.types: set[str] = set()
        self.samples: list[Any] = []
        self.cardinality: set[Any] = set()
        self.cardinality_overflow = False
        self.min: Any = None
        self.max: Any = None

    def observe(self, value: Any) -> None:
        type_name = bson_type_name(value)
        self.types.add(type_name)

        if len(self.samples) < MAX_SAMPLE_VALUES:
            rendered = _sample_repr(value)
            if rendered not in self.samples:
                self.samples.append(rendered)

        if type_name in {"object", "array"}:
            return

        key = value
        if isinstance(value, (dict, list, set)):
            key = repr(value)
        try:
            if len(self.cardinality) < MAX_CARDINALITY_BUCKETS:
                self.cardinality.add(key)
            else:
                self.cardinality_overflow = True
        except TypeError:
            pass

        numeric = _as_number(value)
        if numeric is not None:
            self.min = numeric if self.min is None else min(self.min, numeric)
            self.max = numeric if self.max is None else max(self.max, numeric)
        elif isinstance(value, datetime):
            self.min = value if self.min is None else min(self.min, value)
            self.max = value if self.max is None else max(self.max, value)


def _sample_repr(value: Any) -> Any:
    """A small JSON-safe representation for a sample value."""
    if isinstance(value, (str, bool, int, float)) or value is None:
        if isinstance(value, str) and len(value) > STRING_TRUNCATE:
            return redact_value(value)
        if isinstance(value, str):
            return redact_value(value)
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    rendered = to_jsonable(value)
    text = json.dumps(rendered, default=str)
    if len(text) > STRING_TRUNCATE:
        return text[:STRING_TRUNCATE] + "...[truncated]"
    return text


def _as_number(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    dec = getattr(value, "to_decimal", None)
    if callable(dec):
        try:
            return float(dec())
        except Exception:
            return None
    return None


def _walk(value: Any, prefix: str, depth: int, found: dict[str, _AttributeAccumulator]) -> None:
    if depth > MAX_PATH_DEPTH:
        return
    if isinstance(value, dict):
        for key, sub in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            acc = found.setdefault(path, _AttributeAccumulator(path))
            acc.observe(sub)
            _walk(sub, path, depth + 1, found)
    elif isinstance(value, list):
        arr_path = f"{prefix}.$"
        acc = found.setdefault(arr_path, _AttributeAccumulator(arr_path))
        for item in value[:MAX_ARRAY_ITEMS]:
            acc.observe(item)
            _walk(item, arr_path, depth + 1, found)


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #


class SchemaIndexBuilder:
    """Builds and caches a :class:`SchemaIndex` for one host + database."""

    def __init__(self, db: Any, *, uri: str, database: str, cache_dir: Path | None = None) -> None:
        self.db = db
        self.uri = uri
        self.database = database
        self.cache_dir = cache_dir or Path.home() / ".mongochat"
        self.cache_path = self.cache_dir / f"index-{_host_label(uri)}-{_safe_name(database)}.json"

    # -- cache -------------------------------------------------------------- #

    def load(self, *, ttl_hours: int = INDEX_TTL_HOURS) -> SchemaIndex | None:
        """Return a fresh cached index, or None."""
        path = self.cache_path
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            generated = datetime.fromisoformat(raw["generated_at"])
            if generated.tzinfo is None:
                generated = generated.replace(tzinfo=UTC)
        except (OSError, ValueError, KeyError, TypeError):
            logger.debug("Ignoring unreadable index cache at %s", path)
            return None
        if datetime.now(UTC) - generated > timedelta(hours=ttl_hours):
            logger.debug("Index cache expired at %s", path)
            return None
        try:
            return SchemaIndex.model_validate(raw)
        except Exception:
            logger.debug("Index cache at %s did not validate", path)
            return None

    def save(self, index: SchemaIndex) -> Path:
        """Persist the index with 0600 permissions."""
        path = self.cache_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = index.model_dump(mode="json")
        text = json.dumps(payload, default=str)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        with suppress(OSError):  # pragma: no cover - Windows ACLs
            os.chmod(tmp, 0o600)
        tmp.replace(path)
        return path

    def reset(self) -> bool:
        """Delete the cache file for this host and database."""
        try:
            self.cache_path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:  # pragma: no cover
            return False

    # -- build -------------------------------------------------------------- #

    async def build(self, *, sample_size: int = SAMPLE_SIZE) -> SchemaIndex:
        """Scan every collection and build a fresh index."""
        started = time.perf_counter()
        names: list[str] = sorted(await self.db.list_collection_names())
        collections: list[CollectionIndex] = []
        for name in sorted(names):
            try:
                collections.append(await self._build_collection(name, sample_size=sample_size))
            except Exception as exc:
                logger.warning("Skipping collection %s: %s", name, exc)
        index = SchemaIndex(
            database=self.database,
            generated_at=datetime.now(UTC),
            collections=collections,
        )
        logger.debug(
            "Built index for %s.%s: %d collections in %.0fms",
            self.uri,
            self.database,
            len(collections),
            (time.perf_counter() - started) * 1000,
        )
        return index

    async def _build_collection(self, name: str, *, sample_size: int) -> CollectionIndex:
        coll = self.db[name]
        document_count = await coll.count_documents({})
        avg_doc_size = 0
        try:
            stats = await coll.aggregate([{"$collStats": {"storageStats": {}}}]).to_list(1)
            if stats:
                avg_doc_size = int(stats[0].get("storageStats", {}).get("avgObjSize") or 0)
        except Exception:
            avg_doc_size = 0

        indexes: list[str] = []
        indexed_paths: set[str] = set()
        try:
            async for idx in coll.list_indexes():
                name_ = idx.get("name", "")
                indexes.append(name_)
                for key in idx.get("key", {}):
                    indexed_paths.add(key)
        except Exception:
            indexes = []

        sample_size_actual = (
            sample_size if document_count >= sample_size else max(document_count, 1)
        )
        docs: list[dict[str, Any]] = []
        if document_count > 0:
            if document_count <= sample_size:
                cursor = coll.find({}).limit(sample_size)
                docs = [d async for d in cursor]
            else:
                cursor = coll.aggregate([{"$sample": {"size": sample_size_actual}}])
                docs = [d async for d in cursor]

        found: dict[str, _AttributeAccumulator] = {}
        for doc in docs:
            if isinstance(doc, dict):
                _walk(doc, "", 1, found)

        attributes = [
            _to_attribute(acc, indexed_paths)
            for acc in sorted(found.values(), key=lambda a: a.path)
        ]

        redacted_samples = [redact_document(d) for d in docs[:KEPT_SAMPLE_DOCS]]
        redacted_samples = [to_jsonable(s) for s in redacted_samples]

        return CollectionIndex(
            name=name,
            document_count=document_count,
            avg_doc_size=avg_doc_size,
            attributes=attributes,
            indexes=indexes,
            sample_documents=redacted_samples,
        )

    # -- public conveniences ------------------------------------------------ #

    async def load_or_build(self, *, ttl_hours: int = INDEX_TTL_HOURS) -> SchemaIndex:
        cached = self.load(ttl_hours=ttl_hours)
        if cached is not None:
            return cached
        index = await self.build()
        self.save(index)
        return index

    async def refresh(self) -> SchemaIndex:
        index = await self.build()
        self.save(index)
        return index

    def show(self, index: SchemaIndex) -> dict[str, Any]:
        """A compact, printable summary of an index."""
        return {
            "database": index.database,
            "generated_at": index.generated_at.isoformat(),
            "cache_path": str(self.cache_path),
            "collections": [
                {
                    "name": c.name,
                    "document_count": c.document_count,
                    "avg_doc_size": c.avg_doc_size,
                    "attributes": len(c.attributes),
                    "indexes": len(c.indexes),
                }
                for c in index.collections
            ],
        }


def _to_attribute(acc: _AttributeAccumulator, indexed_paths: set[str]) -> AttributeInfo:
    cardinality: int | None
    cardinality = None if acc.cardinality_overflow else len(acc.cardinality)
    return AttributeInfo(
        path=acc.path,
        types=sorted(acc.types),
        sample_values=acc.samples,
        cardinality_estimate=cardinality,
        min=to_jsonable(acc.min),
        max=to_jsonable(acc.max),
        indexed=acc.path in indexed_paths or acc.path.split(".")[0] in indexed_paths,
    )


def _host_label(uri: str) -> str:
    from mongochat.types import sanitize_host

    return sanitize_host(uri)


def _safe_name(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ".-_" else "-" for ch in name) or "db"


async def build_index(db: Any, *, uri: str, database: str) -> SchemaIndex:
    """Build (or load from cache) the index for a database handle."""
    builder = SchemaIndexBuilder(db, uri=uri, database=database)
    return await builder.load_or_build()
