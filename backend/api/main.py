"""FastAPI application entry point.

The error-to-status mapping lives here and nowhere else. The services raise
domain errors (`planner_core.errors`, plus the importer's two and the strength
domain's `WeekdayLocked`) and never an `HTTPException`, because spec 15 calls
the same functions from Temporal activities where there is no request to fail.
This module is the single place that knows those errors are also HTTP.
"""

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.routes import athletes, blocks, health, proposals, sessions, templates
from api.schemas import ProblemOut
from domains.strength.importer import TemplateExistsError, TemplateImportError
from domains.strength.services import WeekdayLocked
from planner_core.config import get_settings
from planner_core.errors import (
    AlreadyDecided,
    BlockNotRunning,
    NotFound,
    PlanInvalid,
    ProposalStale,
)

settings = get_settings()

app = FastAPI(title="Powerlifting Block Planner", version="0.1.0")

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
