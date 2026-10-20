"""The only module that talks to the model.

Wraps ``openai.AsyncOpenAI`` with the error mapping, fallbacks, and safety rules
mongochat depends on: no key ever appears in an error, and unsupported tool calling
produces an actionable message instead of a stack trace.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any, TypeVar

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    OpenAI,
    PermissionDeniedError,
    RateLimitError,
)
from pydantic import BaseModel, ValidationError

from mongochat.types import (
    LLMAuthError,
    LLMConfig,
    LLMRateLimitError,
    LLMResponseError,
    redact_secret,
    truncate_text,
)

logger = logging.getLogger("mongochat.llm")

T = TypeVar("T", bound=BaseModel)

#: Characters of text we will try to parse as JSON in the fallback path.
MAX_JSON_CHARS = 20_000

TOOL_CAPABLE_HINT = (
    "The provider rejected the `tools` parameter, so {model} cannot do tool calling. "
    "Pick a tool-capable model (for Ollama: qwen2.5, llama3.1+ or mistral-nemo; "
    "for OpenAI: gpt-4o-mini or newer; for OpenRouter: any model marked "
    "'tools' in its context window) and set it with --model or CHAT_MODEL."
)


class LLMClient:
    """Thin, synchronous wrapper around the OpenAI-compatible chat endpoint."""

    def __init__(self, config: LLMConfig, *, timeout: float = 120.0, max_retries: int = 2) -> None:
        self.config = config
        api_key = config.api_key.get_secret_value() if config.api_key else "not-set"
        self._client = OpenAI(
            api_key=api_key,
            base_url=config.base_url,
            timeout=timeout,
            max_retries=max_retries,
        )

    # -- public -------------------------------------------------------------- #

    @property
    def model(self) -> str:
        return self.config.model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        """One chat completion. Returns the full assistant message, tool_calls included."""
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        try:
            response = await self._client.chat.completions.create(**payload)
        except Exception as exc:
            raise self._map_error(exc) from exc

        if not response.choices:
            raise LLMResponseError(
                f"The model {self.config.model} returned no choices. "
                "Check that the base URL points at an OpenAI-compatible /v1 endpoint."
            )
        message = response.choices[0].message
        return _message_to_dict(message)

    async def complete_structured(
        self,
        messages: list[dict[str, Any]],
        schema: type[T],
        *,
        temperature: float = 0.0,
    ) -> T:
        """Ask for a JSON object matching `schema`.

        Tries ``response_format=json_schema`` first, then falls back to JSON-only
        prompting with validation of the returned text.
        """
        request: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": False,
                    "schema": schema.model_json_schema(),
                },
            },
        }
        try:
            response = await self._client.chat.completions.create(**request)
            text = response.choices[0].message.content or ""
            if text.strip():
                return schema.model_validate_json(text)
            logger.debug("Empty structured response; falling back to prompt-based JSON")
        except (BadRequestError, APIStatusError, LLMResponseError) as exc:
            logger.debug("response_format rejected (%s); falling back to JSON prompting", exc)
        except (ValidationError, json.JSONDecodeError, ValueError) as exc:
            logger.debug("structured response invalid (%s); retrying with prompt", exc)

        # Fallback: ask for raw JSON only, then validate.
        fallback_messages = [
            *messages,
            {
                "role": "system",
                "content": (
                    f"Reply with a single JSON object only, no prose and no code fences, "
                    f"matching this JSON schema:\n{json.dumps(schema.model_json_schema())}"
                ),
            },
        ]
        reply = await self.chat(fallback_messages, tools=None, temperature=temperature)
        text = (reply.get("content") or "").strip()
        data = _extract_json(text)
        try:
            return schema.model_validate(data)
        except ValidationError as exc:
            raise LLMResponseError(
                f"The model {self.config.model} did not return valid JSON for "
                f"{schema.__name__}: {exc.errors()[0].get('msg', 'invalid')}."
            ) from exc

    # -- errors --------------------------------------------------------------- #

    def _map_error(self, exc: Exception) -> Exception:
        """Translate provider errors into mongochat errors. Never leaks the key."""
        if isinstance(exc, AuthenticationError | PermissionDeniedError):
            status = getattr(exc, "status_code", 0)
            return LLMAuthError(
                f"The LLM provider rejected the credentials (HTTP {status}). "
                "Check --api-key / OPENAI_API_KEY. The key itself is never shown."
            )
        if isinstance(exc, RateLimitError):
            return LLMRateLimitError(
                "The LLM provider rate-limited the request (HTTP 429). "
                "Wait a moment and retry, or switch provider with --base-url."
            )
        if isinstance(exc, BadRequestError):
            message = str(exc)
            if "tools" in message or "tool_choice" in message or "function_calling" in message:
                return LLMResponseError(TOOL_CAPABLE_HINT.format(model=self.config.model))
            if "api_key" in message.lower() or "api key" in message.lower():
                return LLMAuthError(
                    "The LLM provider rejected the API key. Check --api-key / OPENAI_API_KEY."
                )
            return LLMResponseError(
                f"The LLM provider rejected the request: {truncate_text(message, 400)}. "
                f"Model: {self.config.model}."
            )
        if isinstance(exc, APIConnectionError):
            return LLMResponseError(
                f"Could not reach the LLM provider at {self.config.base_url}: {exc}. "
                "Check OPENAI_BASE_URL and that the server is running."
            )
        if isinstance(exc, APITimeoutError):
            return LLMResponseError(
                f"The LLM provider at {self.config.base_url} timed out. Retry, or use a "
                "faster model with --model."
            )
        if isinstance(exc, APIStatusError):
            status = exc.status_code
            if status in {401, 403}:
                return LLMAuthError(
                    f"The LLM provider rejected the credentials (HTTP {status}). "
                    "Check --api-key / OPENAI_API_KEY."
                )
            if status == 429:
                return LLMRateLimitError(
                    "The LLM provider rate-limited the request (HTTP 429). Retry shortly."
                )
            return LLMResponseError(
                f"The LLM provider returned HTTP {status}: "
                f"{truncate_text(str(exc), 300)}. Model: {self.config.model}."
            )
        if isinstance(exc, LLMResponseError | LLMAuthError | LLMRateLimitError):
            return exc
        return LLMResponseError(
            f"LLM call failed with {type(exc).__name__}: {truncate_text(str(exc), 300)}. "
            f"Model: {self.config.model}."
        )

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._client.close()

    def redacted_summary(self) -> str:
        key = self.config.api_key.get_secret_value() if self.config.api_key else ""
        return (
            f"model={self.config.model} base_url={self.config.base_url} "
            f"api_key={redact_secret(key) if key else '(not set)'}"
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _message_to_dict(message: Any) -> dict[str, Any]:
    """Convert an OpenAI message object to a plain dict we can replay as history."""
    out: dict[str, Any] = {"role": getattr(message, "role", "assistant") or "assistant"}
    content = getattr(message, "content", None)
    if content is not None:
        out["content"] = content
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.id or f"call_{i}",
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments or "{}",
                },
            }
            for i, tc in enumerate(tool_calls)
        ]
    if "content" not in out:
        out["content"] = None
    return out


def _extract_json(text: str) -> Any:
    """Pull a JSON value out of a possibly chatty model reply."""
    text = truncate_text(text, MAX_JSON_CHARS)
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            pass
    start = stripped.find("[")
    end = stripped.rfind("]")
    if start != -1 and end > start:
        try:
            return json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise json.JSONDecodeError("no JSON object found", stripped, 0)
