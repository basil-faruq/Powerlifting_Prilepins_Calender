"""Liveness endpoint: one status per dependency, never a hard failure.

`/health` answers 200 even when everything behind it is down — the per-dependency
fields carry the truth, so a monitoring check can tell "the API is up but Postgres
is gone" from "the API is gone".
"""

import asyncio
from typing import Literal

import httpx
from fastapi import APIRouter
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from api import temporal_client
from planner_core.config import get_settings
from planner_core.db import ping_db

router = APIRouter(tags=["health"])

DependencyStatus = Literal["ok", "down"]

PROBE_TIMEOUT_S = 2.0


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    db: DependencyStatus
    temporal: DependencyStatus
    ollama: DependencyStatus


async def check_db() -> DependencyStatus:
    """`ping_db()` is blocking, so keep it off the event loop — and bound it.

    The engine carries its own `connect_timeout`, but a probe must never be the
    reason a response hangs, so the wait is capped here too. The worker thread
    runs on to completion after a timeout; the engine's timeout is what keeps
    that short.
    """
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_S):
            return "ok" if await run_in_threadpool(ping_db) else "down"
    except TimeoutError:
        return "down"


async def check_temporal() -> DependencyStatus:
    """A real client handshake, which is what ADR-0001 said to replace the
    raw TCP connect with once `temporalio` was a dependency.

    The port being open proved less than it looked: the dev server binds
    before it has finished setting up its schema, so "something is
    listening" and "a workflow can run" are different answers -- and since
    spec 15, approving a proposal depends on the second one.

    It takes no address, unlike its two neighbours. The workflow client is
    shared and cached (one connection for the process, not one per probe),
    so the address it uses is the configured one by construction. Accepting
    an argument it could not honour would be worse than not accepting one.
    """
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_S):
            return "ok" if await temporal_client.ping() else "down"
    except TimeoutError:
        return "down"


async def check_ollama(base_url: str) -> DependencyStatus:
    """Ollama runs on the host; `GET /api/tags` is its cheapest honest probe."""
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
            response = await client.get(f"{base_url.rstrip('/')}/api/tags")
    except httpx.HTTPError:
        return "down"
    return "ok" if response.status_code == 200 else "down"


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    settings = get_settings()
    db, temporal, ollama = await asyncio.gather(
        check_db(),
        check_temporal(),
        check_ollama(settings.ollama_base_url),
    )
    return HealthResponse(status="ok", db=db, temporal=temporal, ollama=ollama)
