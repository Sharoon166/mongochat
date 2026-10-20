"""Shared Pydantic models, type aliases, and exceptions for mongochat.

Everything defined here is the single source of truth: other modules import these
shapes instead of redefining them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, SecretStr, field_serializer

# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


class MongoChatError(Exception):
    """Base class for every mongochat error."""


class ConfigError(MongoChatError):
    """Invalid or missing configuration."""


class ConnectionError(MongoChatError):
    """Could not reach or authenticate to MongoDB."""


class LLMAuthError(MongoChatError):
    """The LLM provider rejected our credentials (401/403)."""


class LLMRateLimitError(MongoChatError):
    """The LLM provider rate-limited us (429)."""


class LLMResponseError(MongoChatError):
    """The LLM returned something we cannot use."""


class UnsafeQueryError(MongoChatError):
    """A query failed validation and must not be executed."""


class QueryTimeoutError(MongoChatError):
    """A query exceeded the server-side time limit."""


class ToolArgumentError(MongoChatError):
    """Tool arguments failed schema validation."""


class AgentStepLimitError(MongoChatError):
    """The agent used up its step budget before producing an answer."""


# --------------------------------------------------------------------------- #
# Configuration models
# --------------------------------------------------------------------------- #


class ConnectionInfo(BaseModel):
    """A MongoDB connection, redacted whenever it is dumped or logged."""

    model_config = ConfigDict(frozen=True)

    uri: str
    database: str

    def redacted(self) -> str:
        """Return the URI with credentials replaced by ***."""
        return redact_uri(self.uri)

    @field_serializer("uri")
    def _serialize_uri(self, value: str) -> str:
        return redact_uri(value)

    @property
    def host_label(self) -> str:
        """A filesystem/cache-safe label derived from the URI host."""
        return sanitize_host(self.uri)


class LLMConfig(BaseModel):
    """How to reach the chat model provider. The key is never serialized."""

    model_config = ConfigDict(frozen=True)

    base_url: str
    model: str
    api_key: SecretStr | None = None

    @field_serializer("api_key")
    def _serialize_key(self, value: SecretStr | None) -> str | None:
        return None if value is None else redact_secret(value.get_secret_value())


# --------------------------------------------------------------------------- #
# Schema index models
# --------------------------------------------------------------------------- #

type BSONTypeName = str


class AttributeInfo(BaseModel):
    """One discovered attribute path within a collection."""

    model_config = ConfigDict(frozen=True)

    path: str
    types: list[BSONTypeName]
    sample_values: list[Any] = []
    cardinality_estimate: int | None = None
    min: Any | None = None
    max: Any | None = None
    indexed: bool = False


class CollectionIndex(BaseModel):
    """Everything we know about a single collection."""

    model_config = ConfigDict(frozen=True)

    name: str
    document_count: int
    avg_doc_size: int = 0
    attributes: list[AttributeInfo] = []
    indexes: list[str] = []
    sample_documents: list[dict[str, Any]] = []

    def find_attribute(self, path: str) -> AttributeInfo | None:
        for attr in self.attributes:
            if attr.path == path:
                return attr
        return None


class SchemaIndex(BaseModel):
    """The whole-database index handed to the model as context."""

    model_config = ConfigDict(frozen=True)

    database: str
    generated_at: datetime
    collections: list[CollectionIndex] = []

    def find_collection(self, name: str) -> CollectionIndex | None:
        for coll in self.collections:
            if coll.name == name:
                return coll
        return None

    @property
    def collection_names(self) -> list[str]:
        return [c.name for c in self.collections]

    def to_compact_prompt(self) -> str:
        """Render the index as a compact, token-cheap block for the system prompt."""
        lines: list[str] = []
        for coll in self.collections:
            lines.append(
                f"collection {coll.name} ({coll.document_count} docs, "
                f"{len(coll.attributes)} attributes)"
            )
            for attr in coll.attributes[:40]:
                types = "|".join(attr.types[:4])
                card = (
                    f"~{attr.cardinality_estimate} distinct"
                    if attr.cardinality_estimate is not None
                    else "unknown cardinality"
                )
                indexed = " indexed" if attr.indexed else ""
                lines.append(f"  {attr.path}: {types} ({card}{indexed})")
            if len(coll.attributes) > 40:
                lines.append(f"  ... {len(coll.attributes) - 40} more attributes")
        if not lines:
            lines.append("(no collections found)")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Tool models
# --------------------------------------------------------------------------- #

type ParamsFactory = type[BaseModel]
type Handler = Callable[..., Awaitable[Any]]


class Tool(BaseModel):
    """One tool, defined once and exported to OpenAI and MCP formats."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    name: str
    description: str
    params: ParamsFactory
    handler: Handler

    def openai_schema(self) -> dict[str, Any]:
        """OpenAI function-calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.params.model_json_schema(),
            },
        }

    def mcp_schema(self) -> dict[str, Any]:
        """MCP (JSON-schema) format used by FastMCP's `add_tool`."""
        schema = self.params.model_json_schema()
        schema.pop("title", None)
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": schema,
        }


class ToolCall(BaseModel):
    """A tool call requested by the model."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    arguments: dict[str, Any] = {}


class ToolResult(BaseModel):
    """The outcome of a tool dispatch. Errors are data, not exceptions."""

    model_config = ConfigDict(frozen=True)

    tool_call_id: str
    content: str
    is_error: bool = False


# --------------------------------------------------------------------------- #
# Agent / execution models
# --------------------------------------------------------------------------- #


class AgentStep(BaseModel):
    """One tool call made during a turn, for display and logging."""

    model_config = ConfigDict(frozen=True)

    index: int
    tool_name: str
    arguments: dict[str, Any] = {}
    result_summary: str = ""
    row_count: int = 0
    elapsed_ms: int = 0
    error: bool = False


class ExecutionResult(BaseModel):
    """The result of a read-only query execution."""

    model_config = ConfigDict(frozen=True)

    rows: list[dict[str, Any]] = []
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: int = 0
    executed_query: dict[str, Any] = {}


class AgentResult(BaseModel):
    """The final outcome of one agent turn."""

    model_config = ConfigDict(frozen=True)

    answer: str
    steps: list[AgentStep] = []
    truncated: bool = False
    elapsed_ms: int = 0
    executed_queries: list[dict[str, Any]] = []


# --------------------------------------------------------------------------- #
# Helpers shared across modules
# --------------------------------------------------------------------------- #

_BSON_NAMES: dict[type, str] = {
    dict: "object",
    list: "array",
    str: "string",
    bool: "bool",
    int: "int",
    float: "double",
    type(None): "null",
}


def bson_type_name(value: Any) -> BSONTypeName:
    """Best-effort BSON type name for a Python value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    cls = type(value)
    if cls in _BSON_NAMES:
        return _BSON_NAMES[cls]
    name = cls.__name__
    if name in {"ObjectId", "Decimal128", "Binary", "Bytes", "datetime"}:
        return {
            "ObjectId": "objectId",
            "Decimal128": "decimal",
            "Binary": "binData",
            "Bytes": "binData",
            "datetime": "date",
        }[name]
    return "string" if isinstance(value, str) else name.lower()


def to_jsonable(value: Any, *, max_depth: int = 8) -> Any:
    """Convert BSON-ish values (ObjectId, datetime, Decimal128, bytes) to JSON."""
    if max_depth <= 0:
        return "..."
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): to_jsonable(v, max_depth=max_depth - 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v, max_depth=max_depth - 1) for v in value]
    if isinstance(value, bytes | bytearray | memoryview):
        data = bytes(value)
        return f"0x{data[:32].hex()}{'...' if len(data) > 32 else ''}"
    # datetime-like
    toisoformat = getattr(value, "to_datetime", None)
    if callable(toisoformat):  # Decimal128
        try:
            return to_jsonable(toisoformat(), max_depth=max_depth - 1)
        except Exception:
            return str(value)
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        try:
            return iso()
        except Exception:
            return str(value)
    to_datetime = getattr(value, "__str__", None)
    if callable(to_datetime):
        text = str(value)
        if len(text) > 200:
            return text[:200] + "..."
        # Decimal128 and friends stringify with a wrapper; strip the obvious ones.
        if text.startswith("Decimal128('") or text.startswith('Decimal128("'):
            return text.split("(", 1)[1].rstrip(")'\"")
        if text.startswith("Binary(") or text.startswith("BytesIO"):
            return text
        return text
    return str(value)


def redact_secret(value: str, *, keep: int = 4) -> str:
    """Redact a secret for display: sk-...abcd."""
    if not value:
        return "(empty)"
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:3]}...{value[-keep:]}"


def redact_uri(uri: str) -> str:
    """Strip credentials from a MongoDB URI."""
    if "@" not in uri or "://" not in uri:
        return uri
    scheme, rest = uri.split("://", 1)
    if "@" in rest:
        hostpart = rest.rsplit("@", 1)[1]
        return f"{scheme}://***:***@{hostpart}"
    return uri


def sanitize_host(uri: str) -> str:
    """A filesystem-safe host label for the index cache filename."""
    try:
        after = uri.split("://", 1)[-1]
        host = after.rsplit("@", 1)[-1].split("/", 1)[0]
    except Exception:
        host = "unknown"
    cleaned = "".join(ch if ch.isalnum() or ch in ".-_" else "-" for ch in host)
    return cleaned or "unknown"


def truncate_text(text: str, limit: int) -> str:
    """Truncate to `limit` characters, marking the cut clearly."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} chars omitted]"


__all__ = [
    "AgentResult",
    "AgentStep",
    "AttributeInfo",
    "BSONTypeName",
    "CollectionIndex",
    "ConfigError",
    "ConnectionError",
    "ConnectionInfo",
    "ExecutionResult",
    "LLMAuthError",
    "LLMConfig",
    "LLMRateLimitError",
    "LLMResponseError",
    "MongoChatError",
    "QueryTimeoutError",
    "SchemaIndex",
    "Tool",
    "ToolArgumentError",
    "ToolCall",
    "ToolResult",
    "UnsafeQueryError",
    "bson_type_name",
    "redact_secret",
    "redact_uri",
    "sanitize_host",
    "to_jsonable",
    "truncate_text",
]
