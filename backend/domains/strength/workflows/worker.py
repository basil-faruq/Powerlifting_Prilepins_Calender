"""The process that actually runs block workflows. `make worker`.

Without this nothing advances: the API can start a workflow and signal it,
but a workflow only makes progress while a worker is polling its task queue.
That is the one thing about the deployment story worth saying out loud --
`make api` on its own gives you a planner that accepts approvals and then
sits there.

Two details that are easy to get wrong and expensive to debug:

**The data converter.** Both this and `api/temporal_client.py` must use
`pydantic_data_converter`. Without it a `Decimal` crossing the activity
boundary comes back a float, and CLAUDE.md non-negotiable #4 -- exact
arithmetic end to end -- is broken somewhere nobody is looking.

**Tracing has its own lifecycle here.** `init_tracing` and `shutdown_tracing`
were only ever called from the FastAPI lifespan. Langfuse batches, so a
worker stopped with Ctrl-C loses its last traces without the shutdown.

Activities are sync functions on a thread pool: `services` is synchronous
throughout, and CP-SAT releases the GIL while it solves, so threads are the
right shape. A process pool would need `init_tracing` in every child for no
benefit.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from domains.strength.workflows.activities import StrengthActivities
from domains.strength.workflows.block_workflow import (
    BlockWorkflow,
    IndexNoteWorkflow,
    ShapePreviewWorkflow,
    SimulateWorkflow,
)
from planner_core.config import get_settings
from planner_core.llm import get_embedder
from planner_core.tracing import init_tracing, shutdown_tracing

logger = logging.getLogger(__name__)

# Enough for one athlete's block and the occasional reschedule. Sized small
# deliberately: every thread holds a database connection while it runs, and
# this is a single-athlete planner (CLAUDE.md), not a service.
MAX_CONCURRENT_ACTIVITIES = 8


async def run_worker() -> None:
    settings = get_settings()
    client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        data_converter=pydantic_data_converter,
    )
    # The embedder has to be handed in, or `index_note` reports "no embedder
    # configured" and returns successfully having written nothing -- so a note
    # logged at the gym never reaches the retrieval index and the failure looks
    # like a completed workflow. Only the tests were passing one, which is why
    # the suite could not see it.
    activities = StrengthActivities(embedder_factory=get_embedder)

    with ThreadPoolExecutor(
        max_workers=MAX_CONCURRENT_ACTIVITIES, thread_name_prefix="activity"
    ) as pool:
        worker = Worker(
            client,
            task_queue=settings.temporal_task_queue,
            # Every workflow the API can start, not just the long-lived one.
            # The three short ones were registered nowhere, so `runner.index_note`,
            # simulate and shape-preview each started a workflow this worker then
            # refused ("not registered on this worker") and retried for ever --
            # and because the caller awaits a result that never comes, the request
            # HUNG rather than failing. Found logging a session: the log committed
            # and the response never arrived.
            workflows=[BlockWorkflow, IndexNoteWorkflow, ShapePreviewWorkflow, SimulateWorkflow],
            activities=activities.all(),
            activity_executor=pool,
            max_concurrent_activities=MAX_CONCURRENT_ACTIVITIES,
        )
        logger.info(
            "worker polling %s on %s", settings.temporal_task_queue, settings.temporal_address
        )
        await worker.run()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    )
    init_tracing()
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        logger.info("worker stopped")
    finally:
        # Langfuse batches; without this the last traces never leave.
        shutdown_tracing()


if __name__ == "__main__":
    main()
