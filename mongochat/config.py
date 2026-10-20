"""Configuration: CLI flag > environment variable > .env file > default."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import SecretStr, field_serializer
from pydantic_settings import BaseSettings, SettingsConfigDict

from mongochat.types import ConfigError, redact_secret

SourceName = Literal["flag", "env", "dotenv", "default"]

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_MAX_STEPS = 8

# Which source supplied each setting, filled in by `Settings.resolve`.
SOURCES: dict[str, SourceName] = {
    "mongodb_uri": "default",
    "mongodb_database": "default",
    "openai_api_key": "default",
    "openai_base_url": "default",
    "chat_model": "default",
    "api_token": "default",
    "debug": "default",
    "max_steps": "default",
}


class Settings(BaseSettings):
    """Raw settings, read from environment variables and .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    mongodb_uri: str = ""
    mongodb_database: str = ""
    openai_api_key: str = ""
    openai_base_url: str = DEFAULT_BASE_URL
    chat_model: str = DEFAULT_MODEL
    mongochat_api_token: str = ""
    mongochat_debug: bool = False
    mongochat_max_steps: int = DEFAULT_MAX_STEPS

    @field_serializer("openai_api_key")
    def _ser_key(self, value: str) -> str:
        return redact_secret(value) if value else ""

    @field_serializer("mongochat_api_token")
    def _ser_token(self, value: str) -> str:
        return redact_secret(value) if value else ""


@dataclass(frozen=True)
class FlagOverrides:
    """Values supplied on the command line, which win over everything else."""

    uri: str | None = None
    db: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    max_steps: int | None = None
    debug: bool | None = None

    def provided(self) -> set[str]:
        out: set[str] = set()
        for name, value in (
            ("uri", self.uri),
            ("db", self.db),
            ("api_key", self.api_key),
            ("base_url", self.base_url),
            ("model", self.model),
            ("max_steps", self.max_steps),
            ("debug", self.debug),
        ):
            if value is not None:
                out.add(name)
        return out


class Config(BaseSettings):
    """Effective configuration plus the source of each value."""

    model_config = SettingsConfigDict(extra="ignore")

    mongodb_uri: str = ""
    mongodb_database: str = ""
    openai_api_key: SecretStr | None = None
    openai_base_url: str = DEFAULT_BASE_URL
    chat_model: str = DEFAULT_MODEL
    api_token: SecretStr | None = None
    debug: bool = False
    max_steps: int = DEFAULT_MAX_STEPS
    sources: dict[str, SourceName] = None  # type: ignore[assignment]

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("sources", dict(SOURCES))
        super().__init__(**kwargs)

    # -- convenience -------------------------------------------------------- #

    @property
    def api_key(self) -> SecretStr | None:
        return self.openai_api_key

    @property
    def token(self) -> SecretStr | None:
        return self.api_token

    def requires_llm(self) -> bool:
        return True

    def redacted(self) -> dict[str, Any]:
        """A printable, key-safe view of the effective configuration."""

        def show(value: Any) -> Any:
            if isinstance(value, SecretStr):
                return redact_secret(value.get_secret_value())
            return value

        return {
            "mongodb_uri": redact_uri_safe(self.mongodb_uri) or "(not set)",
            "mongodb_database": self.mongodb_database or "(not set)",
            "openai_api_key": show(self.openai_api_key) or "(not set)",
            "openai_base_url": self.openai_base_url,
            "chat_model": self.chat_model,
            "mongochat_api_token": show(self.api_token) or "(not set)",
            "mongochat_debug": self.debug,
            "mongochat_max_steps": self.max_steps,
        }

    def source_table(self) -> dict[str, SourceName]:
        return dict(self.sources)


def redact_uri_safe(uri: str) -> str:
    from mongochat.types import redact_uri

    return redact_uri(uri) if uri else ""


def _dotenv_has(key: str) -> bool:
    """True if `key` is set in a .env file (and not in the environment)."""
    import os

    if key in os.environ:
        return False
    path = Path(".env")
    if not path.is_file():
        return False
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            name = stripped.split("=", 1)[0].strip().strip("\"'")
            if name.lower() == key.lower():
                return True
    except OSError:
        return False
    return False


def _source(name: str, *, env_key: str, flag_set: bool) -> SourceName:
    if flag_set:
        return "flag"
    import os

    if env_key in os.environ:
        return "env"
    if _dotenv_has(env_key):
        return "dotenv"
    return "default"


def load_config(flags: FlagOverrides | None = None, *, env: dict[str, str] | None = None) -> Config:
    """Resolve the effective configuration.

    Precedence: CLI flag > environment variable > .env file > default.
    """
    import os

    flags = flags or FlagOverrides()
    provided = flags.provided()
    if env is not None:
        os.environ.update(env)

    raw = Settings()
    sources: dict[str, SourceName] = {
        "mongodb_uri": _source("uri", env_key="MONGODB_URI", flag_set="uri" in provided),
        "mongodb_database": _source("db", env_key="MONGODB_DATABASE", flag_set="db" in provided),
        "openai_api_key": _source(
            "api_key", env_key="OPENAI_API_KEY", flag_set="api_key" in provided
        ),
        "openai_base_url": _source(
            "base_url", env_key="OPENAI_BASE_URL", flag_set="base_url" in provided
        ),
        "chat_model": _source("model", env_key="CHAT_MODEL", flag_set="model" in provided),
        "api_token": ("env" if "MONGOCHAT_API_TOKEN" in os.environ else _dotenv_source_token()),
        "debug": ("flag" if flags.debug is not None else _source_bool("MONGOCHAT_DEBUG")),
        "max_steps": _source(
            "max_steps", env_key="MONGOCHAT_MAX_STEPS", flag_set="max_steps" in provided
        ),
    }

    uri = flags.uri or raw.mongodb_uri
    database = flags.db or raw.mongodb_database
    api_key = flags.api_key or raw.openai_api_key
    base_url = flags.base_url or raw.openai_base_url or DEFAULT_BASE_URL
    model = flags.model or raw.chat_model or DEFAULT_MODEL
    max_steps = flags.max_steps if flags.max_steps is not None else raw.mongochat_max_steps
    debug = flags.debug if flags.debug is not None else raw.mongochat_debug

    if max_steps < 1:
        raise ConfigError("--max-steps must be at least 1")

    return Config(
        mongodb_uri=uri,
        mongodb_database=database,
        openai_api_key=SecretStr(api_key) if api_key else None,
        openai_base_url=base_url.rstrip("/"),
        chat_model=model,
        api_token=SecretStr(raw.mongochat_api_token) if raw.mongochat_api_token else None,
        debug=bool(debug),
        max_steps=int(max_steps),
        sources=sources,
    )


def _source_bool(env_key: str) -> SourceName:
    import os

    if env_key in os.environ:
        return "env"
    return "dotenv" if _dotenv_has(env_key) else "default"


def _dotenv_source_token() -> SourceName:
    return "dotenv" if _dotenv_has("MONGOCHAT_API_TOKEN") else "default"


def require_uri_db(config: Config) -> tuple[str, str]:
    """Validate that a database target was configured, with a helpful message."""
    if not config.mongodb_uri:
        raise ConfigError("No MongoDB URI. Pass --uri or set MONGODB_URI (see .env.example).")
    if not config.mongodb_database:
        raise ConfigError("No database name. Pass --db or set MONGODB_DATABASE (see .env.example).")
    return config.mongodb_uri, config.mongodb_database
