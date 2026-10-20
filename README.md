# mongochat

Ask natural-language questions about a MongoDB database and get accurate, cited answers.

mongochat runs a small, readable tool-calling agent loop over **read-only** MongoDB tools.
The same tool registry is exposed four ways: a CLI, a web dashboard, an MCP server, and an
OpenAI-compatible HTTP API.

## Requirements

- Python 3.12+ ([uv](https://docs.astral.sh/uv/) recommended)
- A MongoDB database (read-only credentials are enough)
- An OpenAI-compatible API key (`OPENAI_API_KEY`; any `--base-url` works: OpenAI, Groq, Ollama, …)

## Install

```bash
uv sync                 # creates .venv and installs the mongochat script
uv run mongochat --help
```

## Configuration

Precedence: **CLI flag > environment variable > `.env` file > default.** Put values in a
`.env` in the working directory or export them.

| Variable | Purpose | Default |
| --- | --- | --- |
| `MONGODB_URI` | MongoDB connection string | — (required) |
| `MONGODB_DATABASE` | Database to query | — (required) |
| `OPENAI_API_KEY` | LLM key | — (needed to ask questions) |
| `OPENAI_BASE_URL` | OpenAI-compatible endpoint | `https://api.openai.com/v1` |
| `CHAT_MODEL` | Model name | `gpt-4o-mini` |
| `MONGOCHAT_API_TOKEN` | If set, HTTP APIs require `Authorization: Bearer <token>` | unset |
| `MONGOCHAT_DEBUG` | Verbose logging and uvicorn debug logs | `false` |
| `MONGOCHAT_MAX_STEPS` | Agent step limit per question | `8` |

Every command also accepts `--uri --db --api-key --base-url --model --max-steps --debug`.

```bash
uv run mongochat config      # effective values, redacted, with each value's source
```

Secrets are never printed in full, never written to `localStorage`/cookies, and the MongoDB
tools are read-only (no `insert`, `update`, `delete`, `drop`).

## Web dashboard

```bash
uv run mongochat serve                    # http://127.0.0.1:8000, opens your browser
uv run mongochat serve --port 9000
uv run mongochat serve --host 0.0.0.0     # warns unless MONGOCHAT_API_TOKEN is set
uv run mongochat serve --no-browser       # do not open a browser
```

On start it prints a banner with the URL and every config value plus its source, then opens
the browser.

If `MONGODB_URI`, `MONGODB_DATABASE` and `MONGOCHAT_API_TOKEN` (when set) are already
configured, the dashboard connects automatically on page load — no dialog. Otherwise the
Connect dialog appears and:

- fields backed by server config stay optional and say so (leave blank to use them);
- the API-key field is skipped when `OPENAI_API_KEY` is set;
- an access-token field appears only when `MONGOCHAT_API_TOKEN` is set;
- the button shows `Connecting…`, and errors render inside the dialog.

Sidebar: collections with document counts and indexed attribute paths (click to insert into
your question), plus **Refresh index** / **Reset index**.

## CLI

```bash
uv run mongochat chat                          # interactive REPL (builds/loads the schema index)
uv run mongochat ask "Which product sold most in March?"
uv run mongochat ask --no-steps "How many users signed up last week?"
uv run mongochat collections                   # collections with document counts
uv run mongochat tools                         # tool names, descriptions, JSON schemas
uv run mongochat config                        # redacted config + source per value
uv run mongochat index                         # show the cached schema index summary
uv run mongochat index --refresh               # rebuild the index now
uv run mongochat index --reset                 # delete the cached index
uv run mongochat index --json                  # full JSON index
```

## MCP server

Runs the same tools over stdio. No LLM key needed — your MCP client does the reasoning.

```bash
uv run mongochat mcp
```

```json
{
  "mcpServers": {
    "mongochat": {
      "command": "uv",
      "args": ["run", "--directory", "C:\\path\\to\\mongochat", "mongochat", "mcp"],
      "env": { "MONGODB_URI": "mongodb://…", "MONGODB_DATABASE": "shop" }
    }
  }
}
```

## HTTP API

`mongochat serve` hosts all of it (`/docs` has the interactive OpenAPI docs).

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | Web dashboard |
| `GET` | `/api/health` | Liveness + session count |
| `GET` | `/api/config` | What the server already has configured (no secrets) |
| `POST` | `/api/session` | Create a session `{uri, database, api_key?, base_url?, model?}` — empty strings fall back to server config |
| `POST` | `/api/chat` | `{session_id, message}` → `{answer, steps, row_count, elapsed_ms}` |
| `GET` | `/api/trace/{session_id}` | Live tool-step progress (polled by the UI) |
| `GET` | `/api/schema/{session_id}` | Cached schema index |
| `POST` | `/api/schema/{session_id}/refresh` | Rebuild the index |
| `DELETE` | `/api/schema/{session_id}` | Drop the cached index |
| `DELETE` | `/api/session/{id}` | Close a session |
| `GET` | `/v1/models` | OpenAI-compatible model list |
| `POST` | `/v1/chat/completions` | OpenAI-compatible chat (single JSON response; `stream=true` is rejected) |

Sessions live in memory and die with the process; only the schema index cache persists.

```bash
curl -s localhost:8000/api/config

curl -s localhost:8000/api/session \
  -H 'content-type: application/json' \
  -d '{"uri":"","database":""}'

curl -s localhost:8000/api/chat \
  -H 'content-type: application/json' \
  -d '{"session_id":"…","message":"How many orders are unpaid?"}'

# OpenAI-compatible (uses the server's MONGODB_URI / MONGODB_DATABASE / OPENAI_API_KEY):
curl -s localhost:8000/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"Top 5 customers by revenue"}]}'
```

When `MONGOCHAT_API_TOKEN` is set, add `-H "Authorization: Bearer $MONGOCHAT_API_TOKEN"`
to every `/api/*` and `/v1/*` request (`/`, `/api/health` and `/api/config` stay open).

## Development

```bash
uv run pytest          # tests
uv run ruff check .    # lint
uv run ruff format .   # format
uv run mypy mongochat  # type check
```

Static assets in `mongochat/static/` are plain HTML/CSS/JS with no build step and are
excluded from lint/type checks.
