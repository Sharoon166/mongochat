"""The real LLMClient, driven through a fake HTTP transport.

The agent/API tests inject fake LLM clients, so nothing else would notice if
``LLMClient`` stopped awaiting its own request correctly (it once awaited a
synchronous client's response object).
"""

from __future__ import annotations

import httpx
from openai import AsyncOpenAI
from pydantic import SecretStr

from mongochat.core.llm import LLMClient
from mongochat.types import LLMAuthError, LLMConfig

COMPLETION = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 0,
    "model": "test-model",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "hello",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "list_collections", "arguments": "{}"},
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


def make_client(handler: httpx.MockTransport) -> LLMClient:
    """A real LLMClient whose requests never leave the process."""
    config = LLMConfig(
        base_url="https://example.invalid/v1",
        model="test-model",
        api_key=SecretStr("sk-test-key"),
    )
    client = LLMClient(config)
    client._client = AsyncOpenAI(
        api_key="sk-test-key",
        base_url=config.base_url,
        http_client=httpx.AsyncClient(transport=handler),
    )
    return client


async def test_chat_awaits_the_response() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["authorization"] = request.headers.get("authorization", "")
        return httpx.Response(200, json=COMPLETION)

    client = make_client(httpx.MockTransport(handler))
    try:
        message = await client.chat([{"role": "user", "content": "hi"}])
    finally:
        await client.close()

    assert message["role"] == "assistant"
    assert message["content"] == "hello"
    assert message["tool_calls"][0]["function"]["name"] == "list_collections"
    assert seen["path"].endswith("/chat/completions")
    assert seen["authorization"] == "Bearer sk-test-key"


async def test_chat_maps_auth_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": {"message": "Invalid API key", "type": "invalid_request_error"}}
        )

    client = make_client(httpx.MockTransport(handler))
    try:
        try:
            await client.chat([{"role": "user", "content": "hi"}])
        except LLMAuthError as exc:
            assert "sk-test-key" not in str(exc)
        else:
            raise AssertionError("expected LLMAuthError")
    finally:
        await client.close()
