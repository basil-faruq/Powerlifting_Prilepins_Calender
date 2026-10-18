"""The API's end of the workflow. One client, shared, and lazily connected.

Since spec 15, approving a proposal is not a database write the route makes;
it is a message to the execution that owns the block. This module is how the
route sends it, and -- just as importantly -- how the answer comes back
looking exactly as it did when approval was a direct call.

Three things it is responsible for:

**Not changing any status code.** An activity raising `ProposalStale` has to
reach `api/main.py` as a `ProposalStale`, or the single place that knows an
error is also an HTTP status (ADR-0007 rule 2) turns an honest 409 into a
500. `planner_core.temporal_errors` does the rebuilding; this unwraps
Temporal's nesting and calls it.

**Failing fast when nothing is listening.** A worker that is not running
would otherwise leave an update waiting indefinitely, with a route holding
the request open behind it. Every call here is bounded, and the timeout is
`TemporalUnavailable` -- a 503 whose message names the command that fixes it,
the way `LlmUnavailable`'s does.

**The data converter.** `pydantic_data_converter` on both ends, or a
`Decimal` crossing the boundary comes back a float (CLAUDE.md #4).

Connection is lazy and cached: importing this module must not require
Temporal to be up, for the same reason `planner_core.db` builds an engine
without connecting -- `/health` has to be able to report that it is down.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, NoReturn

from temporalio.client import (
    Client,
    WorkflowExecutionStatus,
    WorkflowFailureError,
    WorkflowHandle,
    WorkflowUpdateFailedError,
)
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.service import RPCError

from planner_core import temporal_errors
from planner_core.config import get_settings
from planner_core.errors import TemporalUnavailable

logger = logging.getLogger(__name__)


# How long a route waits on the workflow before calling it unreachable. Long
# enough for an approval -- which re-validates the whole block against the
# current date -- and short enough that a stopped worker is a fast 503 and
# not a hung browser tab. A **starting value**.
CALL_TIMEOUT_S = 20.0

# Connecting is separate and shorter: it is a TCP handshake, not work.
CONNECT_TIMEOUT_S = 5.0

# A query is answered by a worker straight from memory, or not at all. There
# is no slow-but-fine case to wait out, so waiting as long as an approval
# only makes "no worker is running" take twenty seconds to say.
QUERY_TIMEOUT_S = 5.0

_client: Client | None = None
_lock = asyncio.Lock()


async def get_client() -> Client:
    """The shared client, connecting on first use.

    Guarded by a lock rather than connected at import, so that a Temporal
    that is down costs the routes that need it a 503 and costs every other
    route nothing.
    """
    global _client
    if _client is not None:
        return _client
    async with _lock:
        if _client is not None:
            return _client
        settings = get_settings()
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT_S):
                _client = await Client.connect(
                    settings.temporal_address,
                    namespace=settings.temporal_namespace,
                    data_converter=pydantic_data_converter,
                )
        except (RPCError, OSError, TimeoutError) as exc:
            raise TemporalUnavailable(settings.temporal_address, str(exc)) from exc
        return _client


async def close_client() -> None:
    """Let go of the cached client. Called from the API's lifespan.

    Dropping the reference is the whole of it: `ServiceClient` exposes no
    `close`, and its connection is released when it is collected. An earlier
    version called one, which does not exist -- the `AttributeError` came
    out of the lifespan's shutdown and wedged the server instead of
    stopping it, which no test caught because none exercises shutdown with
    a live connection.
    """
    global _client
    _client = None


def reset_client() -> None:
    """Forget the connection without closing it. For tests."""
    global _client
    _client = None


async def ping() -> bool:
    """Is the workflow service actually answering?

    A real client handshake, which is what ADR-0001 said should replace
    `/health`'s raw TCP connect once the SDK was a dependency. A port being
    open proves less than it looks: the dev server binds before it has
    finished setting up its schema.
    """
    try:
        client = await get_client()
        async with asyncio.timeout(CONNECT_TIMEOUT_S):
            await client.service_client.check_health()
    except Exception:  # noqa: BLE001 - a probe reports, it never raises
        return False
    return True


# --- Talking to one block's workflow --------------------------------------


def workflow_id_for(block_id: uuid.UUID, stored: str | None = None) -> str:
    """Which execution owns this block.

    The stored column wins. `continue_as_new` keeps the workflow id, so a
    block reached by rolling over is owned by an execution named after the
    *first* block of its chain -- deriving the id from the block would look
    for one that never existed. The derivation is the fallback for a block
    whose workflow has not been started.
    """
    from domains.strength.workflows import contracts

    return stored or contracts.workflow_id_for(block_id)


async def start_block_workflow(
    block_id: uuid.UUID, *, first_proposal_id: uuid.UUID | None = None
) -> str:
    """Start the execution for a block, or accept that it is already running.

    Idempotent (spec §11): approving twice in quick succession, or a retry
    after a dropped response, must not produce two executions for one block.
    """
    from domains.strength.workflows import contracts
    from domains.strength.workflows.block_workflow import BlockWorkflow

    client = await get_client()
    settings = get_settings()
    workflow_id = contracts.workflow_id_for(block_id)
    try:
        async with asyncio.timeout(CALL_TIMEOUT_S):
            await client.start_workflow(
                BlockWorkflow.run,
                args=[block_id, first_proposal_id],
                id=workflow_id,
                task_queue=settings.temporal_task_queue,
            )
    except Exception as exc:  # noqa: BLE001 - narrowed immediately below
        if _already_started(exc):
            logger.info("workflow %s is already running", workflow_id)
            return workflow_id
        translate(exc)
    return workflow_id


async def signal(workflow_id: str, name: str, *args: Any) -> None:
    handle = await _handle(workflow_id)
    try:
        async with asyncio.timeout(CALL_TIMEOUT_S):
            await handle.signal(name, args=list(args))
    except Exception as exc:  # noqa: BLE001 - narrowed in `translate`
        translate(exc)


async def update[T](workflow_id: str, name: str, *args: Any, result_type: type[T]) -> T:
    """Send an update and wait for its answer.

    `result_type` is not optional in practice: an update named by string has
    no annotation for the data converter to read, so without it the answer
    arrives as a plain dict.
    """
    handle = await _handle(workflow_id)
    try:
        async with asyncio.timeout(CALL_TIMEOUT_S):
            return await handle.execute_update(name, args=list(args), result_type=result_type)
    except Exception as exc:  # noqa: BLE001 - narrowed in `translate`
        translate(exc)


async def query[T](workflow_id: str, name: str, *, result_type: type[T]) -> T:
    handle = await _handle(workflow_id)
    try:
        async with asyncio.timeout(QUERY_TIMEOUT_S):
            return await handle.query(name, result_type=result_type)
    except Exception as exc:  # noqa: BLE001 - narrowed in `translate`
        translate(exc)


async def is_running(workflow_id: str) -> bool:
    """Whether an execution exists and has not finished.

    Used to tell "this block has no workflow yet" from "its workflow is
    unreachable", which are a 200 and a 503 respectively.
    """
    handle = await _handle(workflow_id)
    try:
        async with asyncio.timeout(CALL_TIMEOUT_S):
            description = await handle.describe()
    except Exception as exc:  # noqa: BLE001 - narrowed below
        if _not_found(exc):
            return False
        translate(exc)
    return description.status == WorkflowExecutionStatus.RUNNING


async def _handle(workflow_id: str) -> WorkflowHandle:
    client = await get_client()
    return client.get_workflow_handle(workflow_id)


# --- Turning Temporal's failures back into the planner's ------------------


def translate(exc: BaseException) -> NoReturn:
    """Re-raise as something `api/main.py` already knows how to answer.

    A domain refusal is rebuilt and raised as itself, so the status code is
    the one the caller would have got in-process. Anything that means "the
    workflow service is not there" becomes a 503. A genuine bug is left
    alone, because it should look like a bug.
    """
    if isinstance(exc, WorkflowUpdateFailedError | WorkflowFailureError):
        temporal_errors.reraise_domain(exc)
    if temporal_errors.is_refusal(exc):
        raise exc
    if isinstance(exc, RPCError | OSError | TimeoutError):
        raise TemporalUnavailable(get_settings().temporal_address, str(exc)) from exc
    raise exc


def _already_started(exc: BaseException) -> bool:
    return type(exc).__name__ == "WorkflowAlreadyStartedError"


def _not_found(exc: BaseException) -> bool:
    return isinstance(exc, RPCError) and "not found" in str(exc).lower()
