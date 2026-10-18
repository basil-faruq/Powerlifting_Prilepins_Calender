"""The durable half of the planner: one Temporal workflow per block.

A block runs for eleven weeks, and nothing in a request-response API can wait
that long. `BlockWorkflow` can: it sleeps on a timer until each session's day
has passed, notices one that went unlogged, asks spec 08 what to do about it,
waits for the athlete to approve a repair, and rolls into the next block when
the maxes are retested.

Three modules, and the split between them is the whole design:

* `contracts.py` -- the types the other two speak in. Pure, and importable
  inside Temporal's workflow sandbox.
* `block_workflow.py` -- decisions and waiting. No database, no clock, no
  solver; it must replay identically forever (CLAUDE.md non-negotiable #6).
* `activities.py` -- every side effect, each in its own transaction.

The activities do not reimplement anything. `approve_proposal` is still the
one function that writes a schedule (CLAUDE.md non-negotiable #2); spec 15
only changes where it is called from, which is why it takes a session rather
than opening one.

Importing this package deliberately does **not** import `activities` or
`block_workflow`: the first pulls in CP-SAT and SQLAlchemy, and `worker.py`
and the tests are explicit about which they want.
"""

from domains.strength.workflows import contracts

__all__ = ["contracts"]
