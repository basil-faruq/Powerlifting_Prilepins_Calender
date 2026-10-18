"""FastAPI application entry point.

The error-to-status mapping lives here and nowhere else. The services raise
domain errors (`planner_core.errors`, plus the importer's two and the strength
domain's `WeekdayLocked`) and never an `HTTPException`, because spec 15 calls
the same functions from Temporal activities where there is no request to fail.
This module is the single place that knows those errors are also HTTP.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from api.routes import (
    athletes,
    blocks,
    chat,
    decisions,
    health,
    notes,
    proposals,
    sessions,
    templates,
)
from api.schemas import ProblemOut
from domains.strength.importer import TemplateExistsError, TemplateImportError
from domains.strength.services import WeekdayLocked
from planner_core.config import get_settings
from planner_core.errors import (
    AlreadyDecided,
    BlockNotRunning,
    LlmUnavailable,
    NotFound,
    PlanInvalid,
    ProposalStale,
)
from planner_core.llm import warn_missing_models
from planner_core.tracing import init_tracing, shutdown_tracing

logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Say at startup what will be missing at request time.

    A model that is configured but not pulled fails only the routes that need
    it, and it fails them with a message nobody reads until they are already
    confused. Checking once here turns that into one warning at the top of the
    log, next to the command that fixes it.

    It warns and never raises: every deterministic route -- which is most of
    them -- works with no model present at all, so a missing model is not a
    reason to refuse to start. The check is blocking, so it goes to a thread.

    Tracing is started here too, and flushed after the `yield`. Both calls go
    to a thread for the same reason the probe does: they open sockets.

    The bare `except` is deliberate rather than lazy. `warn_missing_models`
    already handles the expected failure, so anything reaching here is a bug in
    the probe itself -- and a bug in a *diagnostic* must not be what takes the
    API down. It is logged with its traceback, which is the only thing that
    would be lost by swallowing it.
    """
    # Before the model probe, so that probe's own spans -- and every span
    # after it -- have somewhere to go. `init_tracing` never raises and says
    # in the log when it found no keys, so there is nothing to guard here.
    await run_in_threadpool(init_tracing)
    try:
        await run_in_threadpool(warn_missing_models)
    except Exception:  # noqa: BLE001 - a diagnostic must not break startup
        logger.exception("The Ollama model check failed; starting anyway.")
    yield
    # Langfuse batches, so a process stopped with Ctrl-C loses the trace it
    # just made unless the pending spans are flushed here (spec 14 §5A).
    await run_in_threadpool(shutdown_tracing)


app = FastAPI(title="Powerlifting Block Planner", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(athletes.router)
app.include_router(templates.router)
app.include_router(blocks.router)
app.include_router(sessions.router)
app.include_router(proposals.router)
app.include_router(notes.router)
app.include_router(chat.router)
app.include_router(decisions.router)


def _problem(status_code: int, problem: ProblemOut) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=problem.model_dump(mode="json"))


@app.exception_handler(NotFound)
async def _not_found(request: Request, exc: NotFound) -> JSONResponse:
    return _problem(404, ProblemOut(detail=str(exc)))


@app.exception_handler(PlanInvalid)
async def _plan_invalid(request: Request, exc: PlanInvalid) -> JSONResponse:
    """422, body lists the violations (spec 06 §13).

    The warnings travel with them: a plan can be refused *and* carry advice
    worth showing, and the client has nowhere else to read it from.
    """
    return _problem(
        422,
        ProblemOut(
            detail=str(exc) or "the plan was rejected by the validator",
            violations=list(exc.result.violations),
            warnings=list(exc.result.warnings),
            problems=[v.message for v in exc.result.violations],
        ),
    )


@app.exception_handler(ProposalStale)
async def _stale(request: Request, exc: ProposalStale) -> JSONResponse:
    return _problem(409, ProblemOut(detail=str(exc)))


@app.exception_handler(BlockNotRunning)
async def _block_not_running(request: Request, exc: BlockNotRunning) -> JSONResponse:
    return _problem(409, ProblemOut(detail=str(exc)))


@app.exception_handler(AlreadyDecided)
async def _already_decided(request: Request, exc: AlreadyDecided) -> JSONResponse:
    return _problem(409, ProblemOut(detail=str(exc)))


@app.exception_handler(WeekdayLocked)
async def _weekday_locked(request: Request, exc: WeekdayLocked) -> JSONResponse:
    return _problem(409, ProblemOut(detail=str(exc)))


@app.exception_handler(LlmUnavailable)
async def _llm_unavailable(request: Request, exc: LlmUnavailable) -> JSONResponse:
    """503, and the message already names the command that fixes it.

    Covers `OllamaUnreachable` and `ModelNotPulled` together: both mean the
    request was fine and the machine behind it was not, which is the one case
    where retrying the same request unchanged is the right advice.
    """
    return _problem(503, ProblemOut(detail=str(exc)))


@app.exception_handler(TemplateExistsError)
async def _template_exists(request: Request, exc: TemplateExistsError) -> JSONResponse:
    """409, not 400: the workbook is fine, the name is taken.

    Deliberately not a `TemplateImportError` subclass (ADR-0003), so this
    handler has to be registered separately -- and the distinction is the point:
    `?replace=true` is the way past this one and nothing fixes a bad cell.
    """
    return _problem(409, ProblemOut(detail=str(exc)))


@app.exception_handler(TemplateImportError)
async def _template_import(request: Request, exc: TemplateImportError) -> JSONResponse:
    """400 with the cell reference, which the importer's message already carries."""
    return _problem(
        400,
        ProblemOut(detail=str(exc), problems=str(exc).split("\n")),
    )
