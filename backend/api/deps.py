"""Shared FastAPI dependencies. Routes stay thin; these are what they lean on."""

from typing import Annotated

from fastapi import Depends
from sqlmodel import Session

from domains.strength.agent.schemas import ChatModels
from planner_core.config import Settings, get_settings
from planner_core.db import get_session
from planner_core.llm import Embedder, get_embedder, get_model

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
# A dependency rather than a direct `get_embedder()` call so a test can override
# it with a fake and no route reaches Ollama (CLAUDE.md: no real model in CI).
EmbedderDep = Annotated[Embedder, Depends(get_embedder)]


def get_chat_models() -> ChatModels:
    """The two models one chat turn needs (spec 13).

    A dependency for the same reason `EmbedderDep` is one: a test has to be
    able to substitute `TestModel`. `Agent.override(model=...)` is no help
    here, because the agents are constructed per turn -- their instructions
    carry that turn's date and block state -- so a test has no instance to
    reach for.

    Resolved per request rather than at import: `get_model` caches, and on
    `LLM_PROVIDER=anthropic` with no key it raises, which the existing
    `LlmUnavailable` handler turns into a 503 before any row is written.
    """
    return ChatModels(router=get_model("router"), agent=get_model("agent"))


ChatModelsDep = Annotated[ChatModels, Depends(get_chat_models)]
