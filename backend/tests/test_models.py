"""Schema tests.

Two halves. The offline half asserts against `SQLModel.metadata` and needs no
database, so it always runs. The DB half requests `db_session` and therefore
skips cleanly when Postgres is unreachable.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DataError, IntegrityError
from sqlmodel import Session, select

from domains.strength import enums, models
from planner_core.config import Settings, get_settings

METADATA = models.SQLModel.metadata

EXPECTED_TABLES = {
    "athlete",
    "block",
    "block_template",
    "chat_message",
    "decision",
    "lift",
    "note_chunk",
    "one_rep_max",
    "planned_lift",
    "planned_session",
    "proposal",
    "session_log",
    "template_entry",
    "weekday_map",
}

# (table, column, precision, scale) for every NUMERIC column in the schema.
NUMERIC_COLUMNS = [
    ("athlete", "plate_increment_kg", 4, 2),
    ("one_rep_max", "kg", 6, 2),
    ("template_entry", "pct_num", 10, 4),
    ("template_entry", "pct_den", 10, 4),
    ("planned_lift", "pct_num", 10, 4),
    ("planned_lift", "pct_den", 10, 4),
    ("planned_lift", "weight_kg", 6, 2),
    ("planned_lift", "rel_intensity", 6, 4),
]

# Every CHECK whose body is generated from a Literal, and the tuple it must
# match. Guards against a value being added to the enum but not the constraint.
ENUM_CHECKS = [
    ("athlete", "ck_athlete_rounding_known", "rounding", enums.ROUNDINGS, False),
    ("one_rep_max", "ck_one_rep_max_source_known", "source", enums.ONE_REP_MAX_SOURCES, False),
    ("block", "ck_block_status_known", "status", enums.BLOCK_STATUSES, False),
    (
        "planned_session",
        "ck_planned_session_status_known",
        "status",
        enums.PLANNED_SESSION_STATUSES,
        False,
    ),
    ("proposal", "ck_proposal_kind_known", "kind", enums.PROPOSAL_KINDS, False),
    ("proposal", "ck_proposal_status_known", "status", enums.PROPOSAL_STATUSES, False),
    ("proposal", "ck_proposal_source_known", "source", enums.PROPOSAL_SOURCES, False),
    ("decision", "ck_decision_source_known", "source", enums.PROPOSAL_SOURCES, False),
    ("decision", "ck_decision_route_known", "route", enums.DECISION_ROUTES, False),
    ("decision", "ck_decision_outcome_known", "outcome", enums.DECISION_OUTCOMES, True),
    ("chat_message", "ck_chat_message_role_known", "role", enums.CHAT_ROLES, False),
    (
        "template_entry",
        "ck_template_entry_variation_known",
        "variation",
        enums.VARIATIONS,
        True,
    ),
    ("planned_lift", "ck_planned_lift_variation_known", "variation", enums.VARIATIONS, True),
]

CASCADE_FOREIGN_KEYS = [
    ("template_entry", "template_id"),
    ("planned_session", "block_id"),
    ("planned_lift", "session_id"),
    ("note_chunk", "log_id"),
    # Required, not optional: planned_session already cascades from block, so a
    # restricted FK here would abort that cascade part-way.
    ("session_log", "planned_session_id"),
]


# --- Offline: structure ---------------------------------------------------


def test_the_schema_has_exactly_the_fourteen_expected_tables():
    assert set(METADATA.tables) == EXPECTED_TABLES


def test_every_table_has_a_timezone_aware_created_at_with_a_server_default():
    """Asserted on the compiled type, not the Python class.

    SQLModel maps `datetime` to its own `UTCDateTime`, which is a
    `TypeDecorator` over `DateTime` rather than a subclass of it, so an
    `isinstance(..., sa.DateTime)` check would fail while the column is
    perfectly correct. What matters is the DDL.
    """
    dialect = postgresql.dialect()
    for name, table in METADATA.tables.items():
        created_at = table.c["created_at"]
        assert created_at.type.compile(dialect=dialect) == "TIMESTAMP WITH TIME ZONE", name
        assert created_at.nullable is False, name
        assert created_at.server_default is not None, name


@pytest.mark.parametrize(("table", "column", "precision", "scale"), NUMERIC_COLUMNS)
def test_numeric_columns_carry_the_specified_precision(table, column, precision, scale):
    column_type = METADATA.tables[table].c[column].type
    assert isinstance(column_type, sa.Numeric)
    assert (column_type.precision, column_type.scale) == (precision, scale)


def test_percentages_are_numeric_rather_than_integer():
    """CLAUDE.md says "numerator/denominator pairs", which reads as integers.

    It cannot be: the authored template holds 112.5/105 and 72.5/70, and Squat
    W11 S3 is a bare 0.5 (CONTEXT.md §4).

    Exactness is preserved by converting each Decimal to a Fraction and
    dividing. Note the two-argument `Fraction(a, b)` constructor rejects
    Decimals, which is asserted here so spec 04 does not reach for it.
    """
    from fractions import Fraction

    for table in ("template_entry", "planned_lift"):
        for column in ("pct_num", "pct_den"):
            assert isinstance(METADATA.tables[table].c[column].type, sa.Numeric)
            assert not isinstance(METADATA.tables[table].c[column].type, sa.Integer)

    assert Fraction(Decimal("112.5")) / Fraction(Decimal("105")) == Fraction(15, 14)
    assert Fraction(Decimal("72.5")) / Fraction(Decimal("70")) == Fraction(29, 28)
    assert Fraction(Decimal("0.5")) == Fraction(1, 2)

    with pytest.raises(TypeError, match="Rational"):
        Fraction(Decimal("112.5"), Decimal("105"))


def test_every_foreign_key_column_leads_some_index():
    """Spec 02 §11: "Every FK has an index".

    PostgreSQL indexes primary and unique keys automatically but never foreign
    keys. A FK is served by any index whose *leading* column it is, so a
    composite unique constraint counts. Asserting the invariant beats trusting a
    hand-maintained list.
    """
    unserved = []
    for table in METADATA.tables.values():
        leading = {next(iter(index.columns)).name for index in table.indexes if len(index.columns)}
        for constraint in table.constraints:
            if isinstance(constraint, sa.UniqueConstraint | sa.PrimaryKeyConstraint) and len(
                constraint.columns
            ):
                leading.add(next(iter(constraint.columns)).name)
        for fk in table.foreign_keys:
            if fk.parent.name not in leading:
                unserved.append(f"{table.name}.{fk.parent.name}")
    assert unserved == []


def test_the_expected_foreign_keys_cascade_on_delete():
    for table_name, column in CASCADE_FOREIGN_KEYS:
        table = METADATA.tables[table_name]
        fks = [fk for fk in table.foreign_keys if fk.parent.name == column]
        assert len(fks) == 1, f"{table_name}.{column}"
        assert fks[0].ondelete == "CASCADE", f"{table_name}.{column}"


def test_the_decision_trail_is_not_deleted_with_its_block_or_proposal():
    """proposal.block_id and decision.proposal_id are deliberately RESTRICT."""
    for table_name, column in (("proposal", "block_id"), ("decision", "proposal_id")):
        fks = [fk for fk in METADATA.tables[table_name].foreign_keys if fk.parent.name == column]
        assert fks[0].ondelete is None, f"{table_name}.{column}"


def test_the_declared_unique_constraints_are_present():
    expected = {
        "block_template": {("name",)},
        "block": {("athlete_id", "number")},
        "template_entry": {("template_id", "lift_code", "week", "slot")},
        "weekday_map": {("athlete_id", "weekday")},
        "planned_session": {("block_id", "seq")},
        "session_log": {("planned_session_id",)},
    }
    for table_name, expected_keys in expected.items():
        actual = {
            tuple(column.name for column in constraint.columns)
            for constraint in METADATA.tables[table_name].constraints
            if isinstance(constraint, sa.UniqueConstraint)
        }
        assert actual == expected_keys, table_name


def test_weekday_map_has_a_composite_primary_key_and_no_surrogate_id():
    table = METADATA.tables["weekday_map"]
    assert [column.name for column in table.primary_key.columns] == ["athlete_id", "slot"]
    assert "id" not in table.c


def test_the_hnsw_index_is_declared_with_cosine_distance():
    index = next(ix for ix in METADATA.tables["note_chunk"].indexes if ix.name.endswith("_hnsw"))
    assert index.dialect_options["postgresql"]["using"] == "hnsw"
    assert index.dialect_options["postgresql"]["ops"] == {"embedding": "vector_cosine_ops"}


def test_the_one_rep_max_lookup_index_orders_set_on_descending():
    """The current 1RM is the newest row per lift, so DESC is load-bearing."""
    indexes = METADATA.tables["one_rep_max"].indexes
    target = next(ix for ix in indexes if ix.name == "ix_one_rep_max_athlete_lift_set_on")
    # Column expressions stringify table-qualified; the literal does not.
    rendered = [str(expression) for expression in target.expressions]
    assert rendered[:2] == ["one_rep_max.athlete_id", "one_rep_max.lift_code"]
    assert rendered[2] == "set_on DESC"


# --- Offline: the enum CHECK drift guard ----------------------------------


@pytest.mark.parametrize(("table", "name", "column", "allowed", "nullable"), ENUM_CHECKS)
def test_each_enum_check_matches_its_literal(table, name, column, allowed, nullable):
    constraint = next(
        c
        for c in METADATA.tables[table].constraints
        if isinstance(c, sa.CheckConstraint) and c.name == name
    )
    assert str(constraint.sqltext) == models._one_of(column, allowed, nullable=nullable)


def test_lift_code_has_no_check_because_the_foreign_key_is_the_constraint():
    checks = {
        c.name for c in METADATA.tables["lift"].constraints if isinstance(c, sa.CheckConstraint)
    }
    assert checks == set()


def test_the_check_builder_refuses_a_value_it_cannot_safely_quote():
    with pytest.raises(ValueError, match="unsafe enum value"):
        models._one_of("status", ("ok", "'; DROP TABLE athlete; --"))


# --- Offline: the embedding dimension guard -------------------------------


def test_embedding_dimension_matches_the_initial_migration():
    """`0001_init` hardcodes 768. These must not drift silently.

    `Vector(...)` is evaluated when models.py is imported, so a changed
    EMBED_DIM would produce a model disagreeing with the migrated column, and
    nothing would fail until the first insert.
    """
    assert Settings(_env_file=None).embed_dim == 768
    assert METADATA.tables["note_chunk"].c["embedding"].type.dim == get_settings().embed_dim


# --- Database-backed ------------------------------------------------------


def test_the_vector_extension_is_installed_and_supports_hnsw(db_session: Session):
    """HNSW needs the *server* extension >= 0.5.0.

    The `pgvector` Python package version says nothing about this, so it is
    asserted rather than assumed.
    """
    version = db_session.exec(
        sa.text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    ).one()[0]
    major, minor = (int(part) for part in version.split(".")[:2])
    assert (major, minor) >= (0, 5), version


def test_the_migrations_create_all_fourteen_tables(db_session: Session):
    names = set(
        db_session.exec(
            sa.text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name <> 'alembic_version'"
            )
        )
        .scalars()
        .all()
    )
    assert names == EXPECTED_TABLES


def test_the_hnsw_index_exists_in_the_database(db_session: Session):
    indexdef = db_session.exec(
        sa.text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_note_chunk_embedding_hnsw'")
    ).one()[0]
    assert "USING hnsw" in indexdef
    assert "vector_cosine_ops" in indexdef


def test_the_five_lifts_are_seeded(db_session: Session):
    lifts = db_session.exec(select(models.Lift).order_by(models.Lift.code)).all()
    assert {lift.code for lift in lifts} == set(enums.LIFT_CODES)
    accessories = {lift.code for lift in lifts if lift.is_accessory}
    assert accessories == {"row"}
    with_variations = {lift.code: lift.variations for lift in lifts if lift.variations}
    assert with_variations == {"bench": ["close_grip", "wide_grip"]}


def test_a_template_entry_with_an_unknown_lift_code_is_rejected(db_session: Session):
    """Definition of Done: one FK violation."""
    template = models.BlockTemplate(name="fk-violation-probe")
    db_session.add(template)
    db_session.flush()

    db_session.add(
        models.TemplateEntry(
            template_id=template.id,
            lift_code="not_a_lift",
            week=1,
            slot=1,
            sets=3,
            reps=5,
            pct_num=Decimal("70"),
            pct_den=Decimal("120"),
        )
    )
    with pytest.raises(IntegrityError, match="fk_template_entry_lift_code_lift"):
        db_session.flush()


def test_two_slots_cannot_share_a_weekday(db_session: Session):
    """Definition of Done: one unique violation."""
    athlete = models.Athlete(name="unique-violation-probe")
    db_session.add(athlete)
    db_session.flush()

    db_session.add(models.WeekdayMap(athlete_id=athlete.id, slot=1, weekday=0))
    db_session.flush()
    db_session.add(models.WeekdayMap(athlete_id=athlete.id, slot=2, weekday=0))
    with pytest.raises(IntegrityError, match="uq_weekday_map_athlete_id_weekday"):
        db_session.flush()


def test_a_block_cannot_start_on_a_day_other_than_monday(db_session: Session):
    athlete = models.Athlete(name="monday-probe")
    template = models.BlockTemplate(name="monday-probe-template")
    db_session.add(athlete)
    db_session.add(template)
    db_session.flush()

    # 2026-01-06 is a Tuesday.
    db_session.add(
        models.Block(
            athlete_id=athlete.id,
            template_id=template.id,
            number=1,
            start_date=dt.date(2026, 1, 6),
            lifts=["squat"],
            one_rm_snapshot={"squat": "140"},
        )
    )
    with pytest.raises(IntegrityError, match="ck_block_start_date_is_monday"):
        db_session.flush()


def test_a_monday_start_date_is_accepted(db_session: Session):
    """The counterpart to the test above: the CHECK must not reject a Monday."""
    athlete = models.Athlete(name="monday-ok-probe")
    template = models.BlockTemplate(name="monday-ok-template")
    db_session.add(athlete)
    db_session.add(template)
    db_session.flush()

    monday = dt.date(2026, 1, 5)
    assert monday.weekday() == 0
    block = models.Block(
        athlete_id=athlete.id,
        template_id=template.id,
        number=1,
        start_date=monday,
        lifts=["squat"],
        one_rm_snapshot={"squat": "140"},
    )
    db_session.add(block)
    db_session.flush()
    assert block.status == "draft"


def test_an_unknown_block_status_is_rejected(db_session: Session):
    """The CHECK is the only enforcement: SQLModel table classes skip validation."""
    athlete = models.Athlete(name="status-probe")
    template = models.BlockTemplate(name="status-probe-template")
    db_session.add(athlete)
    db_session.add(template)
    db_session.flush()

    block = models.Block(
        athlete_id=athlete.id,
        template_id=template.id,
        number=1,
        start_date=dt.date(2026, 1, 5),
        lifts=["squat"],
        one_rm_snapshot={},
    )
    # Constructing this with an invalid status does NOT raise, which is exactly
    # why the database constraint has to exist.
    block.status = "banana"  # type: ignore[assignment]
    db_session.add(block)
    with pytest.raises(IntegrityError, match="ck_block_status_known"):
        db_session.flush()


def test_an_embedding_of_the_wrong_length_is_rejected(db_session: Session):
    athlete = models.Athlete(name="embedding-probe")
    db_session.add(athlete)
    db_session.flush()

    db_session.add(
        models.NoteChunk(
            athlete_id=athlete.id,
            text="knee tight at depth",
            noted_on=dt.date(2026, 1, 5),
            embedding=[0.1, 0.2, 0.3],  # 3 dimensions, not 768
        )
    )
    with pytest.raises((DataError, IntegrityError)):
        db_session.flush()


def test_an_embedding_of_the_right_length_round_trips(db_session: Session):
    athlete = models.Athlete(name="embedding-ok-probe")
    db_session.add(athlete)
    db_session.flush()

    vector = [0.0] * get_settings().embed_dim
    vector[0] = 0.5
    note = models.NoteChunk(
        athlete_id=athlete.id,
        text="slept 5 h, bar slow",
        noted_on=dt.date(2026, 1, 5),
        embedding=vector,
    )
    db_session.add(note)
    db_session.flush()
    db_session.refresh(note)
    assert len(note.embedding) == get_settings().embed_dim
    assert note.is_synthetic is False


def test_deleting_a_block_cascades_through_sessions_and_logs(db_session: Session):
    """The reason session_log.planned_session_id must be CASCADE.

    With a restricted FK there, this delete fails part-way through the cascade
    from block to planned_session.
    """
    athlete = models.Athlete(name="cascade-probe")
    template = models.BlockTemplate(name="cascade-probe-template")
    db_session.add(athlete)
    db_session.add(template)
    db_session.flush()

    block = models.Block(
        athlete_id=athlete.id,
        template_id=template.id,
        number=1,
        start_date=dt.date(2026, 1, 5),
        lifts=["squat"],
        one_rm_snapshot={},
    )
    db_session.add(block)
    db_session.flush()

    session_row = models.PlannedSession(
        block_id=block.id,
        week=1,
        slot=1,
        seq=1,
        date=dt.date(2026, 1, 5),
        original_date=dt.date(2026, 1, 5),
    )
    db_session.add(session_row)
    db_session.flush()

    db_session.add(
        models.SessionLog(planned_session_id=session_row.id, performed_on=dt.date(2026, 1, 5))
    )
    db_session.add(
        models.PlannedLift(
            session_id=session_row.id,
            lift_code="squat",
            sets=3,
            reps=5,
            pct_num=Decimal("70"),
            pct_den=Decimal("120"),
            weight_kg=Decimal("81.00"),
            rel_intensity=Decimal("0.8500"),
        )
    )
    db_session.flush()

    db_session.execute(sa.delete(models.Block).where(models.Block.id == block.id))
    db_session.flush()

    for model in (models.PlannedSession, models.SessionLog, models.PlannedLift):
        remaining = db_session.exec(select(model)).all()
        assert remaining == [], model.__name__


def test_exact_decimals_survive_a_round_trip(db_session: Session):
    """The spreadsheet's real test percentages, stored and read back unchanged."""
    template = models.BlockTemplate(name="decimal-probe-template")
    db_session.add(template)
    db_session.flush()

    entry = models.TemplateEntry(
        template_id=template.id,
        lift_code="bench",
        variation="wide_grip",
        week=10,
        slot=3,
        sets=1,
        reps=1,
        pct_num=Decimal("112.5"),
        pct_den=Decimal("105"),
        is_test=True,
    )
    db_session.add(entry)
    db_session.flush()
    db_session.refresh(entry)
    assert entry.pct_num == Decimal("112.5")
    assert entry.pct_den == Decimal("105")


def test_the_seed_script_is_idempotent(db_session: Session):
    """Definition of Done: the seed athlete script is idempotent."""
    from scripts.seed_athlete import ATHLETE_NAME, seed

    seed(db_session)
    seed(db_session)

    athletes = db_session.exec(
        select(models.Athlete).where(models.Athlete.name == ATHLETE_NAME)
    ).all()
    assert len(athletes) == 1

    # The spreadsheet's maxes are examples: only 1RMs the athlete enters, with
    # the date they tested them, may ever exist.
    one_rms = db_session.exec(
        select(models.OneRepMax).where(models.OneRepMax.athlete_id == athletes[0].id)
    ).all()
    assert one_rms == []


# --- Synthetic development 1RMs (ADR-0004) --------------------------------


def test_a_one_rep_max_is_real_unless_it_says_otherwise(db_session: Session):
    """`is_synthetic` defaults to false, which is the safe direction."""
    athlete = models.Athlete(name="synthetic-default-probe")
    db_session.add(athlete)
    db_session.flush()

    row = models.OneRepMax(
        athlete_id=athlete.id,
        lift_code="squat",
        kg=Decimal("140"),
        set_on=dt.date(2026, 3, 2),
        source="tested",
    )
    db_session.add(row)
    db_session.flush()
    db_session.refresh(row)
    assert row.is_synthetic is False


def test_a_synthetic_source_cannot_be_left_unflagged(db_session: Session):
    """The two columns that answer "is this real?" are unable to disagree."""
    athlete = models.Athlete(name="synthetic-check-probe")
    db_session.add(athlete)
    db_session.flush()

    db_session.add(
        models.OneRepMax(
            athlete_id=athlete.id,
            lift_code="squat",
            kg=Decimal("130"),
            set_on=dt.date(2026, 3, 2),
            source="synthetic",
            is_synthetic=False,
        )
    )
    with pytest.raises(IntegrityError, match="ck_one_rep_max_synthetic_source_is_flagged"):
        db_session.flush()


def test_a_flagged_row_may_carry_any_source(db_session: Session):
    """The constraint is an implication, not a biconditional.

    Faking a `tested` max is how spec 06's test-completion path gets exercised
    without real data, so it has to stay possible.
    """
    athlete = models.Athlete(name="synthetic-faked-tested-probe")
    db_session.add(athlete)
    db_session.flush()

    row = models.OneRepMax(
        athlete_id=athlete.id,
        lift_code="squat",
        kg=Decimal("151"),
        set_on=dt.date(2026, 3, 2),
        source="tested",
        is_synthetic=True,
    )
    db_session.add(row)
    db_session.flush()
    db_session.refresh(row)
    assert (row.source, row.is_synthetic) == ("tested", True)


def test_the_dev_one_rm_seed_flags_every_row_twice(db_session: Session):
    from scripts.seed_athlete import seed
    from scripts.seed_dev_one_rms import DEFAULT_KG, seed_dev_one_rms

    athlete = seed(db_session)
    written = seed_dev_one_rms(db_session, athlete=athlete, kg=dict(DEFAULT_KG))

    assert len(written) == len(enums.LIFT_CODES)
    assert all(row.is_synthetic is True for row in written)
    assert {row.source for row in written} == {"synthetic"}
    # Not the spreadsheet's example values, so the two can never be confused.
    assert {row.kg for row in written}.isdisjoint(
        {Decimal("160"), Decimal("140"), Decimal("105"), Decimal("70")}
    )


def test_the_dev_one_rm_seed_is_idempotent(db_session: Session):
    from scripts.seed_athlete import seed
    from scripts.seed_dev_one_rms import DEFAULT_KG, seed_dev_one_rms

    athlete = seed(db_session)
    seed_dev_one_rms(db_session, athlete=athlete, kg=dict(DEFAULT_KG))
    second = seed_dev_one_rms(db_session, athlete=athlete, kg=dict(DEFAULT_KG))

    assert second == []
    rows = db_session.exec(
        select(models.OneRepMax).where(models.OneRepMax.athlete_id == athlete.id)
    ).all()
    assert len(rows) == len(enums.LIFT_CODES)


def test_clearing_dev_one_rms_spares_real_rows(db_session: Session):
    """The whole point of the flag: fake data is removable without guesswork."""
    from scripts.seed_athlete import seed
    from scripts.seed_dev_one_rms import DEFAULT_KG, clear_dev_one_rms, seed_dev_one_rms

    athlete = seed(db_session)
    seed_dev_one_rms(db_session, athlete=athlete, kg=dict(DEFAULT_KG))
    real = models.OneRepMax(
        athlete_id=athlete.id,
        lift_code="squat",
        kg=Decimal("142.5"),
        set_on=dt.date(2026, 6, 1),
        source="tested",
    )
    db_session.add(real)
    # A faked `tested` row is synthetic too, and must also go.
    faked = models.OneRepMax(
        athlete_id=athlete.id,
        lift_code="bench",
        kg=Decimal("99"),
        set_on=dt.date(2026, 6, 1),
        source="tested",
        is_synthetic=True,
    )
    db_session.add(faked)
    db_session.flush()

    removed = clear_dev_one_rms(db_session)
    assert removed == len(enums.LIFT_CODES) + 1

    remaining = db_session.exec(
        select(models.OneRepMax).where(models.OneRepMax.athlete_id == athlete.id)
    ).all()
    assert [(row.lift_code, row.kg, row.source) for row in remaining] == [
        ("squat", Decimal("142.50"), "tested")
    ]


def test_a_uuid_primary_key_is_generated_in_python(db_session: Session):
    """Spec 02 §4: primary keys are generated in Python with uuid4."""
    athlete = models.Athlete(name="uuid-probe")
    assert isinstance(athlete.id, uuid.UUID)
    assert athlete.id.version == 4
    db_session.add(athlete)
    db_session.flush()
