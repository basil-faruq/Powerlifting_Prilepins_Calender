"""Application settings.

Every module reads configuration through `get_settings()`; nothing else in the
codebase touches `os.environ`. `planner_core` knows nothing about `domains/` —
that dependency points one way only.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Loaded from the environment, falling back to `.env`, then to these defaults."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Infrastructure -------------------------------------------------
    database_url: str = "postgresql+psycopg://planner:planner@localhost:5432/planner"
    temporal_address: str = "localhost:7233"
    temporal_task_queue: str = "strength-planner"
    timezone: str = "Europe/Berlin"

    # --- LLM provider ---------------------------------------------------
    llm_provider: Literal["ollama", "anthropic"] = "ollama"

    # Ollama runs natively on the host, never in Compose, so it can use the GPU.
    ollama_base_url: str = "http://localhost:11434"
    ollama_chat_model: str = "qwen2.5:14b"
    ollama_router_model: str = "qwen2.5:14b"
    ollama_embed_model: str = "nomic-embed-text"
    # Bounded because it sizes `note_chunk.embedding`: a zero or negative value
    # would make every embedding fail with a message blaming the model.
    embed_dim: int = Field(default=768, ge=1)

    # Anthropic is for demos only. Embeddings stay local in every mode.
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-sonnet-5-5"
    anthropic_router_model: str = "claude-haiku-5-5"

    # --- Tracing (spec 14) ----------------------------------------------
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str = "https://cloud.langfuse.com"
    langfuse_project_id: str | None = None

    # --- API ------------------------------------------------------------
    # Complex types are parsed from the environment as JSON, so `.env` needs
    # CORS_ORIGINS=["http://localhost:5173"] — a bare URL will not parse.
    cors_origins: list[str] = ["http://localhost:5173"]

    @field_validator(
        "anthropic_api_key",
        "langfuse_public_key",
        "langfuse_secret_key",
        mode="after",
    )
    @classmethod
    def _blank_secret_is_missing(cls, value: SecretStr | None) -> SecretStr | None:
        """`.env.example` ships these keys empty; treat blank as unset."""
        if value is not None and not value.get_secret_value().strip():
            return None
        return value

    @model_validator(mode="after")
    def _check_provider_credentials(self) -> Settings:
        if self.llm_provider == "anthropic" and self.anthropic_api_key is None:
            raise ValueError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
