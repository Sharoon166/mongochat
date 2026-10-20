# mongochat

Ask natural-language questions about a MongoDB database and get accurate, **cited** answers.

mongochat runs a small, readable tool-calling agent loop (no LangChain, ~60 lines) over a
set of **read-only** MongoDB tools. The model answers only from tool results, names the
collections it used, and states row counts. The same tool registry is exposed four ways:

| Surface | Entry point | Needs an LLM key? |
|---|---|---|
| CLI / REPL | `mongochat chat` | yes |
| Web dashboard | `mongochat serve` | yes |
| MCP server (stdio) | `mongochat mcp` | **no** — your client's model answers |
| OpenAI-compatible API | `POST /v1/chat/completions` | yes |

```
mongochat/
  mongochat/
    types.py          shared Pydantic models, type aliases, exceptions
    config.py         pydantic-settings, BYOK precedence (flag > env > .env > default)
    core/
      llm.py          the only code that talks to the model
      schema_index.py build / cache / load / refresh / reset / summarize the index
      validator.py    pure, security-critical pipeline validation
      executor.py     read-only execution with limits
      tools.py        one tool definition, exported as OpenAI + MCP schemas
      agent.py        the tool-calling loop
      session.py      connection + index + conversation history
    cli.py            typer app + rich REPL
    server.py         FastAPI: web UI, /api, and /v1
    mcp_server.py     FastMCP server exposing the same tools
    static/           vanilla HTML/CSS/JS dashboard (no build step)
  tests/
```

## Quickstart

```bash
uv sync
cp .env.example .env          # fill in MONGODB_URI, MONGODB_DATABASE, OPENAI_API_KEY
uv run mongochat chat --uri mongodb://localhost:27017 --db sample_mflix
```

Ask away:

```
you> which directors have the most films, and how many rows is that?
→ list_collections({}) · 9 rows · 12ms
→ run_query({"collection":"movies","operation":"aggregate","pipeline":[...]}) · 10 rows · 41ms
...
```

Single shot:

```bash
uv run mongochat ask "how many orders were placed last month?" --uri mongodb://localhost:27017 --db shop
```

## BYOK setup

All settings follow **CLI flag > environment variable > `.env` file > default**.
`mongochat config` prints the effective values, redacted, with each value's source:

```
| setting          | value            | source |
| openai_api_key   | gsk...r3HM       | env    |
| chat_model       | gpt-4o-mini      | default|
```

Keys are never logged, printed, or serialized.

### OpenAI

```bash
export OPENAI_API_KEY=sk-...
export CHAT_MODEL=gpt-4o-mini          # tool-capable
uv run mongochat chat --uri ... --db ...
```

### OpenRouter

```bash
export OPENAI_API_KEY=sk-or-v1-...
export OPENAI_BASE_URL=https://openrouter.ai/api/v1
export CHAT_MODEL=openai/gpt-4o-mini   # any model whose page lists "tools"
```

### Ollama (local)

```bash
ollama pull qwen2.5
export OPENAI_BASE_URL=http://localhost:11434/v1
export OPENAI_API_KEY=ollama           # any non-empty string
export CHAT_MODEL=qwen2.5
```

Tool calling needs a tool-capable model. For Ollama use **qwen2.5**, **llama3.1+**, or
**mistral-nemo**. If the provider rejects the `tools` parameter, mongochat raises
`LLMResponseError` naming the model and suggesting one of these.

### LM Studio

```bash
export OPENAI_BASE_URL=http://localhost:1234/v1
export OPENAI_API_KEY=lm-studio
export CHAT_MODEL=<loaded model name>  # enable "Tools" on the model you load
```

## CLI reference

```text
mongochat chat      [--uri] [--db] [--api-key] [--base-url] [--model] [--max-steps] [--debug]
mongochat ask "q"   same flags, plus --no-steps
mongochat tools     list tools with descriptions and JSON schemas
mongochat index --show [--json]     summary table, or full JSON
mongochat index --refresh           rebuild the cache now
mongochat index --reset             delete the cache for this host + database
mongochat collections               collections with document counts
mongochat config                    effective config, redacted, with the source of each value
mongochat serve   [--port 8000] [--host 127.0.0.1]  web dashboard + /v1 API
mongochat mcp     [--uri] [--db]   MCP server on stdio (no LLM key needed)
```

Global flags on the relevant commands: `--uri --db --api-key --base-url --model --max-steps --debug`.

### REPL slash commands

| Command | Effect |
|---|---|
| `/schema` | index summary for this session |
| `/steps` | the last turn's tool calls, arguments, row counts, timings |
| `/history` | conversation history (last 10 user/assistant turns) |
| `/clear` | forget the conversation |
| `/debug` | toggle debug logging (never prints keys or full result sets) |
| `/quit` | exit |

Answers render as Rich markdown followed by a compact line with steps, row count and
elapsed time; tool calls stream live (`→ describe_collection(orders)`); errors appear as
panels with a short message and a hint — no raw tracebacks unless `--debug`.

## Tools

Every tool is read-only and defined once, then exported to OpenAI and MCP schemas.

- `list_collections()` — names and document counts from the index
- `describe_collection(collection)` — attribute paths, types, indexes, cardinality, min/max
- `sample_documents(collection, limit ≤ 5)` — redacted samples (emails/tokens masked,
  strings truncated)
- `run_query(collection, operation, filter?, pipeline?, field?)` — `find`, `aggregate`,
  `count`, `distinct`, validated then executed under limits
- `get_time()` — current local date/time for relative questions

Tool failures come back as `ToolResult(is_error=true)` with a specific reason, so the model
corrects its next call instead of crashing.

**Limits:** find capped at 50 rows; aggregate appends `{$limit: 50}` unless a smaller
`$limit` exists; `allowDiskUse=False`, `maxTimeMS=10000`; distinct capped at 50 values;
tool results truncated to ~6,000 tokens before returning to the model.

**Validator:** rejects `$out`, `$merge`, `$function`, `$accumulator`, `$where`, `$eval` at
every depth (including inside `$facet`, `$lookup` sub-pipelines and `$expr`), rejects
`$lookup`/`$unionWith` targets missing from the index, allowlists query operators, caps
pipelines at 20 stages and 6 levels of nesting.

## Read-only user recommendation

mongochat never writes — but your database user might be able to. At connect time it runs
`connectionStatus`; if the user holds `readWrite`, `dbAdmin`, `root`, and friends, it
prints a warning (it does not block). Create a dedicated user:

```js
use admin
db.createUser({
  user: "mongochat",
  pwd: "choose-a-strong-password",
  roles: [ { role: "read", db: "shop" } ]
})
```

## MCP setup

```bash
uv run mongochat mcp --uri mongodb://localhost:27017 --db shop
```

No LLM key is needed: the calling client's model answers, mongochat only serves tools.

`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "mongochat": {
      "command": "uv",
      "args": [
        "--directory", "/absolute/path/to/mongochat",
        "run", "mongochat", "mcp",
        "--uri", "mongodb://localhost:27017",
        "--db", "shop"
      ]
    }
  }
}
```

The same block works for Cursor and other MCP clients that launch a stdio server.
Tools return text content; tool errors use the MCP error shape, so the server never crashes.

## OpenAI-compatible API

```bash
uv run mongochat serve --port 8000        # http://127.0.0.1:8000
```

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "which collections have over 1000 docs?"}],
        "stream": false
      }'
```

Response is a standard `chat.completion` with `usage`; tool calls stay internal and only
the final answer is returned. `stream=true` → **400** with a clear message. If
`MONGOCHAT_API_TOKEN` is set, send `Authorization: Bearer <token>`; without a token the
server binds to `127.0.0.1` and warns if `--host` is changed.

`GET /v1/models` returns one model: `mongochat-<database>`.

### Web API

```text
POST   /api/session                      {uri, database, api_key?, base_url?, model?}
POST   /api/chat                         {session_id, message, api_key?, base_url?, model?}
GET    /api/schema/{session_id}
POST   /api/schema/{session_id}/refresh
DELETE /api/schema/{session_id}
DELETE /api/session/{session_id}
GET    /api/trace/{session_id}           live step, used by the typing indicator
GET    /api/health
```

Max message length is 2,000 characters; errors use `{ "error": { "code", "message" } }`.
A request-supplied API key is used for that request only and never stored server-side.

## Web dashboard

`mongochat serve` serves a vanilla HTML/CSS/JS UI at `/` — no build step. Dark theme,
system fonts, sidebar becomes a drawer on mobile. The connection modal keeps the API key
**in JS memory only**: it is sent per request and never written to localStorage or
cookies. Markdown is rendered with Marked.js and sanitized (DOMPurify); every other string
is inserted with `textContent`. Each assistant message has a collapsible steps timeline
with arguments, row counts, timings, and the executed pipeline in a syntax-tinted block.

## Schema index

Built on first connection or with `mongochat index --refresh`, cached at
`~/.mongochat/index-<host>-<db>.json` (mode `0600`, TTL 24 h).

- Samples up to 1,000 documents (`$sample`; full scan when the collection is smaller)
- Discovers paths to depth 4; array elements appear as `field.$`, array traversal capped
  at 10 elements
- Per path: BSON types, a few sample values, cardinality estimate, min/max for numbers
  and dates
- Redaction: emails and token-like strings masked, strings truncated to 80 chars, 2
  redacted sample documents kept per collection

Sessions themselves are in-memory and new on every start; only the index cache persists.

## Development

```bash
uv sync --group dev
uv run pytest              # 129 tests
uv run ruff check .        # lint
uv run ruff format .       # format
uv run mypy mongochat      # strict-ish type checking
```

Tests cover the validator (forbidden operators at every depth), tool schemas and error
shapes, the agent loop (fake LLM: single/multiple tool calls, recovery, step limit,
off-topic answers), schema indexing (redaction, discovery, cache TTL/reset), and the
OpenAI-compatible API (response shape, `stream=true`, token enforcement).

## Graceful failure

Bad URI, unreachable server, auth failure, empty database, off-topic question, query
timeout, LLM auth failure, LLM rate limit, a model without tool support, malformed tool
arguments, and hitting the step limit all produce short, actionable messages — never a
stack trace unless `--debug`.
