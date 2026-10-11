"""Settings defaults, the Anthropic credential gate, and env parsing.

`_env_file=None` keeps a developer's local `.env` out of these assertions.
"""

import pytest
from pydantic import ValidationError

from planner_core.config import Settings, get_settings

ANTHROPIC_KEY_MESSAGE = "ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "DATABASE_URL",
        "TEMPORAL_ADDRESS",
        "TIMEZONE",
        "LLM_PROVIDER",
        "ANTHROPIC_API_KEY",
        "EMBED_DIM",
        "CORS_ORIGINS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_defaults_load_without_an_env_file():
    settings = Settings(_env_file=None)

    assert settings.database_url == "postgresql+psycopg://planner:planner@localhost:5432/planner"
    assert settings.temporal_address == "localhost:7233"
    assert settings.temporal_task_queue == "strength-planner"
    assert settings.timezone == "Europe/Berlin"
    assert settings.llm_provider == "ollama"
    assert settings.ollama_base_url == "http://localhost:11434"
    assert settings.ollama_embed_model == "nomic-embed-text"
    assert settings.embed_dim == 768
    assert settings.cors_origins == ["http://localhost:5173"]
    assert settings.anthropic_api_key is None
    assert settings.langfuse_public_key is None


def test_anthropic_without_a_key_is_refused():
    with pytest.raises(ValidationError, match=ANTHROPIC_KEY_MESSAGE):
        Settings(_env_file=None, llm_provider="anthropic")


def test_a_blank_anthropic_key_counts_as_missing():
    """`.env.example` ships `ANTHROPIC_API_KEY=`, which parses as an empty string."""
    with pytest.raises(ValidationError, match=ANTHROPIC_KEY_MESSAGE):
        Settings(_env_file=None, llm_provider="anthropic", anthropic_api_key="   ")


def test_anthropic_with_a_key_is_accepted():
    settings = Settings(_env_file=None, llm_provider="anthropic", anthropic_api_key="sk-test")

    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == "sk-test"


def test_ollama_is_the_default_provider_and_needs_no_key():
    assert Settings(_env_file=None).llm_provider == "ollama"


def test_an_unknown_provider_is_refused():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_provider="openai")


def test_blank_langfuse_keys_disable_tracing():
    settings = Settings(_env_file=None, langfuse_public_key="", langfuse_secret_key="")

    assert settings.langfuse_public_key is None
    assert settings.langfuse_secret_key is None


def test_cors_origins_is_parsed_from_json_in_the_environment(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", '["http://localhost:5173", "http://localhost:4173"]')

    assert Settings(_env_file=None).cors_origins == [
        "http://localhost:5173",
        "http://localhost:4173",
    ]


def test_env_vars_override_defaults(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "UTC")
    monkeypatch.setenv("EMBED_DIM", "1024")

    settings = Settings(_env_file=None)

    assert settings.timezone == "UTC"
    assert settings.embed_dim == 1024


def test_get_settings_is_cached():
    assert get_settings() is get_settings()
