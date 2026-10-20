"""MCP server exposing the same tool registry on stdio.

The calling client's model answers the questions; mongochat only serves tools, so
no LLM key is needed in MCP mode.
"""

from __future__ import annotations

import inspect
import json
import logging
from contextlib import suppress
from typing import Any

from mcp.server.fastmcp import FastMCP

from mongochat.config import Config, require_uri_db
from mongochat.core.session import Session
from mongochat.core.tools import ToolRegistry
from mongochat.types import MongoChatError

logger = logging.getLogger("mongochat.mcp")


def build_server(registry: ToolRegistry, *, database: str) -> FastMCP:
    """Register every Tool in the registry as an MCP tool with the same schema.

    FastMCP derives the input schema from the function signature, so each wrapper
    gets a signature built from the tool's own Pydantic params model. The wrapper
    itself stays a simple ``**kwargs`` passthrough to ``registry.call``, which is
    where validation and error shaping happen.
    """
    server = FastMCP(
        name="mongochat",
        instructions=(
            f"Read-only tools for the MongoDB database {database!r}. "
            "Call list_collections first, then describe_collection or run_query. "
            "Errors come back as tool errors; fix the arguments and retry."
        ),
    )

    for tool in registry.tools:
        server.tool(
            name=tool.name,
            description=tool.description,
        )(_make_handler(registry, tool.name, tool.params, tool.description))

    return server


def _make_handler(
    registry: ToolRegistry, tool_name: str, params: type[Any], description: str
) -> Any:
    async def handler(**kwargs: Any) -> str:
        result = await registry.call(tool_name, kwargs, tool_call_id="mcp_0")
        if result.is_error:
            # MCP tool error shape: reported to the client, server keeps running.
            raise ToolFailure(result.content)
        return result.content

    fields = getattr(params, "model_fields", {})
    signature_params = [
        inspect.Parameter(
            name,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=... if field.is_required() else field.default,
            annotation=field.annotation,
        )
        for name, field in fields.items()
    ]
    handler.__name__ = tool_name
    handler.__qualname__ = tool_name
    handler.__doc__ = description
    handler.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        signature_params, return_annotation=str
    )
    handler.__annotations__ = {
        **{name: field.annotation for name, field in fields.items()},
        "return": str,
    }
    return handler


class ToolFailure(Exception):
    """Raised inside an MCP tool so the client sees a proper tool error."""


async def serve(config: Config) -> None:
    """Connect, build the registry, and serve over stdio."""
    uri, database = require_uri_db(config)
    try:
        session = await Session.create(
            uri=uri,
            database=database,
            llm_config=None,  # MCP mode needs no LLM key
            max_steps=config.max_steps,
            debug=config.debug,
        )
    except MongoChatError as exc:
        raise SystemExit(f"mongochat mcp: {exc}") from exc

    if session.warning:
        print(session.warning, flush=True)

    server = build_server(session.registry, database=database)
    # FastMCP runs the stdio event loop itself; close the session on shutdown.
    try:
        await server.run_stdio_async()
    finally:
        await session.close()


def run_mcp(config: Config) -> None:
    """CLI entry point: `mongochat mcp`."""
    import anyio

    with suppress(KeyboardInterrupt):  # pragma: no cover
        anyio.run(serve, config)


def registry_summary(registry: ToolRegistry) -> str:
    """Small helper used by tests and `mongochat tools --json`."""
    return json.dumps(registry.mcp_tools(), indent=2, default=str)
