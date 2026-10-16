"""Default a new athlete to the step a loaded barbell can actually take.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-13 13:40:00.000000

0001 defaulted `plate_increment_kg` to 1.00 and `rounding` to 'floor' because
that is what the authored workbook does (`FLOOR(x, 1)`), and CONTEXT.md §9
recorded "1 kg floor versus 2.5 kg plate reality" as an open question. It is
now closed, and not in favour of either candidate: plates go on in pairs, so
with 2.5 kg as the smallest plate in the gym the *total* on the bar moves in
5 kg steps. 82.5 kg needs 31.25 kg a side and cannot be loaded at all.

So the default becomes 5.00 / 'nearest'. `nearest` rather than `floor` because
a 5 kg step is coarse enough that always rounding down drifts well below the
percentage the template asked for.

Server defaults only. Existing rows keep whatever they hold: an athlete's
increment is a fact about their gym, and a migration that overwrote it would
also silently invalidate every weight already planned against it (the
`plate_multiple` rule would start firing on a block nobody touched).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("athlete", "plate_increment_kg", server_default=sa.text("5.00"))
    op.alter_column("athlete", "rounding", server_default=sa.text("'nearest'"))


def downgrade() -> None:
    """Back to the workbook's own rule.

    Information-preserving: only the default for rows not yet inserted changes,
    and no existing athlete was rewritten on the way up.
    """
    op.alter_column("athlete", "plate_increment_kg", server_default=sa.text("1.00"))
    op.alter_column("athlete", "rounding", server_default=sa.text("'floor'"))
