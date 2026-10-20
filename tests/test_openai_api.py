"""Tests for the OpenAI-compatible /v1 endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from mongochat.config import Config
from mongochat.core.session import Session
from mongochat.server import create_app
from mongochat.types import AgentResult, AgentStep, ConnectionInfo, SchemaIndex


class StubLLM:
    """Answers without tools, so no real provider is contacted."""

    model = "stub-model"

    def __init__(self) -> None:
        self.config: Any = None

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        question = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        return {
            "role": "assistant",
            "content": f"Answer about orders: 2 rows. (orders) [question: {question[:20]}]",
        }

    async def close(self) -> None:  # pragma: no cover - parity with LLMClient
        pass


def make_session(config: Config) -> Session:
    index = SchemaIndex(
        database=config.mongodb_database or "shop",
        generated_at=datetime.now(UTC),
        collections=[],
    )
    session = Session(
        connection=ConnectionInfo(uri="mongodb://localhost:27017", database="shop"),
        client=None,  # type: ignore[arg-type]
        db=None,  # type: ignore[arg-type]
        index=index,
        llm=StubLLM(),  # type: ignore[arg-type]
        max_steps=config.max_steps,
        debug=False,
    )
    return session


@pytest.fixture
def config() -> Config:
    return Config(
        mongodb_uri="mongodb://localhost:27017",
        mongodb_database="shop",
        openai_api_key=SecretStr("sk-test-key"),
        chat_model="gpt-4o-mini",
        max_steps=4,
    )


@pytest.fixture
def client(config: Config, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def fake_create(cls: Any, **kwargs: Any) -> Session:
        return make_session(Config(**{**config.model_dump(exclude={"sources"}), "sources": {}}))

    monkeypatch.setattr(Session, "create", classmethod(fake_create))
    app = create_app(config)
    return TestClient(app)


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #


def test_chat_completions_standard_shape(client: TestClient) -> None:
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "how many orders?"}],
            "stream": False,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "chat.completion"
    assert payload["model"] == "gpt-4o-mini"
    assert payload["choices"][0]["message"]["role"] == "assistant"
    assert isinstance(payload["choices"][0]["message"]["content"], str)
    assert payload["choices"][0]["finish_reason"] == "stop"
    usage = payload["usage"]
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    # internal tool calls are not exposed
    assert "tool_calls" not in payload["choices"][0]["message"]


def test_chat_completions_with_tools_field_is_ignored(client: TestClient) -> None:
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "x", "parameters": {}}}],
        },
    )
    assert response.status_code == 200
    assert response.json()["object"] == "chat.completion"


def test_no_user_message_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "system", "content": "sys"}]},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "no_user_message"


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #


def test_stream_true_rejected(client: TestClient) -> None:
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "stream_unsupported"
    assert "stream=false" in body["error"]["message"]


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


def test_list_models(client: TestClient) -> None:
    response = client.get("/v1/models")
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "list"
    assert [m["id"] for m in payload["data"]] == ["mongochat-shop"]


# --------------------------------------------------------------------------- #
# Token guard
# --------------------------------------------------------------------------- #


@pytest.fixture
def secured_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    config = Config(
        mongodb_uri="mongodb://localhost:27017",
        mongodb_database="shop",
        openai_api_key=SecretStr("sk-test-key"),
        api_token=SecretStr("sekret-token"),
        chat_model="gpt-4o-mini",
        max_steps=4,
    )

    async def fake_create(cls: Any, **kwargs: Any) -> Session:
        return make_session(config)

    monkeypatch.setattr(Session, "create", classmethod(fake_create))
    return TestClient(create_app(config))


def test_token_required_when_configured(secured_client: TestClient) -> None:
    response = secured_client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 401


def test_wrong_token_rejected(secured_client: TestClient) -> None:
    response = secured_client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer nope"},
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 401


def test_correct_token_accepted(secured_client: TestClient) -> None:
    response = secured_client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sekret-token"},
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200


def test_token_guard_applies_to_api_endpoints(secured_client: TestClient) -> None:
    assert secured_client.get("/api/health").status_code == 200  # health stays open
    assert secured_client.get("/api/schema/whatever").status_code == 401
    assert secured_client.get("/v1/models").status_code == 401


def test_no_token_means_open_local_access(client: TestClient) -> None:
    assert client.get("/v1/models").status_code == 200


# --------------------------------------------------------------------------- #
# Web endpoints
# --------------------------------------------------------------------------- #


def test_health(client: TestClient) -> None:
    assert client.get("/api/health").json()["status"] == "ok"


def test_serves_dashboard(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "mongochat" in response.text.lower()


def test_unknown_session_shape(client: TestClient) -> None:
    response = client.post("/api/chat", json={"session_id": "nope", "message": "hi"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "no_session"


def test_message_length_limit(client: TestClient) -> None:
    response = client.post(
        "/api/chat",
        json={"session_id": "nope", "message": "x" * 2001},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "message_too_long"


def test_agent_result_serializes() -> None:
    result = AgentResult(
        answer="ok",
        steps=[AgentStep(index=0, tool_name="list_collections", row_count=1, elapsed_ms=5)],
        elapsed_ms=10,
    )
    payload = result.model_dump(mode="json")
    assert payload["steps"][0]["tool_name"] == "list_collections"
