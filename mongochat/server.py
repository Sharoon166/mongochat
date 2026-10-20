"""FastAPI app: the web dashboard (/), the /api endpoints, and the /v1 API.

One process serves all three. Sessions are in-memory and die with the process;
only the schema index cache persists.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr

from mongochat.config import Config, redact_uri_safe, require_uri_db
from mongochat.core.agent import set_debug
from mongochat.core.relations import infer_relations
from mongochat.core.session import Session
from mongochat.types import (
    AgentResult,
    LLMConfig,
    MongoChatError,
)

logger = logging.getLogger("mongochat.server")

STATIC_DIR = Path(__file__).parent / "static"
MAX_MESSAGE_CHARS = 2000

# Session registry: session_id -> Session. In-memory by design.
_SESSIONS: dict[str, Session] = {}


def _error(code: str, message: str, status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def _message_for(exc: MongoChatError) -> tuple[str, str]:
    mapping: dict[str, str] = {
        "ConfigError": "config",
        "ConnectionError": "connection",
        "LLMAuthError": "llm_auth",
        "LLMRateLimitError": "llm_rate_limit",
        "LLMResponseError": "llm_response",
        "UnsafeQueryError": "unsafe_query",
        "QueryTimeoutError": "query_timeout",
        "ToolArgumentError": "tool_arguments",
        "AgentStepLimitError": "step_limit",
    }
    return mapping.get(type(exc).__name__, "error"), str(exc)


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #


class SessionRequest(BaseModel):
    # Empty means "use the server's configured value" (flag > env > .env).
    uri: str = ""
    database: str = ""
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None


class ChatRequest(BaseModel):
    session_id: str
    message: str = Field(min_length=1)
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None


class V1Message(BaseModel):
    role: str
    content: Any = None
    tool_calls: list[dict[str, Any]] | None = None
    name: str | None = None


class V1ChatRequest(BaseModel):
    model: str | None = None
    messages: list[V1Message]
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    temperature: float | None = None
    max_tokens: int | None = None


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


def require_token(config: Config) -> Any:
    """Dependency: Bearer token check, only enforced when a token is configured."""

    async def dependency(
        authorization: str | None = Header(default=None),
    ) -> None:
        if config.api_token is None:
            return
        expected = config.api_token.get_secret_value()
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=401,
                detail="Missing bearer token. Send Authorization: Bearer <MONGOCHAT_API_TOKEN>.",
            )
        if authorization.removeprefix("Bearer ").strip() != expected:
            raise HTTPException(status_code=401, detail="Invalid API token.")

    return dependency


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #


def create_app(config: Config) -> FastAPI:
    set_debug(config.debug)
    auth = require_token(config)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info("mongochat server starting (debug=%s)", config.debug)
        yield
        for session in list(_SESSIONS.values()):
            await session.close()
        _SESSIONS.clear()

    app = FastAPI(
        title="mongochat",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.config = config

    @app.exception_handler(MongoChatError)
    async def _mongo_error(_: Request, exc: MongoChatError) -> JSONResponse:
        code, message = _message_for(exc)
        status = 401 if code == "llm_auth" else 400
        if code == "connection":
            status = 502
        return _error(code, message, status)

    # ------------------------------------------------------------------ #
    # Web UI
    # ------------------------------------------------------------------ #

    @app.get("/", include_in_schema=False)
    async def index_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ------------------------------------------------------------------ #
    # /api
    # ------------------------------------------------------------------ #

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "sessions": len(_SESSIONS)}

    @app.get("/api/config")
    async def server_config() -> dict[str, Any]:
        """What the server already knows, so the UI can skip re-asking.

        Booleans, the database name, and a credential-stripped URI hint only —
        no secrets leave the process here.
        """
        uri_set = bool(config.mongodb_uri)
        return {
            "mongodb_uri_set": uri_set,
            "uri_hint": redact_uri_safe(config.mongodb_uri) if uri_set else "",
            "database": config.mongodb_database,
            "api_key_set": config.openai_api_key is not None,
            "base_url": config.openai_base_url,
            "model": config.chat_model,
            "token_required": config.api_token is not None,
            "ready": uri_set and bool(config.mongodb_database),
        }

    @app.post("/api/session", dependencies=[Depends(auth)])
    async def create_session(body: SessionRequest) -> dict[str, Any]:
        uri, database = _resolve(body.uri, body.database, config)
        llm = _llm_config(body.api_key, body.base_url, body.model, config)
        try:
            session = await Session.create(
                uri=uri,
                database=database,
                llm_config=llm,
                max_steps=config.max_steps,
                debug=config.debug,
            )
        except MongoChatError as exc:
            code, message = _message_for(exc)
            status = 502 if code == "connection" else 400
            return _error(code, message, status)  # type: ignore[return-value]
        session_id = uuid.uuid4().hex
        _SESSIONS[session_id] = session
        if session.warning:
            logger.warning("%s", session.warning)
        return {"session_id": session_id, "database": database, "collections": session.collections}

    @app.post("/api/chat", dependencies=[Depends(auth)], response_model=None)
    async def chat(body: ChatRequest) -> dict[str, Any] | JSONResponse:
        if len(body.message) > MAX_MESSAGE_CHARS:
            return _error(
                "message_too_long",
                f"Message exceeds {MAX_MESSAGE_CHARS} characters.",
            )
        session = _SESSIONS.get(body.session_id)
        if session is None:
            return _error("no_session", "Unknown session_id. Create a session first.", 404)
        if session.llm is None or _wants_key(body):
            session.llm = _open_llm(body, config)
        if session.llm is None:
            return _error("no_llm", "No API key configured for this request.", 400)
        try:
            result: AgentResult = await session.ask(body.message)
        except MongoChatError as exc:
            code, message = _message_for(exc)
            status = 401 if code == "llm_auth" else 400
            return _error(code, message, status)
        steps = _steps_with_queries(result)
        return {
            "answer": result.answer,
            "steps": steps,
            "truncated": result.truncated,
            "row_count": sum(s.row_count for s in result.steps),
            "elapsed_ms": result.elapsed_ms,
        }

    @app.get("/api/trace/{session_id}", dependencies=[Depends(auth)], response_model=None)
    async def trace(session_id: str) -> dict[str, Any] | JSONResponse:
        """Latest tool step, polled by the UI while a question is running."""
        session = _get(session_id)
        if isinstance(session, JSONResponse):
            return session
        step = session.last_step
        return {
            "steps_done": session.steps_done,
            "tool_name": step.tool_name if step else None,
            "elapsed_ms": step.elapsed_ms if step else 0,
            "error": step.error if step else False,
        }

    @app.get("/api/schema/{session_id}", dependencies=[Depends(auth)], response_model=None)
    async def get_schema(session_id: str) -> dict[str, Any] | JSONResponse:
        session = _get(session_id)
        if isinstance(session, JSONResponse):
            return session
        return session.index.model_dump(mode="json")

    @app.get("/api/graph/{session_id}", dependencies=[Depends(auth)], response_model=None)
    async def get_graph(session_id: str) -> dict[str, Any] | JSONResponse:
        """Schema index plus inferred relationships, for the explorer graph."""
        session = _get(session_id)
        if isinstance(session, JSONResponse):
            return session
        return {
            "index": session.index.model_dump(mode="json"),
            "relations": [r.model_dump() for r in infer_relations(session.index)],
        }

    @app.post("/api/schema/{session_id}/refresh", dependencies=[Depends(auth)], response_model=None)
    async def refresh_schema(session_id: str) -> dict[str, Any] | JSONResponse:
        session = _get(session_id)
        if isinstance(session, JSONResponse):
            return session
        try:
            index = await session.refresh_index()
        except MongoChatError as exc:
            code, message = _message_for(exc)
            return _error(code, message, 400)
        return {"database": index.database, "collections": index.collection_names}

    @app.delete("/api/schema/{session_id}", dependencies=[Depends(auth)], response_model=None)
    async def reset_schema(session_id: str) -> dict[str, Any] | JSONResponse:
        session = _get(session_id)
        if isinstance(session, JSONResponse):
            return session
        deleted = await session.reset_index()
        session.index = await session.builder.build()
        session.builder.save(session.index)
        return {"reset": deleted, "collections": session.collections}

    @app.delete("/api/session/{session_id}", dependencies=[Depends(auth)], response_model=None)
    async def delete_session(session_id: str) -> dict[str, Any] | JSONResponse:
        session = _SESSIONS.pop(session_id, None)
        if session is None:
            return _error("no_session", "Unknown session_id.", 404)
        await session.close()
        return {"closed": True}

    # ------------------------------------------------------------------ #
    # /v1 — OpenAI-compatible
    # ------------------------------------------------------------------ #

    @app.get("/v1/models", dependencies=[Depends(auth)])
    async def list_models() -> dict[str, Any]:
        name = f"mongochat-{config.mongodb_database or 'database'}"
        return {
            "object": "list",
            "data": [
                {
                    "id": name,
                    "object": "model",
                    "created": 0,
                    "owned_by": "mongochat",
                }
            ],
        }

    @app.post("/v1/chat/completions", dependencies=[Depends(auth)], response_model=None)
    async def chat_completions(body: V1ChatRequest) -> dict[str, Any] | JSONResponse:
        if body.stream:
            return _error(
                "stream_unsupported",
                "Streaming is not supported in v1. Send stream=false (or omit it) and "
                "read the single JSON response.",
                400,
            )
        if body.tools:
            logger.warning(
                "Ignoring request-supplied `tools`: mongochat runs its own internal "
                "tool loop and returns only the final answer."
            )
        question = _last_user_text(body.messages)
        if not question:
            return _error("no_user_message", "No user message found in `messages`.", 400)
        if len(question) > MAX_MESSAGE_CHARS:
            return _error("message_too_long", f"Message exceeds {MAX_MESSAGE_CHARS} characters.")

        session = _SESSIONS.get(f"v1:{config.mongodb_database}")
        if session is None:
            try:
                uri, database = require_uri_db(config)
            except MongoChatError as exc:
                code, message = _message_for(exc)
                return _error(code, message, 400)
            llm = LLMConfig(
                base_url=config.openai_base_url,
                model=body.model or config.chat_model,
                api_key=config.openai_api_key,
            )
            if llm.api_key is None:
                return _error(
                    "no_api_key",
                    "Set OPENAI_API_KEY (or pass --api-key) to use /v1/chat/completions.",
                    400,
                )
            try:
                session = await Session.create(
                    uri=uri,
                    database=database,
                    llm_config=llm,
                    max_steps=config.max_steps,
                    debug=config.debug,
                )
            except MongoChatError as exc:
                code, message = _message_for(exc)
                status = 502 if code == "connection" else 400
                return _error(code, message, status)
            _SESSIONS[f"v1:{database}"] = session

        current_config = getattr(session.llm, "config", None)
        if body.model and current_config is not None and session.llm is not None:
            session.llm = session.llm.__class__(
                LLMConfig(
                    base_url=current_config.base_url,
                    model=body.model,
                    api_key=current_config.api_key,
                )
            )
        try:
            result = await session.ask(question)
        except MongoChatError as exc:
            code, message = _message_for(exc)
            status = 401 if code == "llm_auth" else 400
            return _error(code, message, status)

        return _completion(
            model=body.model or config.chat_model,
            content=result.answer,
            prompt_chars=len(question),
            completion_chars=len(result.answer),
        )

    return app


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _get(session_id: str) -> Session | JSONResponse:
    session = _SESSIONS.get(session_id)
    if session is None:
        return _error("no_session", "Unknown session_id.", 404)
    return session


def _steps_with_queries(result: AgentResult) -> list[dict[str, Any]]:
    """Attach each executed query to the run_query step that produced it."""
    pending = list(result.executed_queries)
    steps: list[dict[str, Any]] = []
    for step in result.steps:
        payload = step.model_dump(mode="json")
        if step.tool_name == "run_query" and pending:
            payload["executed_query"] = pending.pop(0)
        steps.append(payload)
    return steps


def _resolve(uri: str | None, database: str | None, config: Config) -> tuple[str, str]:
    try:
        return uri or require_uri_db(config)[0], database or require_uri_db(config)[1]
    except MongoChatError as exc:
        _code, message = _message_for(exc)
        raise HTTPException(status_code=400, detail=message) from exc


def _llm_config(
    api_key: str | None, base_url: str | None, model: str | None, config: Config
) -> LLMConfig | None:
    """A request-supplied key wins for that request only; it is never stored."""
    key = api_key or (config.openai_api_key.get_secret_value() if config.openai_api_key else None)
    if not key:
        return None
    return LLMConfig(
        base_url=(base_url or config.openai_base_url).rstrip("/"),
        model=model or config.chat_model,
        api_key=SecretStr(key),
    )


def _wants_key(body: ChatRequest) -> bool:
    return bool(body.api_key or body.base_url or body.model)


def _open_llm(body: ChatRequest, config: Config) -> Any:
    cfg = _llm_config(body.api_key, body.base_url, body.model, config)
    if cfg is None:
        return None
    from mongochat.core.llm import LLMClient

    return LLMClient(cfg)


def _last_user_text(messages: list[V1Message]) -> str:
    for message in reversed(messages):
        if message.role == "user":
            content = message.content
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                texts = [
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                ]
                return "".join(texts)
            return ""
    return ""


def _completion(
    *, model: str, content: str, prompt_chars: int, completion_chars: int
) -> dict[str, Any]:
    prompt_tokens = max(1, prompt_chars // 4)
    completion_tokens = max(1, completion_chars // 4)
    return {
        "id": "chatcmpl-mongochat",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def run_server(
    config: Config, *, host: str = "127.0.0.1", port: int = 8000, open_browser: bool = True
) -> None:
    """Bind the app, print where it is, and open a browser."""
    import webbrowser

    import uvicorn

    if host not in {"127.0.0.1", "localhost", "::1"} and config.api_token is None:
        logger.warning(
            "Serving on %s without MONGOCHAT_API_TOKEN: anyone who can reach this port "
            "can query your database and spend your LLM key. Set MONGOCHAT_API_TOKEN.",
            host,
        )
        print(
            f"WARNING: --host {host} with no MONGOCHAT_API_TOKEN set. The API is unauthenticated.",
            flush=True,
        )
    app = create_app(config)
    url = f"http://{host}:{port}"
    _print_banner(config, url)
    if open_browser:
        print("Opening your browser…", flush=True)
        try:
            webbrowser.open(url)
        except Exception:  # pragma: no cover - depends on the desktop session
            print(f"Could not open a browser automatically. Visit {url} yourself.", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="debug" if config.debug else "warning")


def _print_banner(config: Config, url: str) -> None:
    """Say out loud what was loaded (and from where), so a silent console never happens."""
    values = config.redacted()
    sources = config.source_table()
    rows = [
        ("MongoDB URI", "mongodb_uri"),
        ("Database", "mongodb_database"),
        ("API key", "openai_api_key"),
        ("Base URL", "openai_base_url"),
        ("Model", "chat_model"),
    ]
    width = max(len(label) for label, _ in rows)
    lines = ["mongochat web dashboard", f"  {'URL'.ljust(width)}  {url}"]
    lines += [
        f"  {label.ljust(width)}  {values[key]}  [{sources.get(key, 'default')}]"
        for label, key in rows
    ]
    lines.append(f"  {'Docs'.ljust(width)}  {url}/docs")
    if not config.mongodb_uri or not config.mongodb_database:
        lines.append("")
        lines.append("Missing values will be asked for in the connect dialog.")
    # flush=True: stdout is block-buffered when redirected to a file or pipe.
    print("\n".join(lines), flush=True)


__all__ = ["create_app", "run_server"]


def __getattr__(name: str) -> Any:
    """Lazy `app` attribute so `uvicorn mongochat.server:app` works when configured."""
    if name == "app":
        from mongochat.config import load_config

        return create_app(load_config())
    raise AttributeError(name)
