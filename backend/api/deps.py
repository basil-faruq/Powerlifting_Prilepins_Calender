"""Shared FastAPI dependencies. Routes stay thin; these are what they lean on."""

from typing import Annotated

from fastapi import Depends
from sqlmodel import Session

from planner_core.config import Settings, get_settings
from planner_core.db import get_session
from planner_core.llm import Embedder, get_embedder

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
# A dependency rather than a direct `get_embedder()` call so a test can override
# it with a fake and no route reaches Ollama (CLAUDE.md: no real model in CI).
EmbedderDep = Annotated[Embedder, Depends(get_embedder)]
