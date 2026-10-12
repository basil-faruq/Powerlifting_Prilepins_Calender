"""Allow a 1RM to be marked as synthetic development data.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-12 13:43:10.190778

The planner cannot do anything without a 1RM, and the authored spreadsheet's
maxes are examples rather than measurements (ADR-0003), so local testing had no
input at all. This adds a way to seed one that can never be mistaken for real:

* `one_rep_max.is_synthetic` — the authoritative "is this a real measurement?".
  Defaults to false, so a row is real unless it says otherwise. Mirrors
  `note_chunk.is_synthetic`, which spec 12 uses the same way.
* `source = 'synthetic'` — written by `scripts/seed_dev_one_rms.py` and nothing
  else, so the specific producer is identifiable too.
* `ck_one_rep_max_synthetic_source_is_flagged` — the two cannot disagree.

Autogenerate produced only the `add_column` here: Alembic does not detect
CHECK constraint changes, so both constraints below are hand-written. That is
the same split ADR-0002 recorded — useful for drafting, incomplete for
finishing.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out rather than imported from `enums`, so this migration keeps
# describing the schema as it was at this revision even after the enum grows
# again. A migration is a historical record, not a view onto current code.
_SOURCES_BEFORE = ("tested", "estimated", "imported")
_SOURCES_AFTER = ("tested", "estimated", "imported", "synthetic")

# Wrapped in op.f() at every use, exactly as 0001_init does. Without it the
# `ck_%(table_name)s_%(constraint_name)s` naming convention composes a second
# time and the name becomes ck_one_rep_max_ck_one_rep_max_source_known — which
# renders fine and then fails to match anything at runtime.
_SOURCE_CHECK = "ck_one_rep_max_source_known"
_FLAG_CHECK = "ck_one_rep_max_synthetic_source_is_flagged"


def _one_of(column: str, allowed: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in allowed)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    op.add_column(
        "one_rep_max",
        sa.Column("is_synthetic", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )

    # Widen the value set before anything can write the new value.
    op.drop_constraint(op.f(_SOURCE_CHECK), "one_rep_max", type_="check")
    op.create_check_constraint(
        op.f(_SOURCE_CHECK), "one_rep_max", _one_of("source", _SOURCES_AFTER)
    )

    # An implication rather than a biconditional: `source = 'synthetic'` forces
    # the flag, but a flagged row may carry any source, so a dev can fake a
    # `tested` max to exercise spec 06's test-completion path.
    op.create_check_constraint(
        op.f(_FLAG_CHECK), "one_rep_max", "source <> 'synthetic' OR is_synthetic"
    )


def downgrade() -> None:
    """Reverses the schema. **Not** information-preserving, by necessity.

    Rows with `source = 'synthetic'` cannot satisfy the restored CHECK, so they
    are deleted. They are development data by definition — nothing else may
    write that value — but this is still a destructive step, and it is the
    reason to clear them first with `make seed-dev-1rms ARGS=--clear`.

    `is_synthetic` flags on rows with some *other* source are silently lost
    when the column goes, which is the more insidious half: a faked `tested`
    max survives the downgrade looking real. Check for those before running it.
    """
    op.execute(sa.text("DELETE FROM one_rep_max WHERE source = 'synthetic'"))

    op.drop_constraint(op.f(_FLAG_CHECK), "one_rep_max", type_="check")
    op.drop_constraint(op.f(_SOURCE_CHECK), "one_rep_max", type_="check")
    op.create_check_constraint(
        op.f(_SOURCE_CHECK), "one_rep_max", _one_of("source", _SOURCES_BEFORE)
    )

    op.drop_column("one_rep_max", "is_synthetic")
