"""One session = one connection + one schema index + one conversation history.

Sessions live in memory only; the only thing that persists across restarts is the
schema index cache on disk.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from mongochat.core.agent import Agent, prune_history, set_debug
from mongochat.core.llm import LLMClient
from mongochat.core.schema_index import SchemaIndexBuilder
from mongochat.core.tools import ToolRegistry
from mongochat.types import (
    AgentResult,
    AgentStep,
    ConnectionError,
    ConnectionInfo,
    LLMConfig,
    SchemaIndex,
    redact_uri,
)

logger = logging.getLogger("mongochat.session")

MotorClient = AsyncIOMotorClient[Any]
MotorDatabase = AsyncIOMotorDatabase[Any]

READ_ONLY_ROLES = {"read", "readAnyDatabase"}
WRITE_ROLES = {"readWrite", "readWriteAnyDatabase", "dbAdmin", "root", "clusterAdmin", "restore"}


async def connect(
    uri: str, database: str, *, server_selection_ms: int = 5000
) -> tuple[MotorClient, MotorDatabase]:
    """Open a connection and verify it with `connectionStatus`."""
    if not uri or not uri.strip():
        raise ConnectionError("No MongoDB URI given. Pass --uri or set MONGODB_URI.")
    if not database or not database.strip():
        raise ConnectionError("No database name given. Pass --db or set MONGODB_DATABASE.")

    try:
        client = MotorClient(
            uri,
            serverSelectionTimeoutMS=server_selection_ms,
            connectTimeoutMS=server_selection_ms,
        )
        await client.admin.command("ping")
    except ConnectionError:
        raise
    except Exception as exc:
        raise ConnectionError(
            f"Could not connect to {redact_uri(uri)}: {type(exc).__name__}: {exc}. "
            "Check the URI, that mongod is running, and your credentials."
        ) from exc

    db = client[database]
    return client, db


async def _read_roles(client: MotorClient, database: str) -> set[str]:
    status = await client.admin.command("connectionStatus")
    auth = status.get("authInfo", {})
    roles: set[str] = set()
    for entry in auth.get("authenticatedUserRoles", []):
        roles.add(str(entry.get("role", "")))
    return roles


def warn_if_writable(roles: set[str], *, database: str) -> str | None:
    """Return a warning when the user has write roles; never blocks."""
    write_roles = sorted(roles & WRITE_ROLES)
    if not write_roles:
        return None
    return (
        f"The MongoDB user has write roles ({', '.join(write_roles)}). "
        f"mongochat is read-only, but a dedicated read-only user for {database!r} is "
        "recommended, e.g.:\n"
        "  use admin; db.createUser({user: 'mongochat', pwd: '...', "
        f"roles: [{{role: 'read', db: '{database}'}}]}})"
    )


class Session:
    """Binds a connection, an index, a tool registry, and an agent."""

    def __init__(
        self,
        *,
        connection: ConnectionInfo,
        client: MotorClient,
        db: MotorDatabase,
        index: SchemaIndex,
        llm: LLMClient | None,
        max_steps: int = 8,
        debug: bool = False,
        writable_warning: str | None = None,
    ) -> None:
        self.connection = connection
        self.client = client
        self.db = db
        self.index = index
        self.llm = llm
        self.max_steps = max_steps
        self.debug = debug
        self.warning = writable_warning
        self.history: list[dict[str, Any]] = []
        self.registry = ToolRegistry(db=db, get_index=lambda: self.index)
        set_debug(debug)
        self._agent: Agent | None = None
        self.last_step: AgentStep | None = None
        self.steps_done: int = 0

    # -- factory -------------------------------------------------------------- #

    @classmethod
    async def create(
        cls,
        *,
        uri: str,
        database: str,
        llm_config: LLMConfig | None,
        max_steps: int = 8,
        debug: bool = False,
        rebuild_index: bool = False,
    ) -> Session:
        connection = ConnectionInfo(uri=uri, database=database)
        client, db = await connect(uri, database)
        roles = await _read_roles(client, database)
        warning = warn_if_writable(roles, database=database)
        if warning:
            logger.warning("%s", warning)

        builder = SchemaIndexBuilder(db, uri=uri, database=database)
        index = await builder.refresh() if rebuild_index else await builder.load_or_build()

        llm = LLMClient(llm_config) if llm_config is not None else None
        return cls(
            connection=connection,
            client=client,
            db=db,
            index=index,
            llm=llm,
            max_steps=max_steps,
            debug=debug,
            writable_warning=warning,
        )

    # -- behaviour ------------------------------------------------------------- #

    @property
    def builder(self) -> SchemaIndexBuilder:
        return SchemaIndexBuilder(
            self.db, uri=self.connection.uri, database=self.connection.database
        )

    @property
    def collections(self) -> list[str]:
        return self.index.collection_names

    def _get_agent(self, *, on_step: Any | None = None) -> Agent:
        if self.llm is None:
            raise ConnectionError(
                "No LLM configured. Set OPENAI_API_KEY (or pass --api-key) to ask questions."
            )
        return Agent(
            llm=self.llm,
            registry=self.registry,
            index=self.index,
            max_steps=self.max_steps,
            on_step=on_step,
        )

    async def ask(self, question: str, *, on_step: Any | None = None) -> AgentResult:
        """Answer one question, keeping the final answer in history."""

        def trace(step: Any) -> None:
            self.last_step = step
            self.steps_done += 1
            if on_step is not None:
                on_step(step)

        self.last_step = None
        self.steps_done = 0
        agent = self._get_agent(on_step=trace)
        result = await agent.run(question, history=self.history)
        self.history.append({"role": "user", "content": question})
        self.history.append({"role": "assistant", "content": result.answer})
        self.history = prune_history(self.history)
        return result

    async def refresh_index(self) -> SchemaIndex:
        self.index = await self.builder.refresh()
        return self.index

    async def reset_index(self) -> bool:
        deleted = self.builder.reset()
        return deleted

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            self.client.close()
        if self.llm is not None:
            self.llm.close()
