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

from pydantic import BaseModel, ConfigDict, Field

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
