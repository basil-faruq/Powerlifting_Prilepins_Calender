"""Shared FastAPI dependencies. Routes stay thin; these are what they lean on."""

import uuid
from typing import Annotated, Protocol

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


# --- Where the slow work runs (spec 15) -----------------------------------


class WorkflowRunner(Protocol):
    """Simulation, shaping and note indexing, wherever they happen to run.

    ADR-0011, ADR-0012 and ADR-0014 each deferred the same decision to spec
    15: this work is slow, it was running on the API's own threadpool, and
    it dies with the process. Moving it into Temporal activities fixes that
    and makes it visible in the workflow UI, which is half the point of the
    demo.

    It is a seam rather than a direct call for the same reason `EmbedderDep`
    is one. The route contracts must not change -- spec 16 generates
    frontend types from them, and the existing route tests assert them -- so
    a test substitutes an implementation that calls the services directly
    and every one of those assertions still means what it meant.
    """

    async def simulate(self, request: object) -> dict: ...

    async def shape_preview(self, request: object) -> dict: ...

    async def index_note(self, request: object) -> None: ...


class TemporalRunner:
    """The real one: each job is a one-activity workflow.

    Needs `make worker` running, which is why CLAUDE.md lists it next to
    `make api` rather than as an optional extra.
    """

    async def simulate(self, request: object) -> dict:
        from domains.strength.workflows.block_workflow import SimulateWorkflow

        return await self._run(SimulateWorkflow.run, request, "simulate")

    async def shape_preview(self, request: object) -> dict:
        from domains.strength.workflows.block_workflow import ShapePreviewWorkflow

        return await self._run(ShapePreviewWorkflow.run, request, "shape")

    async def index_note(self, request: object) -> None:
        from domains.strength.workflows.block_workflow import IndexNoteWorkflow

        await self._run(IndexNoteWorkflow.run, request, "index-note")

    async def _run(self, method, request: object, prefix: str):
        from api import temporal_client

        client = await temporal_client.get_client()
        settings = get_settings()
        try:
            return await client.execute_workflow(
                method,
                request,
                # A fresh id per call: these answer a question rather than
                # own anything, so two of them at once is fine and reusing
                # an id would make the second wait for the first.
                id=f"{prefix}-{uuid.uuid4()}",
                task_queue=settings.temporal_task_queue,
            )
        except Exception as exc:  # noqa: BLE001 - re-raised as itself or as 503
            temporal_client.translate(exc)


def get_runner() -> WorkflowRunner:
    """The runner a route should use. Overridden in tests."""
    return TemporalRunner()


RunnerDep = Annotated[WorkflowRunner, Depends(get_runner)]
