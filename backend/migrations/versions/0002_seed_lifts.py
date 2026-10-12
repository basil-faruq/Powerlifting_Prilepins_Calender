"""seed the five lifts

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-11 20:56:25.703902

The five lifts are reference data, not user data: every `lift_code` foreign key
in the schema points here, so the table has to be populated before anything
else can be written. That makes it a migration rather than a seed script.

Hand-written, because `--autogenerate` does not produce data migrations.
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, insert

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# An ad-hoc table stub, deliberately not an import of `domains.strength.models`.
# A migration must keep emitting the same SQL after the model changes, so it
# cannot depend on the model's current shape.
lift = sa.table(
    "lift",
    sa.column("code", sa.Text),
    sa.column("name", sa.Text),
    sa.column("is_accessory", sa.Boolean),
    sa.column("variations", JSONB),
)

# Display names follow CONTEXT.md §4's own table rather than invented long forms
# ("Barbell Row"), so they match the vocabulary the rest of the project and the
# source spreadsheet's sheet labels use.
#
# `row` is the only accessory and the only lift never tested; bench is the only
# lift with variations (spec 02 §4B, §10).
LIFTS: tuple[tuple[str, str, bool, list[str]], ...] = (
    ("squat", "Squat", False, []),
    ("bench", "Bench", False, ["close_grip", "wide_grip"]),
    ("deadlift", "Deadlift", False, []),
    ("press", "Press", False, []),
    ("row", "Row", True, []),
)


def upgrade() -> None:
    rows = [
        {
            "code": code,
            "name": name,
            "is_accessory": is_accessory,
            # The cast is not decoration. Under `literal_binds` — which is how
            # `alembic upgrade head --sql` renders this file — a bare Python
            # list bound to a JSONB column raises
            #   CompileError: No literal value renderer is available for
            #   literal value "[]" with datatype JSONB
            # Casting a JSON string renders as CAST('[...]' AS JSONB), which
            # compiles both online and offline.
            "variations": sa.cast(sa.literal(json.dumps(variations)), JSONB),
        }
        for code, name, is_accessory, variations in LIFTS
    ]
    # Idempotent: re-running the migration on a populated table is a no-op
    # rather than a unique violation.
    op.execute(insert(lift).values(rows).on_conflict_do_nothing(index_elements=["code"]))


def downgrade() -> None:
    # Delete only the rows this revision inserted.
    op.execute(lift.delete().where(lift.c.code.in_([code for code, *_ in LIFTS])))
