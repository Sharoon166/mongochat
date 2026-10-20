"""The mongochat CLI: a typer app with a rich REPL."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from mongochat.config import Config, FlagOverrides, load_config, require_uri_db
from mongochat.core.session import Session
from mongochat.core.tools import ToolRegistry
from mongochat.types import AgentResult, AgentStep, LLMConfig, MongoChatError

app = typer.Typer(
    name="mongochat",
    help="Ask natural-language questions about a MongoDB database.",
    add_completion=False,
    no_args_is_help=True,
)
index_app = typer.Typer(help="Manage the schema index cache.", no_args_is_help=False)
app.add_typer(index_app, name="index")

console = Console()
err_console = Console(stderr=True)

logger = logging.getLogger("mongochat.cli")


# --------------------------------------------------------------------------- #
# Shared options
# --------------------------------------------------------------------------- #


def setup_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("mongochat").setLevel(level)


def llm_config(config: Config) -> LLMConfig | None:
    if config.openai_api_key is None:
        return None
    return LLMConfig(
        base_url=config.openai_base_url,
        model=config.chat_model,
        api_key=config.openai_api_key,
    )


def fail(message: str, *, hint: str | None = None, code: int = 1) -> None:
    body = Text(message)
    err_console.print(Panel(body, title="Error", border_style="red", expand=False))
    if hint:
        err_console.print(Text(f"Hint: {hint}", style="dim"))
    raise typer.Exit(code)


def report_error(exc: MongoChatError, *, debug: bool) -> None:
    hints = {
        "ConfigError": "Run `mongochat config` to see what is missing.",
        "ConnectionError": "Check the URI and that mongod is reachable.",
        "LLMAuthError": "Check OPENAI_API_KEY or pass --api-key.",
        "LLMRateLimitError": "Wait a moment and retry.",
        "LLMResponseError": "Pick a model that supports tool calling, e.g. gpt-4o-mini.",
        "QueryTimeoutError": "Narrow the filter or add a $limit.",
        "UnsafeQueryError": "Remove the rejected operator and try again.",
        "AgentStepLimitError": "Ask a narrower question or raise --max-steps.",
    }
    name = type(exc).__name__
    if debug:
        err_console.print_exception(show_locals=False)
    fail(str(exc), hint=hints.get(name))


async def open_session(config: Config, *, rebuild: bool = False) -> Session:
    uri, database = require_uri_db(config)
    with console.status("[dim]Connecting and indexing…[/dim]"):
        session = await Session.create(
            uri=uri,
            database=database,
            llm_config=llm_config(config),
            max_steps=config.max_steps,
            debug=config.debug,
            rebuild_index=rebuild,
        )
    if session.warning:
        err_console.print(
            Panel(session.warning, title="Read-only recommendation", border_style="yellow")
        )
    return session


# --------------------------------------------------------------------------- #
# chat / ask
# --------------------------------------------------------------------------- #


@app.command()
def chat(
    uri: Annotated[str | None, typer.Option("--uri", show_default=False)] = None,
    db: Annotated[str | None, typer.Option("--db", show_default=False)] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", show_default=False)] = None,
    base_url: Annotated[str | None, typer.Option("--base-url", show_default=False)] = None,
    model: Annotated[str | None, typer.Option("--model", show_default=False)] = None,
    max_steps: Annotated[int | None, typer.Option("--max-steps")] = None,
    debug: Annotated[bool, typer.Option("--debug")] = False,
) -> None:
    """Interactive REPL. Builds or loads the schema index on start."""
    flags = FlagOverrides(
        uri=uri,
        db=db,
        api_key=api_key,
        base_url=base_url,
        model=model,
        max_steps=max_steps,
        debug=True if debug else None,
    )
    config = load_config(flags)
    setup_logging(config.debug)
    try:
        asyncio.run(_repl(config))
    except KeyboardInterrupt:
        console.print("\n[dim]bye[/dim]")


async def _repl(config: Config) -> None:
    try:
        session = await open_session(config)
    except MongoChatError as exc:
        report_error(exc, debug=config.debug)
        return

    console.print(
        Panel(
            f"[bold]mongochat[/bold] on [cyan]{session.connection.database}[/cyan] "
            f"({len(session.collections)} collections) · model "
            f"[green]{config.chat_model}[/green]\n"
            "[dim]Type a question, or /help for slash commands.[/dim]",
            border_style="blue",
        )
    )
    last_result: AgentResult | None = None
    try:
        while True:
            try:
                question = console.input("[bold blue]you>[/bold blue] ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not question:
                continue
            if question.startswith("/"):
                action = question.split()[0].lower()
                if action in {"/quit", "/exit", "/q"}:
                    break
                if action in {"/help", "/h"}:
                    _print_help()
                    continue
                if action == "/clear":
                    session.history.clear()
                    last_result = None
                    console.print("[dim]history cleared[/dim]")
                    continue
                if action == "/debug":
                    config = load_config(
                        FlagOverrides(
                            uri=config.mongodb_uri,
                            db=config.mongodb_database,
                            debug=not config.debug,
                        )
                    )
                    session.debug = config.debug
                    from mongochat.core.agent import set_debug

                    set_debug(config.debug)
                    console.print(f"[dim]debug {'on' if config.debug else 'off'}[/dim]")
                    continue
                if action == "/history":
                    _print_history(session.history)
                    continue
                if action in {"/schema", "/index"}:
                    _print_schema(session)
                    continue
                if action == "/steps":
                    if last_result is None:
                        console.print("[dim]no turn yet[/dim]")
                    else:
                        _print_steps(last_result)
                    continue
                console.print(f"[red]unknown command {action}[/red] — try /help")
                continue

            try:
                result = await _ask_with_trace(session, question, debug=config.debug)
            except MongoChatError as exc:
                report_error(exc, debug=config.debug)
                continue
            last_result = result
            console.print(Markdown(result.answer))
            _print_footer(result)
    finally:
        await session.close()


async def _ask_with_trace(session: Session, question: str, *, debug: bool) -> AgentResult:
    status_text = Text.assemble(("→ ", "cyan"), ("thinking…", "dim"))
    with Live(status_text, console=console, refresh_per_second=8, transient=True) as live:

        def on_step(step: AgentStep) -> None:
            mark = "[red]✗[/red]" if step.error else "[cyan]→[/cyan]"
            live.update(
                Text.from_markup(
                    f"{mark} [bold]{step.tool_name}[/bold] [dim]{json.dumps(step.arguments)[:80]} "
                    f"· {step.row_count} rows · {step.elapsed_ms}ms[/dim]"
                )
            )

        return await session.ask(question, on_step=on_step)


def _print_footer(result: AgentResult) -> None:
    rows = sum(s.row_count for s in result.steps)
    tools = ", ".join(s.tool_name for s in result.steps) or "none"
    parts = [f"steps {len(result.steps)}", f"rows {rows}", f"{result.elapsed_ms}ms"]
    if result.truncated:
        parts.append("truncated")
    console.print(Text(f"[{' · '.join(parts)}] tools: {tools}", style="dim"))
    if result.steps:
        _print_steps(result)


def _print_steps(result: AgentResult) -> None:
    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
    table.add_column("#", style="dim", width=3)
    table.add_column("tool")
    table.add_column("args", overflow="fold")
    table.add_column("rows", justify="right")
    table.add_column("ms", justify="right")
    table.add_column("status")
    for step in result.steps:
        table.add_row(
            str(step.index),
            step.tool_name,
            json.dumps(step.arguments, default=str)[:90],
            str(step.row_count),
            str(step.elapsed_ms),
            "[red]error[/red]" if step.error else "ok",
        )
    console.print(table)
    for query in result.executed_queries:
        console.print(
            Panel(
                json.dumps(query, indent=2, default=str),
                title="executed query",
                border_style="dim",
                expand=False,
            )
        )


def _print_schema(session: Session) -> None:
    index = session.index
    table = Table(show_header=True, header_style="bold")
    table.add_column("collection")
    table.add_column("docs", justify="right")
    table.add_column("attributes", justify="right")
    table.add_column("indexes")
    for coll in index.collections:
        table.add_row(
            coll.name,
            str(coll.document_count),
            str(len(coll.attributes)),
            ", ".join(coll.indexes[:4]),
        )
    console.print(table)
    for coll in index.collections:
        paths = ", ".join(a.path for a in coll.attributes[:20])
        console.print(f"  [cyan]{coll.name}[/cyan]: {paths}")


def _print_history(history: list[dict[str, Any]]) -> None:
    if not history:
        console.print("[dim]history is empty[/dim]")
        return
    for message in history:
        role = message.get("role", "?")
        content = str(message.get("content") or "")
        style = "bold blue" if role == "user" else "green"
        console.print(f"[{style}]{role}>[/{style}] {content[:400]}")


def _print_help() -> None:
    console.print(
        Panel(
            "/schema   show the index for this session\n"
            "/steps    show the last turn's tool calls\n"
            "/history  show conversation history\n"
            "/clear    forget the conversation\n"
            "/debug    toggle debug logging\n"
            "/quit     exit",
            title="commands",
            border_style="blue",
        )
    )


# --------------------------------------------------------------------------- #
# ask
# --------------------------------------------------------------------------- #


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="The question to answer.")],
    uri: Annotated[str | None, typer.Option("--uri", show_default=False)] = None,
    db: Annotated[str | None, typer.Option("--db", show_default=False)] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", show_default=False)] = None,
    base_url: Annotated[str | None, typer.Option("--base-url", show_default=False)] = None,
    model: Annotated[str | None, typer.Option("--model", show_default=False)] = None,
    max_steps: Annotated[int | None, typer.Option("--max-steps")] = None,
    debug: Annotated[bool, typer.Option("--debug")] = False,
    no_steps: Annotated[bool, typer.Option("--no-steps", help="Hide the step table.")] = False,
) -> None:
    """Answer a single question and exit."""
    flags = FlagOverrides(
        uri=uri,
        db=db,
        api_key=api_key,
        base_url=base_url,
        model=model,
        max_steps=max_steps,
        debug=True if debug else None,
    )
    config = load_config(flags)
    setup_logging(config.debug)
    asyncio.run(_ask_once(config, question, no_steps=no_steps))


async def _ask_once(config: Config, question: str, *, no_steps: bool) -> None:
    session = None
    try:
        session = await open_session(config)
        result = await session.ask(question, on_step=_live_step)
    except MongoChatError as exc:
        report_error(exc, debug=config.debug)
        return
    finally:
        if session is not None:
            await session.close()
    console.print(Markdown(result.answer))
    if not no_steps:
        _print_footer(result)


def _live_step(step: AgentStep) -> None:
    mark = "✗" if step.error else "→"
    console.print(
        f"[cyan]{mark} {step.tool_name}[/cyan] "
        f"[dim]{json.dumps(step.arguments, default=str)[:80]} · {step.elapsed_ms}ms[/dim]"
    )


# --------------------------------------------------------------------------- #
# tools / collections / config
# --------------------------------------------------------------------------- #


@app.command()
def tools() -> None:
    """List the tools with their descriptions and schemas."""
    registry = ToolRegistry(db=None, get_index=_empty_index)
    table = Table(show_header=True, header_style="bold")
    table.add_column("tool")
    table.add_column("description", overflow="fold")
    table.add_column("arguments")
    for tool in registry.tools:
        props = tool.openai_schema()["function"]["parameters"]["properties"]
        args = ", ".join(props) or "(none)"
        table.add_row(tool.name, tool.description, args)
    console.print(table)
    for schema in registry.mcp_tools():
        console.print(
            Panel(
                json.dumps(schema["inputSchema"], indent=2, default=str),
                title=schema["name"],
                border_style="dim",
                expand=False,
            )
        )


def _empty_index() -> Any:
    from datetime import UTC, datetime

    from mongochat.types import SchemaIndex

    return SchemaIndex(database="", generated_at=datetime.now(UTC), collections=[])


@app.command()
def collections(
    uri: Annotated[str | None, typer.Option("--uri", show_default=False)] = None,
    db: Annotated[str | None, typer.Option("--db", show_default=False)] = None,
    debug: Annotated[bool, typer.Option("--debug")] = False,
) -> None:
    """Collections with document counts."""
    config = load_config(FlagOverrides(uri=uri, db=db, debug=True if debug else None))
    setup_logging(config.debug)
    asyncio.run(_collections(config))


async def _collections(config: Config) -> None:
    try:
        session = await open_session(config)
    except MongoChatError as exc:
        report_error(exc, debug=config.debug)
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("collection")
    table.add_column("documents", justify="right")
    for coll in session.index.collections:
        table.add_row(coll.name, f"{coll.document_count:,}")
    if not table.row_count:
        console.print("[yellow]No collections in this database.[/yellow]")
    else:
        console.print(table)
    await session.close()


@app.command()
def config(
    uri: Annotated[str | None, typer.Option("--uri", show_default=False)] = None,
    db: Annotated[str | None, typer.Option("--db", show_default=False)] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", show_default=False)] = None,
    base_url: Annotated[str | None, typer.Option("--base-url", show_default=False)] = None,
    model: Annotated[str | None, typer.Option("--model", show_default=False)] = None,
    max_steps: Annotated[int | None, typer.Option("--max-steps")] = None,
    debug: Annotated[bool, typer.Option("--debug")] = False,
) -> None:
    """Show the effective configuration, redacted, with each value's source."""
    flags = FlagOverrides(
        uri=uri,
        db=db,
        api_key=api_key,
        base_url=base_url,
        model=model,
        max_steps=max_steps,
        debug=True if debug else None,
    )
    effective = load_config(flags)
    table = Table(show_header=True, header_style="bold")
    table.add_column("setting")
    table.add_column("value")
    table.add_column("source", style="dim")
    values = effective.redacted()
    sources = effective.source_table()
    for key, value in values.items():
        table.add_row(key, str(value), sources.get(key, "default"))
    console.print(table)
    console.print(
        "[dim]Keys are redacted and never printed in full. "
        "Precedence: flag > env > .env > default.[/dim]"
    )


# --------------------------------------------------------------------------- #
# index
# --------------------------------------------------------------------------- #


@index_app.callback(invoke_without_command=True)
def index_root(
    ctx: typer.Context,
    show: Annotated[bool, typer.Option("--show", help="Show the index summary.")] = True,
    refresh: Annotated[bool, typer.Option("--refresh", help="Rebuild the index now.")] = False,
    reset: Annotated[bool, typer.Option("--reset", help="Delete the cached index.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print full JSON.")] = False,
    uri: Annotated[str | None, typer.Option("--uri", show_default=False)] = None,
    db: Annotated[str | None, typer.Option("--db", show_default=False)] = None,
    debug: Annotated[bool, typer.Option("--debug")] = False,
) -> None:
    """Show, refresh, or reset the schema index for a host and database."""
    if ctx.invoked_subcommand:
        return
    config = load_config(FlagOverrides(uri=uri, db=db, debug=True if debug else None))
    setup_logging(config.debug)
    asyncio.run(_index_command(config, show=show, refresh=refresh, reset=reset, as_json=as_json))


async def _index_command(
    config: Config, *, show: bool, refresh: bool, reset: bool, as_json: bool
) -> None:
    try:
        uri, database = require_uri_db(config)
    except MongoChatError as exc:
        report_error(exc, debug=config.debug)
        return

    from mongochat.core.schema_index import SchemaIndexBuilder
    from mongochat.core.session import connect

    try:
        client, db = await connect(uri, database)
    except MongoChatError as exc:
        report_error(exc, debug=config.debug)
        return
    builder = SchemaIndexBuilder(db, uri=uri, database=database)
    try:
        if reset:
            deleted = builder.reset()
            console.print(
                "[green]index cache deleted[/green]"
                if deleted
                else "[dim]no cache file to delete[/dim]"
            )
            return
        index = await builder.refresh() if refresh else await builder.load_or_build()
    except MongoChatError as exc:
        client.close()
        report_error(exc, debug=config.debug)
        return
    client.close()

    if as_json:
        console.print_json(json.dumps(index.model_dump(mode="json"), default=str))
        return
    summary = builder.show(index)
    table = Table(show_header=True, header_style="bold", title=f"index · {summary['database']}")
    table.add_column("collection")
    table.add_column("docs", justify="right")
    table.add_column("avg size", justify="right")
    table.add_column("attributes", justify="right")
    table.add_column("indexes", justify="right")
    for row in summary["collections"]:
        table.add_row(
            row["name"],
            f"{row['document_count']:,}",
            f"{row['avg_doc_size']:,}B" if row["avg_doc_size"] else "-",
            str(row["attributes"]),
            str(row["indexes"]),
        )
    if not summary["collections"]:
        console.print("[yellow]No collections in this database.[/yellow]")
    else:
        console.print(table)
    console.print(f"[dim]generated {summary['generated_at']} · cache {summary['cache_path']}[/dim]")


# --------------------------------------------------------------------------- #
# serve / mcp
# --------------------------------------------------------------------------- #


@app.command()
def serve(
    port: Annotated[int, typer.Option("--port", help="Port to listen on.")] = 8000,
    host: Annotated[str, typer.Option("--host", help="Bind address.")] = "127.0.0.1",
    uri: Annotated[str | None, typer.Option("--uri", show_default=False)] = None,
    db: Annotated[str | None, typer.Option("--db", show_default=False)] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", show_default=False)] = None,
    base_url: Annotated[str | None, typer.Option("--base-url", show_default=False)] = None,
    model: Annotated[str | None, typer.Option("--model", show_default=False)] = None,
    max_steps: Annotated[int | None, typer.Option("--max-steps")] = None,
    debug: Annotated[bool, typer.Option("--debug")] = False,
) -> None:
    """Run the web dashboard and the /v1 API."""
    flags = FlagOverrides(
        uri=uri,
        db=db,
        api_key=api_key,
        base_url=base_url,
        model=model,
        max_steps=max_steps,
        debug=True if debug else None,
    )
    config = load_config(flags)
    setup_logging(config.debug)
    from mongochat.server import run_server

    run_server(config, host=host, port=port)


@app.command()
def mcp(
    uri: Annotated[str | None, typer.Option("--uri", show_default=False)] = None,
    db: Annotated[str | None, typer.Option("--db", show_default=False)] = None,
    debug: Annotated[bool, typer.Option("--debug")] = False,
) -> None:
    """Run the MCP server on stdio (no LLM key needed)."""
    flags = FlagOverrides(uri=uri, db=db, debug=True if debug else None)
    config = load_config(flags)
    setup_logging(config.debug)
    from mongochat.mcp_server import run_mcp

    run_mcp(config)


def main() -> None:  # pragma: no cover - console entry point
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
