"""The tool-calling agent loop.

Read the loop itself first: everything else here is prompt shaping, bookkeeping,
and the rules that keep answers honest (cite collections, state row counts, never
invent values).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from mongochat.core.llm import LLMClient
from mongochat.core.tools import ToolRegistry
from mongochat.types import (
    AgentResult,
    AgentStep,
    LLMResponseError,
    MongoChatError,
    SchemaIndex,
    ToolCall,
    redact_secret,
    truncate_text,
)

logger = logging.getLogger("mongochat.agent")

#: Cap on characters of tool output sent back to the model (~6k tokens).
MAX_TOOL_RESULT_CHARS = 24_000
#: Conversation history kept per session, as user/assistant turn pairs.
HISTORY_TURNS = 10

SYSTEM_PROMPT = """\
You are mongochat, a read-only analyst for a MongoDB database.

Rules you must follow:
- Answer ONLY from the results of the tools you call. Never invent values, counts, dates,
  or field names. If you did not see it in a tool result, do not state it.
- Call tools to learn the schema before querying: list_collections, then describe_collection
  or sample_documents, then run_query.
- Cite the collections you used by name in the answer, in parentheses, e.g. (orders, users).
- State the row count for any query you run, e.g. "returned 12 rows".
- If a query returns no rows, say plainly that the result is empty. Do not rephrase an empty
  result as a trend or a conclusion.
- Only read-only queries are possible. Do not attempt writes, admin commands, or JavaScript.
- If the question cannot be answered from this database (the data is missing, the collection
  does not exist, or the topic is unrelated to the data), say so clearly and do NOT call
  run_query to guess.
- If a tool returns an error, read the reason, correct the arguments, and try again.
- For relative dates like "last month", call get_time first.
- Keep the final answer concise: a direct answer, the evidence, and any caveats.
"""


def build_system_prompt(index: SchemaIndex) -> str:
    """The system prompt plus the compact schema index."""
    return SYSTEM_PROMPT + "\nDatabase schema index:\n" + index.to_compact_prompt()


def prune_history(
    messages: list[dict[str, Any]], *, turns: int = HISTORY_TURNS
) -> list[dict[str, Any]]:
    """Keep the last `turns` user/assistant pairs; drop earlier tool chatter.

    The leading system message is always kept. Messages older than the window are
    dropped entirely, which removes their tool messages too.
    """
    if not messages:
        return messages
    system = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]

    last_user = None
    for i, m in enumerate(rest):
        if m.get("role") == "user":
            last_user = i
    if last_user is None:
        return system + rest

    keep_from = last_user
    seen = 0
    for i in range(last_user, -1, -1):
        if rest[i].get("role") == "user":
            seen += 1
            if seen > turns:
                keep_from = i + 1
                break
        else:
            keep_from = i
    window = rest[keep_from:]
    # Drop tool chatter from turns that are still inside the window but are not
    # part of the live exchange (i.e. tool results older than the newest user turn).
    cleaned: list[dict[str, Any]] = []
    for m in window:
        if m.get("role") == "tool":
            continue
        cleaned.append(m)
    return system + cleaned


class Agent:
    """Runs one turn: model -> tools -> model -> ... -> final answer."""

    def __init__(
        self,
        *,
        llm: LLMClient,
        registry: ToolRegistry,
        index: SchemaIndex,
        max_steps: int = 8,
        on_step: Any | None = None,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.index = index
        self.max_steps = max(1, max_steps)
        self.on_step = on_step  # optional callback(step: AgentStep) for live traces

    async def run(
        self,
        question: str,
        history: list[dict[str, Any]] | None = None,
    ) -> AgentResult:
        """Answer one question, running tools as needed."""
        started = time.perf_counter()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": build_system_prompt(self.index)},
            *(history or []),
            {"role": "user", "content": question},
        ]

        steps: list[AgentStep] = []
        executed: list[dict[str, Any]] = []
        truncated = False

        for step_index in range(self.max_steps):
            reply = await self.llm.chat(messages, tools=self.registry.openai_tools())
            messages.append(reply)

            raw_calls = reply.get("tool_calls") or []
            if not raw_calls:
                answer = reply.get("content") or ""
                if not answer.strip():
                    raise LLMResponseError(
                        f"The model {self.llm.model} returned neither an answer nor a tool "
                        "call. Try a tool-capable model such as gpt-4o-mini."
                    )
                return AgentResult(
                    answer=answer,
                    steps=steps,
                    truncated=truncated,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    executed_queries=executed,
                )

            for position, raw in enumerate(raw_calls):
                call = _to_tool_call(raw, fallback_id=f"call_{step_index}_{position}")
                if logger.isEnabledFor(logging.DEBUG) or _debug:
                    logger.debug(
                        "step %d → %s(%s)",
                        step_index,
                        call.name,
                        _safe_args(call.arguments),
                    )
                call_started = time.perf_counter()
                result = await self.registry.dispatch(call)
                elapsed_ms = int((time.perf_counter() - call_started) * 1000)

                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": result.content}
                )
                row_count, was_truncated, summary, executed_query = _summarize(result.content)
                truncated = truncated or was_truncated
                if executed_query is not None:
                    executed.append(executed_query)

                step = AgentStep(
                    index=len(steps),
                    tool_name=call.name,
                    arguments=_safe_args(call.arguments),
                    result_summary=summary,
                    row_count=row_count,
                    elapsed_ms=elapsed_ms,
                    error=result.is_error,
                )
                steps.append(step)
                if self.on_step is not None:
                    _fire(self.on_step, step)
                if logger.isEnabledFor(logging.DEBUG) or _debug:
                    logger.debug(
                        "step %d ← %s in %dms rows=%d error=%s :: %s",
                        step_index,
                        call.name,
                        elapsed_ms,
                        row_count,
                        result.is_error,
                        summary,
                    )

        # Step budget exhausted: give the model one last chance to answer plainly.
        messages.append(
            {
                "role": "user",
                "content": (
                    "You have used the maximum number of tool steps. Give your best answer "
                    "now from what you already know, state the row counts you have, and say "
                    "explicitly what you could not determine. Do not call any more tools."
                ),
            }
        )
        try:
            reply = await self.llm.chat(messages, tools=None)
            answer = (reply.get("content") or "").strip()
        except MongoChatError:
            answer = ""
        if not answer:
            answer = (
                "I hit the step limit of "
                f"{self.max_steps} tool calls before reaching an answer. "
                "Ask a narrower question or raise --max-steps."
            )
        else:
            answer = answer + "\n\n*(note: the tool step limit was reached)*"
        return AgentResult(
            answer=answer,
            steps=steps,
            truncated=truncated,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            executed_queries=executed,
        )


# Module-level flag set by the CLI/server so `run()` can log without extra wiring.
_debug = False


def set_debug(enabled: bool) -> None:
    global _debug
    _debug = enabled


def _fire(callback: Any, step: AgentStep) -> None:
    try:
        callback(step)
    except Exception:
        logger.debug("on_step callback failed", exc_info=True)


def _to_tool_call(raw: Any, *, fallback_id: str) -> ToolCall:
    """Accept both dict-shaped (replayed) and object-shaped tool calls."""
    if isinstance(raw, dict):
        function = raw.get("function") or {}
        args = function.get("arguments") or "{}"
        return ToolCall(
            id=raw.get("id") or fallback_id,
            name=str(function.get("name") or ""),
            arguments=_parse_args(args),
        )
    function = getattr(raw, "function", None)
    return ToolCall(
        id=getattr(raw, "id", None) or fallback_id,
        name=str(getattr(function, "name", "") or ""),
        arguments=_parse_args(getattr(function, "arguments", "{}") or "{}"),
    )


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"_malformed_json": raw[:500]}
    if not isinstance(parsed, dict):
        return {"_unexpected_shape": parsed}
    return parsed


def _safe_args(arguments: dict[str, Any]) -> dict[str, Any]:
    """Arguments for logging: keys that look like secrets are redacted."""
    out: dict[str, Any] = {}
    for key, value in arguments.items():
        lowered = key.lower()
        if any(token in lowered for token in ("key", "token", "secret", "password", "uri")):
            out[key] = redact_secret(str(value)) if isinstance(value, str) else "***"
        else:
            out[key] = value
    return out


def _summarize(content: str) -> tuple[int, bool, str, dict[str, Any] | None]:
    """Pull row_count / truncated / executed_query out of a run_query payload."""
    row_count = 0
    truncated = False
    executed: dict[str, Any] | None = None
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return 0, False, truncate_text(content.replace("\n", " "), 200), None
    if isinstance(data, dict):
        if isinstance(data.get("row_count"), int):
            row_count = data["row_count"]
        truncated = bool(data.get("truncated"))
        if isinstance(data.get("executed_query"), dict):
            executed = data["executed_query"]
        summary = ", ".join(f"{k}={v}" for k, v in data.items() if k != "rows")
    else:
        summary = str(data)
    return row_count, truncated, truncate_text(summary.replace("\n", " "), 200), executed
