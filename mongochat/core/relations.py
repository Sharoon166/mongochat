"""Infer relationships between collections from the schema index.

MongoDB enforces no foreign keys, so relations are *guessed* from field names:
``orders.customerId`` is read as pointing at ``customers._id``. Every relation
produced here is a hint for the explorer UI and the model, never a guarantee.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

from mongochat.types import AttributeInfo, SchemaIndex

#: Field suffixes that usually mark a reference, tried longest-first.
REFERENCE_SUFFIXES = ("refid", "ref", "id")

#: Types that are definitely NOT references (containers and booleans).
NON_REFERENCE_TYPES = frozenset({"object", "array", "bool", "null"})
#: Stricter gate for bare name matches (``orders.customer -> customers``).
OBJECT_TYPES = frozenset({"objectid", "string"})

_SPLIT_RE = re.compile(r"[^a-zA-Z0-9]+")
_CAMEL_RE = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")


class Relation(BaseModel):
    """One inferred link from a field to another collection's ``_id``."""

    model_config = ConfigDict(frozen=True)

    source_collection: str
    source_field: str
    target_collection: str
    target_field: str = "_id"
    confidence: str  # "exact" | "singular_plural" | "bare"
    self_reference: bool = False


def normalize(name: str) -> str:
    """camelCase / snake_case / kebab-case -> one lowercase key."""
    parts: list[str] = []
    for chunk in _SPLIT_RE.split(name):
        parts.extend(match.group(0) for match in _CAMEL_RE.finditer(chunk))
    return "".join(part.lower() for part in parts)


def singular(word: str) -> str:
    """Naive singular of a lowercase key: orders -> order, categories -> category."""
    if word.endswith("eries"):  # series, batteries → unchanged
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("sses"):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _variants(key: str) -> set[str]:
    return {key, singular(key)}


def _reference_stem(path: str) -> tuple[str, bool] | None:
    """(stem, had_suffix) for a field that might be a reference, else None."""
    leaf = path.split(".")[-1]
    key = normalize(leaf)
    if key in {"id", "pk"}:
        return None
    for suffix in REFERENCE_SUFFIXES:
        if key.endswith(suffix) and len(key) > len(suffix):
            return key[: -len(suffix)], True
    return key, False


def _is_reference_type(attr: AttributeInfo, *, bare: bool) -> bool:
    types = {t.lower() for t in attr.types}
    if bare:
        # For bare name matches (no suffix), require ObjectId or string
        return bool(types & OBJECT_TYPES)
    # For suffixed fields (customerId, supplier_ref, etc.), accept anything
    # that isn't a container or boolean — legacy DBs may use int/string keys too
    return bool(types - NON_REFERENCE_TYPES)


def infer_relations(index: SchemaIndex) -> list[Relation]:
    """Guess which fields point at which collections. Deterministic and cheap."""
    by_key: dict[str, list[str]] = {}
    for coll in index.collections:
        by_key.setdefault(normalize(coll.name), []).append(coll.name)

    def lookup(stem: str) -> str | None:
        hits = [
            name
            for key, names in by_key.items()
            if stem in _variants(key) or key in _variants(stem)
            for name in names
        ]
        # Ambiguous stems (both "user" and "users" exist) are skipped.
        return hits[0] if len(hits) == 1 else None

    found: dict[tuple[str, str, str], Relation] = {}
    for coll in index.collections:
        for attr in coll.attributes:
            parsed = _reference_stem(attr.path)
            if parsed is None:
                continue
            stem, had_suffix = parsed
            if not stem:
                continue
            if not _is_reference_type(attr, bare=not had_suffix):
                continue
            target = lookup(stem)
            if target is None:
                continue
            if not had_suffix:
                confidence = "bare"
            elif normalize(target) == stem:
                confidence = "exact"
            else:
                confidence = "singular_plural"
            key = (coll.name, attr.path, target)
            if key in found:
                continue
            found[key] = Relation(
                source_collection=coll.name,
                source_field=attr.path,
                target_collection=target,
                confidence=confidence,
                self_reference=target == coll.name,
            )

    return sorted(found.values(), key=lambda r: (r.source_collection, r.source_field))
