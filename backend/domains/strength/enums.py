"""Closed value sets for the strength domain's text columns.

Every enum-like column in `models.py` is a `TEXT` column constrained three ways:

1. a `Literal` alias here, which types the field and shapes the OpenAPI schema;
2. a database `CHECK` built from `get_args` of that same alias;
3. the Pydantic request schemas of spec 06, which reuse these aliases.

(2) is not redundant. SQLModel `table=True` classes bypass Pydantic validation
entirely — `sqlmodel_table_construct` skips `__init__` and `validate_assignment`
is off — so `Block(status="banana")` constructs without complaint. Until spec 06
adds request schemas, the database CHECK is the *only* enforcement in the system.

Deriving the tuples from the `Literal`s with `get_args` keeps one source of
truth, and `tests/test_models.py` asserts every CHECK body still matches its
alias, so adding a value to one and not the other fails the suite.

Note: no `from __future__ import annotations` in this module. `get_args` needs
the real `Literal` objects, not stringified annotations.
"""

from typing import Literal, get_args

from planner_core import proposal as _proposal

# --- Lifts ----------------------------------------------------------------
# `row` is the only accessory and the only lift that is never tested
# (CONTEXT.md §4).
LiftCode = Literal["squat", "bench", "deadlift", "press", "row"]

# Bench is the only lift with variations. `Wide Grp` in the source spreadsheet
# is a typo for `Wide Grip`, normalised on import (spec 03).
Variation = Literal["close_grip", "wide_grip"]

# --- Athlete --------------------------------------------------------------
# `floor` matches the authored spreadsheet's 1 kg floor. Whether a real gym's
# 2.5 kg plate reality should win is an open question (CONTEXT.md §9), so this
# default is "matches the source material", not "derived".
Rounding = Literal["floor", "nearest"]

# --- One-rep maxes --------------------------------------------------------
# Where a max came from. `tested` is a completed test single (spec 06's
# log_session), `estimated` a missed test falling back on the block snapshot
# (spec 04), `imported` a prior record the athlete entered by hand.
#
# `synthetic` is written by `scripts/seed_dev_one_rms.py` and by nothing else.
# It exists because the planner needs a max to do anything at all, and the
# spreadsheet's maxes are examples (ADR-0003), so local testing would otherwise
# have no input. A synthetic row is always also flagged `is_synthetic`, which a
# CHECK enforces — so `is_synthetic` is the authoritative "is this real?" and
# `source` only narrows which producer wrote it. **Filter on `is_synthetic`.**
#
# Note the asymmetry: a row may be `is_synthetic` with any source, so a dev can
# fake a `tested` max to exercise spec 06's test-completion path. The reverse
# cannot happen: `source = 'synthetic'` forces the flag.
OneRepMaxSource = Literal["tested", "estimated", "imported", "synthetic"]

# --- Blocks and sessions --------------------------------------------------
BlockStatus = Literal["draft", "approved", "active", "paused", "done", "abandoned"]

# Distinct vocabulary from `SessionChange.action` in spec 05
# (create/move/skip/repeat_week). That describes a *requested change*; this
# describes a session's *current state*. Do not collapse them.
PlannedSessionStatus = Literal["planned", "done", "missed", "moved", "skipped"]

# The statuses that put a session's date beyond further argument: it was
# trained, or it was written off. A proposal may not re-date these, the
# calendar solver pins them, and spec 08's policy reads them as history.
#
# Note what is *not* here. A session the athlete intends to make up stays
# `planned` with a date in the past until a reschedule is approved -- that is
# what lets the solver re-place it. `missed` means written off, not "did not
# happen yet" (spec 08).
HISTORY_STATUSES: tuple[str, ...] = ("done", "missed")

# --- Proposals ------------------------------------------------------------
# Re-exported from `planner_core.proposal`, not defined here: `Proposal` is
# generic and domain-agnostic, so it is typed by these literals and cannot
# import them from a domain. Keeping one definition means `models.Proposal`'s
# CHECK constraints and the engine's own types cannot drift (spec 05).
ProposalKind = _proposal.ProposalKind
ProposalStatus = _proposal.ProposalStatus
ProposalSource = _proposal.ProposalSource

# --- Decision audit trail -------------------------------------------------
# `safety` and `clarify` are the deterministic hard filters' outcomes; the other
# four are the routes the LLM selector may choose among (CLAUDE.md
# non-negotiable #3).
DecisionRoute = Literal["answer", "plan", "reschedule", "simulate", "safety", "clarify"]
DecisionOutcome = Literal["approved", "rejected", "answered", "failed"]

ChatRole = Literal["user", "assistant"]


# --- Derived tuples, for CHECK constraints and validation -----------------
LIFT_CODES: tuple[str, ...] = get_args(LiftCode)
VARIATIONS: tuple[str, ...] = get_args(Variation)
ROUNDINGS: tuple[str, ...] = get_args(Rounding)
ONE_REP_MAX_SOURCES: tuple[str, ...] = get_args(OneRepMaxSource)
BLOCK_STATUSES: tuple[str, ...] = get_args(BlockStatus)
PLANNED_SESSION_STATUSES: tuple[str, ...] = get_args(PlannedSessionStatus)
PROPOSAL_KINDS: tuple[str, ...] = get_args(ProposalKind)
PROPOSAL_STATUSES: tuple[str, ...] = get_args(ProposalStatus)
PROPOSAL_SOURCES: tuple[str, ...] = get_args(ProposalSource)
DECISION_ROUTES: tuple[str, ...] = get_args(DecisionRoute)
DECISION_OUTCOMES: tuple[str, ...] = get_args(DecisionOutcome)
CHAT_ROLES: tuple[str, ...] = get_args(ChatRole)

# --- Fixed values from spec 02 §10 ---------------------------------------
# The template is 11 weeks x 3 slots, overreaching from week 7. These are the
# authored block's shape, stored per template in `block_template` so a future
# template can differ; these constants are the defaults the importer writes.
TEMPLATE_WEEKS = 11
TEMPLATE_SLOTS = 3
OVERREACH_FROM_WEEK = 7
