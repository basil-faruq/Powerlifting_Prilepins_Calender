"""Non-table Pydantic shapes for the strength domain.

Unlike `models.py`, the `Literal` annotations here **do** validate. ADR-0002's
finding — that a `Literal` on a `table=True` SQLModel class enforces nothing —
is a property of SQLModel's table construction (`sqlmodel_table_construct`
skips `__init__`), not of Pydantic. On a plain `BaseModel`,
`ParsedEntry(lift_code="bananas")` raises a `ValidationError`.

This module is therefore the first real enforcement point in the write path,
and the only one that acts before a row exists. It also binds every *future*
producer of a `ParsedTemplate` — spec 06's upload route, a hand-built test
fixture — not just `importer.py`.

Deliberately free of `openpyxl`: spec 06 imports this module for its response
models, and the parser's own coordinate type (`importer.SheetLayout`) stays in
`importer.py` for that reason.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from fractions import Fraction
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from domains.strength import enums


class ParsedEntry(BaseModel):
    """One template row, as read from one row of one lift sheet."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lift_code: enums.LiftCode
    variation: enums.Variation | None = None
    week: int = Field(ge=1)
    slot: int = Field(ge=1)
    sets: int = Field(gt=0)
    reps: int = Field(gt=0)
    # The pair exactly as authored, unreduced: `=70/120` is stored as 70 and
    # 120, never as 7/12 and never as 0.5833. See importer.parse_intensity for
    # why the denominator is worth preserving (it is not the 1RM).
    pct_num: Decimal = Field(gt=0)
    pct_den: Decimal = Field(gt=0)
    is_test: bool = False

    # --- Provenance -------------------------------------------------------
    # Carried so every downstream message names a place in the workbook, and
    # so scripts/build_golden_fixture.py can join this row against the same
    # row of the data_only workbook to read Excel's cached weight.
    source_sheet: str = Field(min_length=1)
    source_row: int = Field(ge=1)

    @property
    def pct(self) -> Fraction:
        """The intensity as one exact rational.

        Two single-argument `Fraction`s divided — never `Fraction(num, den)`,
        which raises `TypeError: both arguments should be Rational instances`
        on Decimals (ADR-0002 rule 2).

        A property rather than a stored field, so it can never drift from the
        pair, and so every intensity comparison is exact. That matters: equal
        intensities may hold unequal pairs (`120/120` and `1/1`), so comparing
        the `(num, den)` tuples is wrong where comparing `pct` is right.
        """
        return Fraction(self.pct_num) / Fraction(self.pct_den)

    @property
    def key(self) -> tuple[str, int, int]:
        """The key `uq_template_entry_...` enforces, minus `template_id`.

        Note `variation` is not part of it, matching the constraint.
        """
        return (self.lift_code, self.week, self.slot)


class ParsedTemplate(BaseModel):
    """A whole workbook's worth of template, before it reaches the database."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    weeks: int = Field(ge=1)
    slots: int = Field(ge=1)
    overreach_from_week: int = Field(ge=1)
    # The file's basename, never its absolute path: the path is machine-specific
    # and the workbook is private, gitignored data (CLAUDE.md guardrail).
    source_file: str | None = None
    # A tuple, not a list: `frozen=True` blocks attribute assignment but not
    # in-place mutation of a list field, so a list would leave the model only
    # half frozen. Spec 03 §5A says list; this is the one-word deviation.
    entries: tuple[ParsedEntry, ...]

    # There is deliberately no `one_rms` field. The workbook's `1RM` sheet
    # holds example maxes, and a template is a pattern -- sets, reps and
    # intensity -- that any athlete's own 1RM can be applied to. Real maxes
    # reach `one_rep_max` only when the athlete enters them with the date they
    # tested them.


# --- Spec 04: the generated block, before it is persisted -----------------
#
# `DraftLift` mirrors `models.PlannedLift` field for field, and `DraftSession`
# mirrors `models.PlannedSession`. They exist separately because the generator
# is a pure function: it computes a whole block with no database, the validator
# (spec 05) rejects or accepts that draft, and only `generator.persist_block`
# turns it into rows. Collections are tuples for the reason ParsedTemplate's
# are: `frozen=True` does not stop a list being mutated in place.


class DraftLift(BaseModel):
    """One set block inside a drafted session, with its weight computed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lift_code: enums.LiftCode
    variation: enums.Variation | None = None
    sets: int = Field(gt=0)
    reps: int = Field(gt=0)
    # Carried through from the template unchanged and unreduced, so the athlete
    # can be shown the intensity they authored.
    pct_num: Decimal = Field(gt=0)
    pct_den: Decimal = Field(gt=0)
    # FLOOR(1RM x pct, plate_increment_kg) -- the athlete's 1RM, never pct_den
    # (ADR-0003 rule 3).
    weight_kg: Decimal = Field(gt=0)
    # pct / brzycki.rep_max_pct(reps), to 4 dp. Above 1.0 is overreaching,
    # which spec 05 permits only from the template's overreach week.
    rel_intensity: Decimal = Field(gt=0)
    is_test: bool = False

    @property
    def pct(self) -> Fraction:
        """The intensity as one exact rational. See `ParsedEntry.pct`."""
        return Fraction(self.pct_num) / Fraction(self.pct_den)


class DraftSession(BaseModel):
    """One dated training day of a drafted block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    week: int = Field(ge=1)
    slot: int = Field(ge=1)
    # 1..n in date order across the whole block, which is the key
    # `uq_planned_session_block_id_seq` enforces once persisted.
    seq: int = Field(ge=1)
    date: dt.date
    lifts: tuple[DraftLift, ...] = Field(min_length=1)


class BlockDraft(BaseModel):
    """A whole generated block: dated sessions with sets, reps and kilograms.

    Not persisted. `generator.persist_block` is the only writer.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    athlete_id: uuid.UUID
    template_id: uuid.UUID
    # The template's week count, carried so `next_block_one_rms` can define its
    # "last 4 weeks" window without reaching for the template again (and
    # without a clock). Not in spec 04 §5C's field list; see ADR-0005.
    template_weeks: int = Field(ge=1)
    start_date: dt.date
    lifts: tuple[enums.LiftCode, ...] = Field(min_length=1)
    # {lift_code: kg} frozen at generation. The maxes every weight below was
    # computed from, and what a missed test falls back on.
    one_rm_snapshot: dict[str, Decimal]
    sessions: tuple[DraftSession, ...]


class NewOneRm(BaseModel):
    """What a lift's 1RM becomes after a block, and where that number came from.

    `source = None` means **unchanged**: the test was missed or failed, or no
    log beat the old max, so `kg` is the block's snapshot value carried over and
    nothing should be written to `one_rep_max`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lift_code: enums.LiftCode
    # `kg` and `set_on` rather than spec 04's `value_kg`/`tested_on`: these are
    # `models.OneRepMax`'s own column names, and a mismatch between a schema and
    # the table it is written into is a bug waiting to happen.
    kg: Decimal = Field(gt=0)
    source: Literal["tested", "estimated"] | None = None
    # The date the athlete did the lift this number comes from -- their own log
    # date, never a clock reading. None exactly when `source` is None, because a
    # carried-over value was not measured on any new day (CLAUDE.md #9).
    set_on: dt.date | None = None


class MeasuredOneRm(BaseModel):
    """A max the athlete reports, with the day they measured it.

    Spec 06's `PUT /athletes/{id}/one-rep-maxes` writes these. Distinct from
    `NewOneRm`, which is what a max *becomes* after a block and may mean
    "unchanged": this one is always a measurement, so `set_on` is required and
    `source` cannot be absent.

    `synthetic` is not in `source` and there is no `is_synthetic` field. Only
    `scripts/seed_dev_one_rms.py` may write either (ADR-0004 rule 3), so the
    type nothing else can express it is the enforcement.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lift_code: enums.LiftCode
    kg: Decimal = Field(gt=0)
    set_on: dt.date
    source: Literal["tested", "estimated", "imported"] = "tested"


# --- Spec 04: what actually happened, as the generator wants to read it ---
#
# `models.SessionLog.entries` is JSONB documented as
# `[{lift_code, sets_done, reps_done, weight_kg, rpe?}]`. These two shapes are
# that JSON, validated. `next_block_one_rms` needs the *prescription* too (was
# this lift a test? at what weight?), which the log does not carry -- so it
# reads the plan from a `BlockDraft` and the performance from these, and stays
# a pure function. Spec 15's activity maps rows onto them.


class LoggedLift(BaseModel):
    """One lift as performed, from a session log entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lift_code: enums.LiftCode
    # Zero is meaningful: a lift that was attempted and not completed.
    sets_done: int = Field(ge=0)
    # An int when every set hit the same number, a tuple when they differed
    # (spec 20 logs either).
    reps_done: int | tuple[int, ...]
    weight_kg: Decimal = Field(gt=0)
    rpe: Decimal | None = None


class LoggedSession(BaseModel):
    """One session's log, keyed to the planned session by `seq`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    performed_on: dt.date
    entries: tuple[LoggedLift, ...] = ()


# --- Spec 08: a running block, as the policy needs to read it -------------


class BlockState(BaseModel):
    """Everything spec 08 needs to know about a block that is already running.

    A `BlockDraft` describes a block that has not happened yet; this describes
    one partway through, where some sessions are `done`, some were logged and
    some are still ahead. The two stay separate rather than one gaining
    optional fields, because every rule the policy applies turns on that
    difference.

    `plan` is deliberately `DraftSession`, not the rows: the calendar solver,
    the validator and the generator all speak that type already, so the policy
    needs no adapter and a plan read from the database is judged by exactly
    the same code as one that was just generated. `services.load_block_state`
    builds it through `generator.draft_session_of`.

    Nothing here is persisted. It is assembled per call and thrown away.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    block_id: uuid.UUID
    athlete_id: uuid.UUID
    template_id: uuid.UUID
    status: enums.BlockStatus
    # The block's own Monday. The week grid is derived from it rather than from
    # the session dates, which a previous reschedule may already have moved.
    start_date: dt.date
    template_weeks: int = Field(ge=1)
    lifts: tuple[enums.LiftCode, ...] = Field(min_length=1)
    one_rm_snapshot: dict[str, Decimal]
    weekday_map: dict[int, int]
    plan: tuple[DraftSession, ...] = Field(min_length=1)
    status_of: dict[int, enums.PlannedSessionStatus]
    logs: tuple[LoggedSession, ...] = ()

    @model_validator(mode="after")
    def _seqs_line_up(self) -> BlockState:
        """The three seq-keyed collections must describe the same sessions.

        Caught here rather than in the policy because every one of these is a
        loader bug, not a planning decision, and a policy that silently plans
        around a missing session is worse than one that refuses to start.
        """
        seqs = [block_session.seq for block_session in self.plan]
        if len(set(seqs)) != len(seqs):
            raise ValueError("two sessions share a seq")
        if seqs != sorted(seqs):
            raise ValueError("plan is not in seq order")
        if set(self.status_of) != set(seqs):
            raise ValueError("status_of does not cover exactly the planned seqs")
        unknown = sorted({log.seq for log in self.logs} - set(seqs))
        if unknown:
            raise ValueError(f"logs name sessions not in the plan: {unknown}")
        return self

    @property
    def by_seq(self) -> dict[int, DraftSession]:
        return {block_session.seq: block_session for block_session in self.plan}

    @property
    def history_seqs(self) -> frozenset[int]:
        """Seqs a proposal may not re-date, which the solver pins.

        `missed` is terminal: a session the athlete has written off stays where
        it was, as a record that the week went that way. A session they *intend*
        to make up is still `planned` with a date in the past, and so is not
        here -- which is exactly what lets the solver re-place it.
        """
        return frozenset(
            seq for seq, status in self.status_of.items() if status in enums.HISTORY_STATUSES
        )

    def week_end(self, week: int) -> dt.date:
        """The Sunday that closes a training week, counted from `start_date`."""
        return self.start_date + dt.timedelta(days=7 * week - 1)


# --- Spec 05: what a proposal about this domain carries -------------------
#
# `planner_core.Proposal` is generic in its payload; `StrengthPayload` is this
# domain's. It travels into `proposal.payload` (JSONB), so every field has to
# survive a JSON round-trip -- which is why the weights are `Decimal` and
# serialise as strings, never as floats.


class SessionChange(BaseModel):
    """One requested change to one session.

    Note the vocabulary is deliberately *not* `enums.PlannedSessionStatus`:
    that describes what a session currently is, this describes what is being
    asked of it. `repeat_week` is a change to the week this session belongs to
    and carries no dates of its own -- the resulting dates are in `plan`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    week: int = Field(ge=1)
    slot: int = Field(ge=1)
    action: Literal["create", "move", "skip", "repeat_week", "fold"]
    from_date: dt.date | None = None
    to_date: dt.date | None = None
    # `fold` only: the seq that takes this session's lifts on. The folded
    # session is itself skipped, so the pair is read together -- see
    # `missed_policy` for why folding needs a target at all.
    into_seq: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _dates_match_the_action(self) -> SessionChange:
        """A change that places a session must say where.

        The validator's `past_date` rule reads `to_date`, so a `move` with none
        would silently pass the one check that protects against scheduling into
        the past.
        """
        if self.action in {"create", "move"} and self.to_date is None:
            raise ValueError(f"a {self.action} needs a to_date")
        if self.action == "move" and self.from_date is None:
            raise ValueError("a move needs a from_date")
        if self.action == "fold" and self.into_seq is None:
            raise ValueError("a fold needs an into_seq")
        if self.action != "fold" and self.into_seq is not None:
            raise ValueError(f"a {self.action} may not carry an into_seq")
        if self.action == "fold" and self.into_seq == self.seq:
            raise ValueError("a session cannot be folded into itself")
        return self


class StrengthPayload(BaseModel):
    """A proposed change to a block: the diff, and the whole resulting schedule.

    `plan` is the **full** resulting schedule rather than just the sessions that
    moved (spec 05 §5B), so the validator always judges the whole picture. A
    diff cannot show that moving week 4's squat put it a day after week 3's.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    changes: tuple[SessionChange, ...] = ()
    plan: tuple[DraftSession, ...] = ()
    # Set only by a rollover proposal (spec 15's `finish_block`), where the
    # maxes the next block is computed from are themselves part of the change.
    new_one_rms: dict[str, Decimal] | None = None
    # Set only by spec 08's `restart`, which proposes a *new* block to replace
    # one that is still running. Naming the block explicitly beats "abandon
    # whatever else is active": a restart proposed on Monday and approved on
    # Friday must retire the block it was actually computed against, not
    # whichever one happens to be running by then.
    supersedes_block_id: uuid.UUID | None = None


class WeekdayChangePayload(StrengthPayload):
    """A `weekday_change` proposal: new training days, and the plan they imply.

    `ProposalKind` has carried `weekday_change` since spec 05, but no spec gave
    it a payload -- spec 06 §11 only says a weekday change while a block is
    running must go through one. It needs exactly one field more than a
    reschedule: the map to install, because the sessions alone cannot say which
    weekday Day 2 now *means* once they have all moved.

    Inherits the plan and the diff, so `validate` and `_apply_plan` treat it
    like any other change to the schedule.
    """

    # {slot: weekday}, 0 = Monday. Replaces the athlete's map wholesale rather
    # than patching it: a partial map cannot express swapping two days.
    slots: dict[int, int]

    @model_validator(mode="after")
    def _slots_are_distinct_weekdays(self) -> WeekdayChangePayload:
        """Mirrors `uq_weekday_map_athlete_id_weekday`, before the database does.

        An IntegrityError would abort the whole approving transaction (ADR-0002
        rule 1), taking the re-dated schedule with it.
        """
        for weekday in self.slots.values():
            if not 0 <= weekday <= 6:
                raise ValueError(f"weekday {weekday} is not in 0-6 (0 = Monday)")
        if len(set(self.slots.values())) != len(self.slots):
            raise ValueError("two slots share a weekday")
        return self


class NoteHit(BaseModel):
    """One retrieved training note, with how far it sat from the query.

    `distance` is cosine distance from pgvector: 0 is identical, 1 is
    unrelated. Returned rather than a similarity score because it is the number
    the database actually produced, and the explainer may only repeat numbers it
    was given (CLAUDE.md non-negotiable 1). `pain_flags` reports 0.0, since a
    keyword match has no distance -- it did not ask a vector anything.

    `is_synthetic` travels with every hit. ADR-0004 makes the flag the
    authoritative "is this real?" and requires every consumer to filter on it;
    carrying it means a caller that opted *into* synthetic notes can still tell
    the seeded ones apart, and the UI can say so.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    noted_on: dt.date
    lift_codes: tuple[str, ...] = ()
    distance: float
    is_synthetic: bool = False
