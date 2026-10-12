"""init: the vector extension and all 14 tables

Revision ID: 0001
Revises:
Create Date: 2026-10-11 20:56:25.703902

Drafted with `--autogenerate` against the models, then hand-finished. Three
changes were required, and all three are why this file is not left as
generated:

1.  **`CREATE EXTENSION IF NOT EXISTS vector` had to be added, first.**
    Autogenerate never emits it, and `note_chunk.embedding` cannot be created
    without it. The `pgvector/pgvector:pg16` image ships the extension files;
    this is what enables them in *this* database. `planner` is the image's
    `POSTGRES_USER` and therefore superuser, so it has the privilege.
2.  **Two types were rendered without their imports.** Autogenerate emitted
    `sqlmodel.sql.sqltypes.UTCDateTime()` and
    `pgvector.sqlalchemy.vector.VECTOR(dim=768)` but imported neither module,
    so the file raised `NameError` as generated. `UTCDateTime` is a
    `TypeDecorator` over `DateTime(timezone=True)` — it compiles to the same
    `TIMESTAMP WITH TIME ZONE`, and its only extra behaviour is rejecting naive
    datetimes on bind, which is a Python-side concern of no relevance to DDL.
    So the plain SQLAlchemy type is used here rather than reaching into
    SQLModel's internals. `test_the_migrations_match_the_models` confirms the
    two are equivalent as far as the database is concerned.
3.  **`EMBED_DIM` is hardcoded.** See the constant below.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Hardcoded on purpose, and not read from `settings.embed_dim`.
#
# A migration is a historical record of what the schema *became*. It has to
# produce the same DDL in five years regardless of what EMBED_DIM is configured
# to then. Changing the embedding model is a new migration — an ALTER TABLE to
# vector(n) plus an index rebuild — not a silent re-interpretation of this one.
#
# `tests/test_models.py` asserts the model and the default config both still
# agree with this number, so the two cannot drift unnoticed.
EMBED_DIM = 768

# TIMESTAMPTZ, matching SQLModel's UTCDateTime without importing it.
_TIMESTAMPTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    # Must precede note_chunk. IF NOT EXISTS makes a re-run harmless.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "athlete",
        sa.Column("created_at", _TIMESTAMPTZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "plate_increment_kg",
            sa.Numeric(precision=4, scale=2),
            server_default=sa.text("1.00"),
            nullable=False,
        ),
        sa.Column("rounding", sa.Text(), server_default=sa.text("'floor'"), nullable=False),
        sa.Column("timezone", sa.Text(), server_default=sa.text("'Europe/Berlin'"), nullable=False),
        sa.CheckConstraint(
            "rounding IN ('floor', 'nearest')", name=op.f("ck_athlete_rounding_known")
        ),
        sa.CheckConstraint(
            "plate_increment_kg > 0", name=op.f("ck_athlete_plate_increment_positive")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_athlete")),
    )
    op.create_table(
        "block_template",
        sa.Column("created_at", _TIMESTAMPTZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("weeks", sa.Integer(), nullable=False),
        sa.Column("slots", sa.Integer(), nullable=False),
        sa.Column(
            "overreach_from_week",
            sa.Integer(),
            server_default=sa.text("7"),
            nullable=False,
        ),
        sa.Column("source_file", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "overreach_from_week >= 1 AND overreach_from_week <= weeks",
            name=op.f("ck_block_template_overreach_within_block"),
        ),
        sa.CheckConstraint("slots > 0", name=op.f("ck_block_template_slots_positive")),
        sa.CheckConstraint("weeks > 0", name=op.f("ck_block_template_weeks_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_block_template")),
        sa.UniqueConstraint("name", name=op.f("uq_block_template_name")),
    )
    op.create_table(
        "lift",
        sa.Column("created_at", _TIMESTAMPTZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("is_accessory", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "variations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_lift")),
    )
    op.create_table(
        "block",
        sa.Column("created_at", _TIMESTAMPTZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("lifts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("one_rm_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("workflow_id", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft', 'approved', 'active', 'paused', 'done', 'abandoned')",
            name=op.f("ck_block_status_known"),
        ),
        sa.CheckConstraint(
            "EXTRACT(ISODOW FROM start_date) = 1", name=op.f("ck_block_start_date_is_monday")
        ),
        sa.CheckConstraint("number >= 1", name=op.f("ck_block_number_positive")),
        sa.ForeignKeyConstraint(
            ["athlete_id"], ["athlete.id"], name=op.f("fk_block_athlete_id_athlete")
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["block_template.id"],
            name=op.f("fk_block_template_id_block_template"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_block")),
        sa.UniqueConstraint("athlete_id", "number", name=op.f("uq_block_athlete_id_number")),
    )
    op.create_index(op.f("ix_block_template_id"), "block", ["template_id"], unique=False)
    op.create_table(
        "template_entry",
        sa.Column("created_at", _TIMESTAMPTZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("lift_code", sa.Text(), nullable=False),
        sa.Column("variation", sa.Text(), nullable=True),
        sa.Column("week", sa.Integer(), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("sets", sa.Integer(), nullable=False),
        sa.Column("reps", sa.Integer(), nullable=False),
        sa.Column("pct_num", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("pct_den", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("is_test", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint(
            "variation IS NULL OR variation IN ('close_grip', 'wide_grip')",
            name=op.f("ck_template_entry_variation_known"),
        ),
        sa.CheckConstraint("pct_den > 0", name=op.f("ck_template_entry_pct_den_positive")),
        sa.CheckConstraint("pct_num > 0", name=op.f("ck_template_entry_pct_num_positive")),
        sa.CheckConstraint("reps > 0", name=op.f("ck_template_entry_reps_positive")),
        sa.CheckConstraint("sets > 0", name=op.f("ck_template_entry_sets_positive")),
        sa.CheckConstraint("slot >= 1", name=op.f("ck_template_entry_slot_positive")),
        sa.CheckConstraint("week >= 1", name=op.f("ck_template_entry_week_positive")),
        sa.ForeignKeyConstraint(
            ["lift_code"], ["lift.code"], name=op.f("fk_template_entry_lift_code_lift")
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["block_template.id"],
            name=op.f("fk_template_entry_template_id_block_template"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_template_entry")),
        sa.UniqueConstraint(
            "template_id",
            "lift_code",
            "week",
            "slot",
            name=op.f("uq_template_entry_template_id_lift_code_week_slot"),
        ),
    )
    op.create_index(
        op.f("ix_template_entry_lift_code"), "template_entry", ["lift_code"], unique=False
    )
    op.create_table(
        "weekday_map",
        sa.Column("created_at", _TIMESTAMPTZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("locked_from", sa.Date(), nullable=True),
        sa.CheckConstraint("slot BETWEEN 1 AND 3", name=op.f("ck_weekday_map_slot_in_range")),
        sa.CheckConstraint("weekday BETWEEN 0 AND 6", name=op.f("ck_weekday_map_weekday_in_range")),
        sa.ForeignKeyConstraint(
            ["athlete_id"], ["athlete.id"], name=op.f("fk_weekday_map_athlete_id_athlete")
        ),
        sa.PrimaryKeyConstraint("athlete_id", "slot", name=op.f("pk_weekday_map")),
        sa.UniqueConstraint(
            "athlete_id", "weekday", name=op.f("uq_weekday_map_athlete_id_weekday")
        ),
    )
    op.create_table(
        "one_rep_max",
        sa.Column("created_at", _TIMESTAMPTZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("lift_code", sa.Text(), nullable=False),
        sa.Column("kg", sa.Numeric(precision=6, scale=2), nullable=False),
        sa.Column("set_on", sa.Date(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("block_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "source IN ('tested', 'estimated', 'imported')",
            name=op.f("ck_one_rep_max_source_known"),
        ),
        sa.CheckConstraint("kg > 0", name=op.f("ck_one_rep_max_kg_positive")),
        sa.ForeignKeyConstraint(
            ["athlete_id"], ["athlete.id"], name=op.f("fk_one_rep_max_athlete_id_athlete")
        ),
        sa.ForeignKeyConstraint(
            ["block_id"], ["block.id"], name=op.f("fk_one_rep_max_block_id_block")
        ),
        sa.ForeignKeyConstraint(
            ["lift_code"], ["lift.code"], name=op.f("fk_one_rep_max_lift_code_lift")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_one_rep_max")),
    )
    # DESC is load-bearing: the current 1RM is the newest row per lift.
    op.create_index(
        "ix_one_rep_max_athlete_lift_set_on",
        "one_rep_max",
        ["athlete_id", "lift_code", sa.literal_column("set_on DESC")],
        unique=False,
    )
    op.create_index(op.f("ix_one_rep_max_block_id"), "one_rep_max", ["block_id"], unique=False)
    op.create_index(op.f("ix_one_rep_max_lift_code"), "one_rep_max", ["lift_code"], unique=False)
    op.create_table(
        "planned_session",
        sa.Column("created_at", _TIMESTAMPTZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("block_id", sa.Uuid(), nullable=False),
        sa.Column("week", sa.Integer(), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("original_date", sa.Date(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'planned'"), nullable=False),
        sa.CheckConstraint(
            "status IN ('planned', 'done', 'missed', 'moved', 'skipped')",
            name=op.f("ck_planned_session_status_known"),
        ),
        sa.CheckConstraint("seq >= 1", name=op.f("ck_planned_session_seq_positive")),
        sa.CheckConstraint("slot >= 1", name=op.f("ck_planned_session_slot_positive")),
        sa.CheckConstraint("week >= 1", name=op.f("ck_planned_session_week_positive")),
        sa.ForeignKeyConstraint(
            ["block_id"],
            ["block.id"],
            name=op.f("fk_planned_session_block_id_block"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_planned_session")),
        sa.UniqueConstraint("block_id", "seq", name=op.f("uq_planned_session_block_id_seq")),
    )
    op.create_table(
        "proposal",
        sa.Column("created_at", _TIMESTAMPTZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("block_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "cost",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("decided_at", _TIMESTAMPTZ, nullable=True),
        sa.CheckConstraint(
            "kind IN ('new_block', 'reschedule', 'weekday_change')",
            name=op.f("ck_proposal_kind_known"),
        ),
        sa.CheckConstraint("source IN ('chat', 'workflow')", name=op.f("ck_proposal_source_known")),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'superseded')",
            name=op.f("ck_proposal_status_known"),
        ),
        sa.ForeignKeyConstraint(
            ["block_id"], ["block.id"], name=op.f("fk_proposal_block_id_block")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_proposal")),
    )
    op.create_index(op.f("ix_proposal_block_id"), "proposal", ["block_id"], unique=False)
    op.create_table(
        "decision",
        sa.Column("created_at", _TIMESTAMPTZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("route", sa.Text(), nullable=False),
        sa.Column("filter_fired", sa.Text(), nullable=True),
        sa.Column(
            "tool_calls",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("proposal_id", sa.Uuid(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("langfuse_trace_id", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('approved', 'rejected', 'answered', 'failed')",
            name=op.f("ck_decision_outcome_known"),
        ),
        sa.CheckConstraint(
            "route IN ('answer', 'plan', 'reschedule', 'simulate', 'safety', 'clarify')",
            name=op.f("ck_decision_route_known"),
        ),
        sa.CheckConstraint("source IN ('chat', 'workflow')", name=op.f("ck_decision_source_known")),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["proposal.id"], name=op.f("fk_decision_proposal_id_proposal")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_decision")),
    )
    op.create_index(op.f("ix_decision_proposal_id"), "decision", ["proposal_id"], unique=False)
    op.create_index(op.f("ix_decision_run_id"), "decision", ["run_id"], unique=False)
    op.create_table(
        "planned_lift",
        sa.Column("created_at", _TIMESTAMPTZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("lift_code", sa.Text(), nullable=False),
        sa.Column("variation", sa.Text(), nullable=True),
        sa.Column("sets", sa.Integer(), nullable=False),
        sa.Column("reps", sa.Integer(), nullable=False),
        sa.Column("pct_num", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("pct_den", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("weight_kg", sa.Numeric(precision=6, scale=2), nullable=False),
        sa.Column("rel_intensity", sa.Numeric(precision=6, scale=4), nullable=False),
        sa.Column("is_test", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint(
            "variation IS NULL OR variation IN ('close_grip', 'wide_grip')",
            name=op.f("ck_planned_lift_variation_known"),
        ),
        sa.CheckConstraint("pct_den > 0", name=op.f("ck_planned_lift_pct_den_positive")),
        sa.CheckConstraint("pct_num > 0", name=op.f("ck_planned_lift_pct_num_positive")),
        sa.CheckConstraint(
            "rel_intensity > 0", name=op.f("ck_planned_lift_rel_intensity_positive")
        ),
        sa.CheckConstraint("reps > 0", name=op.f("ck_planned_lift_reps_positive")),
        sa.CheckConstraint("sets > 0", name=op.f("ck_planned_lift_sets_positive")),
        sa.CheckConstraint("weight_kg > 0", name=op.f("ck_planned_lift_weight_kg_positive")),
        sa.ForeignKeyConstraint(
            ["lift_code"], ["lift.code"], name=op.f("fk_planned_lift_lift_code_lift")
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["planned_session.id"],
            name=op.f("fk_planned_lift_session_id_planned_session"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_planned_lift")),
    )
    op.create_index(op.f("ix_planned_lift_lift_code"), "planned_lift", ["lift_code"], unique=False)
    op.create_index(
        op.f("ix_planned_lift_session_id"), "planned_lift", ["session_id"], unique=False
    )
    op.create_table(
        "session_log",
        sa.Column("created_at", _TIMESTAMPTZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("planned_session_id", sa.Uuid(), nullable=False),
        sa.Column("performed_on", sa.Date(), nullable=False),
        sa.Column(
            "entries",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        # CASCADE is required, not stylistic: planned_session already cascades
        # from block, so a restricted FK here would abort that cascade.
        sa.ForeignKeyConstraint(
            ["planned_session_id"],
            ["planned_session.id"],
            name=op.f("fk_session_log_planned_session_id_planned_session"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session_log")),
        sa.UniqueConstraint("planned_session_id", name=op.f("uq_session_log_planned_session_id")),
    )
    op.create_table(
        "chat_message",
        sa.Column("created_at", _TIMESTAMPTZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("decision_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "role IN ('user', 'assistant')", name=op.f("ck_chat_message_role_known")
        ),
        sa.ForeignKeyConstraint(
            ["athlete_id"], ["athlete.id"], name=op.f("fk_chat_message_athlete_id_athlete")
        ),
        sa.ForeignKeyConstraint(
            ["decision_id"], ["decision.id"], name=op.f("fk_chat_message_decision_id_decision")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_message")),
    )
    op.create_index(
        op.f("ix_chat_message_athlete_id"), "chat_message", ["athlete_id"], unique=False
    )
    op.create_index(
        "ix_chat_message_conversation_id_created_at",
        "chat_message",
        ["conversation_id", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_chat_message_decision_id"), "chat_message", ["decision_id"], unique=False
    )
    op.create_table(
        "note_chunk",
        sa.Column("created_at", _TIMESTAMPTZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("athlete_id", sa.Uuid(), nullable=False),
        sa.Column("log_id", sa.Uuid(), nullable=True),
        sa.Column(
            "lift_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("noted_on", sa.Date(), nullable=False),
        sa.Column("embedding", Vector(EMBED_DIM), nullable=False),
        sa.Column("is_synthetic", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.ForeignKeyConstraint(
            ["athlete_id"], ["athlete.id"], name=op.f("fk_note_chunk_athlete_id_athlete")
        ),
        sa.ForeignKeyConstraint(
            ["log_id"],
            ["session_log.id"],
            name=op.f("fk_note_chunk_log_id_session_log"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_note_chunk")),
    )
    op.create_index(op.f("ix_note_chunk_athlete_id"), "note_chunk", ["athlete_id"], unique=False)
    # Cosine, because nomic-embed-text is trained for it. m / ef_construction
    # stay at pgvector's defaults; there is no tuning data to justify otherwise.
    op.create_index(
        "ix_note_chunk_embedding_hnsw",
        "note_chunk",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index(op.f("ix_note_chunk_log_id"), "note_chunk", ["log_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_note_chunk_log_id"), table_name="note_chunk")
    op.drop_index(
        "ix_note_chunk_embedding_hnsw",
        table_name="note_chunk",
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.drop_index(op.f("ix_note_chunk_athlete_id"), table_name="note_chunk")
    op.drop_table("note_chunk")
    op.drop_index(op.f("ix_chat_message_decision_id"), table_name="chat_message")
    op.drop_index("ix_chat_message_conversation_id_created_at", table_name="chat_message")
    op.drop_index(op.f("ix_chat_message_athlete_id"), table_name="chat_message")
    op.drop_table("chat_message")
    op.drop_table("session_log")
    op.drop_index(op.f("ix_planned_lift_session_id"), table_name="planned_lift")
    op.drop_index(op.f("ix_planned_lift_lift_code"), table_name="planned_lift")
    op.drop_table("planned_lift")
    op.drop_index(op.f("ix_decision_run_id"), table_name="decision")
    op.drop_index(op.f("ix_decision_proposal_id"), table_name="decision")
    op.drop_table("decision")
    op.drop_index(op.f("ix_proposal_block_id"), table_name="proposal")
    op.drop_table("proposal")
    op.drop_table("planned_session")
    op.drop_index(op.f("ix_one_rep_max_lift_code"), table_name="one_rep_max")
    op.drop_index(op.f("ix_one_rep_max_block_id"), table_name="one_rep_max")
    op.drop_index("ix_one_rep_max_athlete_lift_set_on", table_name="one_rep_max")
    op.drop_table("one_rep_max")
    op.drop_table("weekday_map")
    op.drop_index(op.f("ix_template_entry_lift_code"), table_name="template_entry")
    op.drop_table("template_entry")
    op.drop_index(op.f("ix_block_template_id"), table_name="block")
    op.drop_table("block")
    op.drop_table("lift")
    op.drop_table("block_template")
    op.drop_table("athlete")
    # The `vector` extension is deliberately left in place. Dropping an
    # extension that another database object might depend on is a footgun, and
    # re-creating it costs nothing.
