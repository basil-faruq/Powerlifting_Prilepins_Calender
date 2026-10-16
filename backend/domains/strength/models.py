"""SQLModel tables for the strength domain.

The schema arrives in the database through Alembic migrations only; nothing here
is ever created with `create_all()` (CLAUDE.md non-negotiable #7). These classes
are the ORM mapping and the source the migrations were written against, and
`tests/test_models.py` asserts the two have not drifted.

Three conventions worth knowing before editing:

*   **`import sqlalchemy as sa`, never `from sqlalchemy import text`.** `NoteChunk`
    has a column named `text` and `PlannedSession` one named `date`; a bare
    `text: str = Field(...)` rebinds that name for the rest of the class body, so
    a later `text("...")` would silently call a string. Same reason for
    `import datetime as dt`.
*   **Enum-like columns need `sa_type=sa.Text`.** A bare `Literal[...]`
    annotation on a `table=True` field raises `TypeError: issubclass() arg 1 must
    be a class` at class-definition time: SQLModel's type inference hands the
    `Literal` special form to `issubclass`. `sa_type` short-circuits inference
    before that call.
*   **Percentages are `NUMERIC`, not integers.** See `TemplateEntry.pct_num`.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from domains.strength import enums
from planner_core.config import get_settings

# --- Constraint naming ----------------------------------------------------
# Set before any model class is defined, so every constraint and index gets a
# deterministic name. Without this, SQLAlchemy leaves CHECK constraints unnamed,
# which makes them impossible to drop in a later migration and impossible to
# assert on in a test.
#
# Pass the *short* name to a constraint (`name="kg_positive"`); the convention
# adds the `ck_<table>_` prefix. Passing the full name double-prefixes it.
SQLModel.metadata.naming_convention = {
    "pk": "pk_%(table_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
}


_SAFE_VALUE = re.compile(r"\A[a-z][a-z_]*\Z")


def _one_of(column: str, allowed: tuple[str, ...], *, nullable: bool = False) -> str:
    """Build a CHECK body restricting `column` to a closed set of identifiers.

    This formats SQL from Python values, which CLAUDE.md's conventions otherwise
    forbid. The exception is narrow and deliberate: `allowed` always comes from
    `get_args` of a `Literal` declared in `domains.strength.enums`, it is
    evaluated once at import time, it never touches runtime data, and the regex
    assertion below makes a quote or semicolon impossible. The rule exists to
    stop injection from user input, of which there is none on this path.

    The alternative — writing a dozen CHECK bodies out by hand — loses the
    single source of truth, which is the worse failure mode.

    A nullable column needs the `IS NULL OR` form: under three-valued logic a
    bare `col IN (...)` evaluates to NULL for a NULL column, which passes.
    """
    if not allowed:
        raise ValueError(f"{column}: empty value set")
    for value in allowed:
        if not _SAFE_VALUE.fullmatch(value):
            raise ValueError(f"{column}: unsafe enum value {value!r}")
    quoted = ", ".join(f"'{value}'" for value in allowed)
    body = f"{column} IN ({quoted})"
    return f"{column} IS NULL OR {body}" if nullable else body


class TimestampMixin(SQLModel):
    """`created_at TIMESTAMPTZ DEFAULT now()` on every table (spec 02 §4).

    `sa_column_kwargs`, not `sa_column`: a `Column` object cannot be shared
    between mappers, so putting one on a mixin raises as soon as a second class
    inherits it.

    SQLModel maps `datetime` to `DateTime(timezone=True)` and rejects naive
    datetimes on bind, hence the explicit UTC in the default factory.
    """

    created_at: dt.datetime = Field(
        default_factory=lambda: dt.datetime.now(dt.UTC),
        sa_column_kwargs={"server_default": sa.func.now()},
        nullable=False,
    )


# --- A. athlete -----------------------------------------------------------
class Athlete(TimestampMixin, table=True):
    __tablename__ = "athlete"
    __table_args__ = (
        sa.CheckConstraint("plate_increment_kg > 0", name="plate_increment_positive"),
        sa.CheckConstraint(_one_of("rounding", enums.ROUNDINGS), name="rounding_known"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(sa_type=sa.Text, nullable=False)
    # 5.00 is what a loaded bar can actually take: plates go on in pairs, and
    # the gym's smallest is 2.5 kg, so the total moves in 5 kg steps (migration
    # 0005 closes CONTEXT.md §9). The workbook's own 1.00 / 'floor' is still a
    # per-athlete setting -- the golden test uses it -- just no longer default.
    plate_increment_kg: Decimal = Field(
        default=Decimal("5.00"),
        max_digits=4,
        decimal_places=2,
        sa_column_kwargs={"server_default": sa.text("5.00")},
        nullable=False,
    )
    # 'nearest' because a 5 kg step is coarse enough that always flooring
    # drifts noticeably under the percentage the template prescribed.
    rounding: enums.Rounding = Field(
        default="nearest",
        sa_type=sa.Text,
        sa_column_kwargs={"server_default": sa.text("'nearest'")},
        nullable=False,
    )
    timezone: str = Field(
        default_factory=lambda: get_settings().timezone,
        sa_type=sa.Text,
        sa_column_kwargs={"server_default": sa.text("'Europe/Berlin'")},
        nullable=False,
    )


# --- B. lift --------------------------------------------------------------
class Lift(TimestampMixin, table=True):
    """Seeded by migration `0002_seed_lifts`, never written by the application.

    No CHECK on `code`: every `lift_code` elsewhere is a foreign key into this
    table, so the five seeded rows already are the closed set. A CHECK would
    only prevent inserting a sixth lift — pure future churn for no enforcement
    gain. `enums.LiftCode` documents the set.
    """

    __tablename__ = "lift"

    code: str = Field(sa_type=sa.Text, primary_key=True)
    name: str = Field(sa_type=sa.Text, nullable=False)
    is_accessory: bool = Field(
        default=False,
        sa_column_kwargs={"server_default": sa.false()},
        nullable=False,
    )
    variations: list[str] = Field(
        default_factory=list,
        sa_type=JSONB,
        sa_column_kwargs={"server_default": sa.text("'[]'::jsonb")},
        nullable=False,
    )


# --- D. block_template ----------------------------------------------------
class BlockTemplate(TimestampMixin, table=True):
    __tablename__ = "block_template"
    __table_args__ = (
        sa.CheckConstraint("weeks > 0", name="weeks_positive"),
        sa.CheckConstraint("slots > 0", name="slots_positive"),
        # Same-row comparison, so this one *is* expressible as a CHECK.
        sa.CheckConstraint(
            "overreach_from_week >= 1 AND overreach_from_week <= weeks",
            name="overreach_within_block",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Unique: spec 03's importer raises TemplateExistsError off this constraint
    # unless called with replace=True.
    name: str = Field(sa_type=sa.Text, unique=True, nullable=False)
    weeks: int = Field(default=enums.TEMPLATE_WEEKS, nullable=False)
    slots: int = Field(default=enums.TEMPLATE_SLOTS, nullable=False)
    overreach_from_week: int = Field(
        default=enums.OVERREACH_FROM_WEEK,
        sa_column_kwargs={"server_default": sa.text("7")},
        nullable=False,
    )
    source_file: str | None = Field(default=None, sa_type=sa.Text, nullable=True)


# --- F. weekday_map -------------------------------------------------------
class WeekdayMap(TimestampMixin, table=True):
    """Slot -> weekday, fixed once per athlete.

    Composite primary key `(athlete_id, slot)` with no surrogate `id`, per
    spec 02 §4F. That PK also serves the `athlete_id` foreign key's index.
    """

    __tablename__ = "weekday_map"
    __table_args__ = (
        # Two slots cannot share a weekday. Spec 04's check_weekday_map relies
        # on this.
        # No explicit name: unlike the "ck" template, the "uq" naming
        # convention has no %(constraint_name)s placeholder, so passing a name
        # overrides the convention instead of composing with it and the uq_
        # prefix is lost.
        sa.UniqueConstraint("athlete_id", "weekday"),
        sa.CheckConstraint("slot BETWEEN 1 AND 3", name="slot_in_range"),
        sa.CheckConstraint("weekday BETWEEN 0 AND 6", name="weekday_in_range"),
    )

    athlete_id: uuid.UUID = Field(foreign_key="athlete.id", primary_key=True)
    slot: int = Field(primary_key=True)
    # 0 = Monday ... 6 = Sunday, matching Python's date.weekday() and CLAUDE.md.
    #
    # Beware: this is a THIRD numbering alongside PostgreSQL's two. PG's
    # EXTRACT(DOW) is 0 = Sunday and EXTRACT(ISODOW) is 1 = Monday. Comparing
    # this column against either in SQL without converting is off by a day.
    weekday: int = Field(nullable=False)
    locked_from: dt.date | None = Field(default=None, nullable=True)


# --- G. block -------------------------------------------------------------
class Block(TimestampMixin, table=True):
    __tablename__ = "block"
    __table_args__ = (
        # §4G says blocks are numbered "1, 2, 3... per athlete", which only
        # means something if enforced. Also serves the athlete_id FK index.
        sa.UniqueConstraint("athlete_id", "number"),
        sa.CheckConstraint("number >= 1", name="number_positive"),
        # ISODOW over DOW purely for legibility: both give 1 for Monday, but
        # ISODOW's Monday=1..Sunday=7 cannot be misread the way DOW's Sunday=0
        # can. Legal in a CHECK — only index expressions and generated columns
        # require immutability, and date_part on a date is immutable anyway.
        #
        # This is the backstop, not the user-facing guard: an IntegrityError is
        # a poor error message, so spec 04's generator must never propose a
        # non-Monday start in the first place.
        sa.CheckConstraint("EXTRACT(ISODOW FROM start_date) = 1", name="start_date_is_monday"),
        sa.CheckConstraint(_one_of("status", enums.BLOCK_STATUSES), name="status_known"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    athlete_id: uuid.UUID = Field(foreign_key="athlete.id", nullable=False)
    template_id: uuid.UUID = Field(foreign_key="block_template.id", index=True, nullable=False)
    number: int = Field(nullable=False)
    start_date: dt.date = Field(nullable=False)
    status: enums.BlockStatus = Field(
        default="draft",
        sa_type=sa.Text,
        sa_column_kwargs={"server_default": sa.text("'draft'")},
        nullable=False,
    )
    # The chosen lift codes for this block — a subset, so a block is not always
    # 33 sessions (spec 04 allows e.g. deadlift-only, giving 11).
    #
    # NOT NULL with no server default: a block with no lifts is a bug, and
    # forcing the writer to supply it surfaces that at the insert rather than
    # hiding it as an empty list.
    lifts: list[str] = Field(default_factory=list, sa_type=JSONB, nullable=False)
    # {lift_code: kg} frozen at block start. Stored rather than derived because
    # spec 04 falls back on it when a test session is missed.
    one_rm_snapshot: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB, nullable=False)
    workflow_id: str | None = Field(default=None, sa_type=sa.Text, nullable=True)


# --- C. one_rep_max -------------------------------------------------------
# Declared after Block: it carries a nullable FK to block.id.
class OneRepMax(TimestampMixin, table=True):
    """The current 1RM for a lift is the newest row per `(athlete_id, lift_code)`.

    No unique constraint on `(athlete_id, lift_code, set_on)`: the spec lists
    none, and two maxes for one lift on one day is not obviously wrong.
    `scripts/seed_athlete.py` achieves idempotency by selecting first.
    """

    __tablename__ = "one_rep_max"
    __table_args__ = (
        # The "newest row per lift" access path. DESC matters: this is the
        # ordering every current-1RM lookup uses.
        sa.Index(
            "ix_one_rep_max_athlete_lift_set_on",
            "athlete_id",
            "lift_code",
            sa.text("set_on DESC"),
        ),
        sa.CheckConstraint("kg > 0", name="kg_positive"),
        sa.CheckConstraint(_one_of("source", enums.ONE_REP_MAX_SOURCES), name="source_known"),
        # Two columns answer "is this real?", so make them unable to disagree.
        # Same-row, therefore expressible as a CHECK (ADR-0002 rule 3).
        #
        # An implication, not a biconditional: `source = 'synthetic'` forces
        # the flag, but a flagged row may carry any source. That asymmetry is
        # deliberate — a dev needs to fake a `tested` max to exercise spec 06's
        # test-completion path, and forbidding it would leave that path
        # untestable without real data.
        sa.CheckConstraint(
            "source <> 'synthetic' OR is_synthetic",
            name="synthetic_source_is_flagged",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # athlete_id's FK index is the composite above, which it leads.
    athlete_id: uuid.UUID = Field(foreign_key="athlete.id", nullable=False)
    # sa_type is needed even though this is a plain str: without it SQLModel
    # infers AutoString, which renders VARCHAR rather than the TEXT the spec
    # asks for (and which lift.code itself uses).
    lift_code: str = Field(sa_type=sa.Text, foreign_key="lift.code", index=True, nullable=False)
    kg: Decimal = Field(max_digits=6, decimal_places=2, nullable=False)
    set_on: dt.date = Field(nullable=False)
    source: enums.OneRepMaxSource = Field(sa_type=sa.Text, nullable=False)
    # The authoritative "is this a real measurement?". Mirrors
    # note_chunk.is_synthetic, so the two kinds of seeded demo data are asked
    # about the same way, and spec 12's `include_synthetic` filter pattern
    # generalises here.
    #
    # Defaults to False and is NOT NULL, so a row is real unless it says
    # otherwise — the safe direction. Every consumer that must not train an
    # athlete on invented numbers filters on this column, and spec 17/19 must
    # surface a banner while any row has it set (ADR-0004).
    is_synthetic: bool = Field(
        default=False,
        sa_column_kwargs={"server_default": sa.false()},
        nullable=False,
    )
    block_id: uuid.UUID | None = Field(
        default=None, foreign_key="block.id", index=True, nullable=True
    )


# --- E. template_entry ----------------------------------------------------
class TemplateEntry(TimestampMixin, table=True):
    """One row of the authored template: lift x week x slot.

    77 rows for block B1 (CONTEXT.md §4).
    """

    __tablename__ = "template_entry"
    __table_args__ = (
        # Note `variation` is deliberately NOT part of the key, so one lift
        # cannot hold two variations in the same week+slot. B1 is safe: bench is
        # slot 1 close-grip and slot 3 wide-grip, distinct on slot.
        sa.UniqueConstraint("template_id", "lift_code", "week", "slot"),
        # Only the lower bounds are enforceable here. The upper bounds live in
        # block_template.weeks / .slots — a different row in a different table —
        # so "week <= weeks" is not expressible as a CHECK and becomes a hard
        # validator rule in specs 04/05 instead.
        sa.CheckConstraint("week >= 1", name="week_positive"),
        sa.CheckConstraint("slot >= 1", name="slot_positive"),
        sa.CheckConstraint("sets > 0", name="sets_positive"),
        sa.CheckConstraint("reps > 0", name="reps_positive"),
        sa.CheckConstraint("pct_num > 0", name="pct_num_positive"),
        sa.CheckConstraint("pct_den > 0", name="pct_den_positive"),
        sa.CheckConstraint(
            _one_of("variation", enums.VARIATIONS, nullable=True), name="variation_known"
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # template_id's FK index is the unique constraint above, which it leads.
    # CASCADE is what makes spec 03's importer replace=True cheap.
    template_id: uuid.UUID = Field(
        foreign_key="block_template.id", ondelete="CASCADE", nullable=False
    )
    lift_code: str = Field(sa_type=sa.Text, foreign_key="lift.code", index=True, nullable=False)
    variation: str | None = Field(default=None, sa_type=sa.Text, nullable=True)
    week: int = Field(nullable=False)
    slot: int = Field(nullable=False)
    sets: int = Field(nullable=False)
    reps: int = Field(nullable=False)
    # NUMERIC, not INTEGER, and this is load-bearing. CLAUDE.md
    # non-negotiable #4 says intensities are stored "as numerator/denominator
    # pairs", which reads as integers — but the authored template contains
    # 112.5/105 (bench test) and 72.5/70 (press test), and Squat W11 S3 is a
    # bare 0.5 (CONTEXT.md §4). Exactness is preserved regardless, because a
    # Decimal converts to a Fraction losslessly:
    #   Fraction(Decimal("112.5")) / Fraction(Decimal("105")) == Fraction(15, 14)
    # Note the single-argument form and the division. Fraction's *two*-argument
    # constructor rejects Decimals outright —
    # `Fraction(Decimal("112.5"), Decimal("105"))` raises
    # `TypeError: both arguments should be Rational instances` — so spec 04 must
    # divide two Fractions rather than pass a numerator and denominator.
    # Do not "fix" these to INTEGER; it would break the 77-weight golden test.
    pct_num: Decimal = Field(max_digits=10, decimal_places=4, nullable=False)
    pct_den: Decimal = Field(max_digits=10, decimal_places=4, nullable=False)
    # True for exactly 4 of B1's 77 entries: sets == 1 and reps == 1 and pct > 1.
    is_test: bool = Field(
        default=False,
        sa_column_kwargs={"server_default": sa.false()},
        nullable=False,
    )


# --- H. planned_session ---------------------------------------------------
class PlannedSession(TimestampMixin, table=True):
    __tablename__ = "planned_session"
    __table_args__ = (
        sa.UniqueConstraint("block_id", "seq"),
        sa.CheckConstraint("week >= 1", name="week_positive"),
        sa.CheckConstraint("slot >= 1", name="slot_positive"),
        sa.CheckConstraint("seq >= 1", name="seq_positive"),
        sa.CheckConstraint(_one_of("status", enums.PLANNED_SESSION_STATUSES), name="status_known"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # block_id's FK index is the unique constraint above, which it leads.
    block_id: uuid.UUID = Field(foreign_key="block.id", ondelete="CASCADE", nullable=False)
    week: int = Field(nullable=False)
    slot: int = Field(nullable=False)
    seq: int = Field(nullable=False)
    date: dt.date = Field(nullable=False)
    # The date this session was *first* planned for, so a reschedule stays
    # visible. Spec 04's persist_block sets it equal to `date` on creation.
    original_date: dt.date = Field(nullable=False)
    status: enums.PlannedSessionStatus = Field(
        default="planned",
        sa_type=sa.Text,
        sa_column_kwargs={"server_default": sa.text("'planned'")},
        nullable=False,
    )


# --- I. planned_lift ------------------------------------------------------
class PlannedLift(TimestampMixin, table=True):
    """A set block inside a planned session, with its weight already computed.

    Mirrors spec 04's `DraftLift` field for field.
    """

    __tablename__ = "planned_lift"
    __table_args__ = (
        sa.CheckConstraint("sets > 0", name="sets_positive"),
        sa.CheckConstraint("reps > 0", name="reps_positive"),
        sa.CheckConstraint("pct_num > 0", name="pct_num_positive"),
        sa.CheckConstraint("pct_den > 0", name="pct_den_positive"),
        sa.CheckConstraint("weight_kg > 0", name="weight_kg_positive"),
        sa.CheckConstraint("rel_intensity > 0", name="rel_intensity_positive"),
        sa.CheckConstraint(
            _one_of("variation", enums.VARIATIONS, nullable=True), name="variation_known"
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    session_id: uuid.UUID = Field(
        foreign_key="planned_session.id", ondelete="CASCADE", index=True, nullable=False
    )
    lift_code: str = Field(sa_type=sa.Text, foreign_key="lift.code", index=True, nullable=False)
    variation: str | None = Field(default=None, sa_type=sa.Text, nullable=True)
    sets: int = Field(nullable=False)
    reps: int = Field(nullable=False)
    pct_num: Decimal = Field(max_digits=10, decimal_places=4, nullable=False)
    pct_den: Decimal = Field(max_digits=10, decimal_places=4, nullable=False)
    # FLOOR(1RM x pct, plate_increment_kg), computed by spec 04's generator.
    weight_kg: Decimal = Field(max_digits=6, decimal_places=2, nullable=False)
    # pct / rep_max_pct(reps), rounded to 4 dp. 1.0 is the limit for that rep
    # count; above 1.0 is overreaching, permitted only from week 7.
    rel_intensity: Decimal = Field(max_digits=6, decimal_places=4, nullable=False)
    is_test: bool = Field(
        default=False,
        sa_column_kwargs={"server_default": sa.false()},
        nullable=False,
    )


# --- J. session_log -------------------------------------------------------
class SessionLog(TimestampMixin, table=True):
    """What actually happened, at most once per planned session."""

    __tablename__ = "session_log"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # ON DELETE CASCADE is required, not a preference: planned_session already
    # cascades from block, so without it, deleting a block would abort
    # mid-cascade on this restricted foreign key. The unique index serves as
    # this FK's index.
    planned_session_id: uuid.UUID = Field(
        foreign_key="planned_session.id", ondelete="CASCADE", unique=True, nullable=False
    )
    performed_on: dt.date = Field(nullable=False)
    # [{lift_code, sets_done, reps_done, weight_kg, rpe?}]
    # Defaulted, unlike block.lifts: a session logged with nothing completed is
    # a coherent thing to record.
    entries: list[Any] = Field(
        default_factory=list,
        sa_type=JSONB,
        sa_column_kwargs={"server_default": sa.text("'[]'::jsonb")},
        nullable=False,
    )
    note: str | None = Field(default=None, sa_type=sa.Text, nullable=True)


# --- K. note_chunk --------------------------------------------------------
class NoteChunk(TimestampMixin, table=True):
    """An embedded training note, retrieved by meaning in spec 12.

    Safety screening of notes is keyword-based and deterministic, never
    embedding-based (CLAUDE.md non-negotiable #3) — this table feeds retrieval,
    not the pain filter.
    """

    __tablename__ = "note_chunk"
    __table_args__ = (
        # Cosine is correct for nomic-embed-text, which is trained for it.
        # m and ef_construction are left at pgvector's defaults (16 / 64):
        # there is no tuning data, and CLAUDE.md forbids presenting a guessed
        # threshold as derived.
        sa.Index(
            "ix_note_chunk_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    athlete_id: uuid.UUID = Field(foreign_key="athlete.id", index=True, nullable=False)
    # Nullable: a note need not come from a logged session.
    log_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="session_log.id",
        ondelete="CASCADE",
        index=True,
        nullable=True,
    )
    lift_codes: list[str] = Field(
        default_factory=list,
        sa_type=JSONB,
        sa_column_kwargs={"server_default": sa.text("'[]'::jsonb")},
        nullable=False,
    )
    text: str = Field(sa_type=sa.Text, nullable=False)
    noted_on: dt.date = Field(nullable=False)
    # Dimension read from settings; migration 0001 hardcodes 768. A guard test
    # pins both, because this is evaluated at import time and a mismatch would
    # not surface until the first insert failed.
    embedding: list[float] = Field(sa_type=Vector(get_settings().embed_dim), nullable=False)
    # Demo seed notes are flagged, in the database and the README
    # (CONTEXT.md §8).
    is_synthetic: bool = Field(
        default=False,
        sa_column_kwargs={"server_default": sa.false()},
        nullable=False,
    )


# --- L. proposal ----------------------------------------------------------
class Proposal(TimestampMixin, table=True):
    """A candidate change awaiting human approval.

    `block_id` is RESTRICT, not CASCADE: the approval trail should outlive a
    deleted block.
    """

    __tablename__ = "proposal"
    __table_args__ = (
        sa.CheckConstraint(_one_of("kind", enums.PROPOSAL_KINDS), name="kind_known"),
        sa.CheckConstraint(_one_of("status", enums.PROPOSAL_STATUSES), name="status_known"),
        sa.CheckConstraint(_one_of("source", enums.PROPOSAL_SOURCES), name="source_known"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    block_id: uuid.UUID = Field(foreign_key="block.id", index=True, nullable=False)
    kind: enums.ProposalKind = Field(sa_type=sa.Text, nullable=False)
    status: enums.ProposalStatus = Field(
        default="pending",
        sa_type=sa.Text,
        sa_column_kwargs={"server_default": sa.text("'pending'")},
        nullable=False,
    )
    # The change set. Spec 05's StrengthPayload embeds the whole resulting
    # schedule, not just the diff, so the validator always checks the full
    # picture — expect this to be a sizeable document.
    payload: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB, nullable=False)
    cost: dict[str, Any] = Field(
        default_factory=dict,
        sa_type=JSONB,
        sa_column_kwargs={"server_default": sa.text("'{}'::jsonb")},
        nullable=False,
    )
    reasons: list[Any] = Field(
        default_factory=list,
        sa_type=JSONB,
        sa_column_kwargs={"server_default": sa.text("'[]'::jsonb")},
        nullable=False,
    )
    warnings: list[Any] = Field(
        default_factory=list,
        sa_type=JSONB,
        sa_column_kwargs={"server_default": sa.text("'[]'::jsonb")},
        nullable=False,
    )
    source: enums.ProposalSource = Field(sa_type=sa.Text, nullable=False)
    decided_at: dt.datetime | None = Field(default=None, nullable=True)
    # Why the *human* decided as they did, which is not what `reasons` holds:
    # that is why the solver proposed. Added in 0004 for spec 06's
    # `POST /proposals/{id}/reject {reason?}`, which otherwise had nowhere to
    # put its only argument. Null on an approval, and on a reject with no
    # reason given.
    decision_reason: str | None = Field(default=None, sa_type=sa.Text, nullable=True)


# --- M. decision ----------------------------------------------------------
class Decision(TimestampMixin, table=True):
    """One chat turn's trace: which filter fired, which route ran, what it did.

    `proposal_id` is RESTRICT for the same reason as above: the decision trail
    is a first-class artefact, surfaced in spec 21.
    """

    __tablename__ = "decision"
    __table_args__ = (
        sa.CheckConstraint(_one_of("source", enums.PROPOSAL_SOURCES), name="source_known"),
        sa.CheckConstraint(_one_of("route", enums.DECISION_ROUTES), name="route_known"),
        sa.CheckConstraint(
            _one_of("outcome", enums.DECISION_OUTCOMES, nullable=True), name="outcome_known"
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Indexed because spec 21's trace view looks decisions up by run.
    run_id: str = Field(sa_type=sa.Text, index=True, nullable=False)
    source: enums.ProposalSource = Field(sa_type=sa.Text, nullable=False)
    route: enums.DecisionRoute = Field(sa_type=sa.Text, nullable=False)
    # e.g. "pain_keyword". Non-null means a deterministic hard filter decided
    # the route before any LLM call.
    filter_fired: str | None = Field(default=None, sa_type=sa.Text, nullable=True)
    tool_calls: list[Any] = Field(
        default_factory=list,
        sa_type=JSONB,
        sa_column_kwargs={"server_default": sa.text("'[]'::jsonb")},
        nullable=False,
    )
    proposal_id: uuid.UUID | None = Field(
        default=None, foreign_key="proposal.id", index=True, nullable=True
    )
    outcome: enums.DecisionOutcome | None = Field(default=None, sa_type=sa.Text, nullable=True)
    langfuse_trace_id: str | None = Field(default=None, sa_type=sa.Text, nullable=True)


# --- N. chat_message ------------------------------------------------------
class ChatMessage(TimestampMixin, table=True):
    __tablename__ = "chat_message"
    __table_args__ = (
        # Composite rather than a bare conversation_id index: §4N requires
        # conversation_id be indexed, and this also serves the only read it has
        # — the last 10 messages of a conversation (CONTEXT.md §10).
        sa.Index("ix_chat_message_conversation_id_created_at", "conversation_id", "created_at"),
        sa.CheckConstraint(_one_of("role", enums.CHAT_ROLES), name="role_known"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Not a foreign key: a conversation has no table of its own in v1.
    conversation_id: uuid.UUID = Field(nullable=False)
    athlete_id: uuid.UUID = Field(foreign_key="athlete.id", index=True, nullable=False)
    role: enums.ChatRole = Field(sa_type=sa.Text, nullable=False)
    content: str = Field(sa_type=sa.Text, nullable=False)
    decision_id: uuid.UUID | None = Field(
        default=None, foreign_key="decision.id", index=True, nullable=True
    )
