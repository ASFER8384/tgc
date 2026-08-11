"""purchase order revision and cancellation

Revision ID: 6cba230ba358
Revises: e9c9a63e291f
Create Date: 2026-08-11 11:28:38.004996
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Text  # noqa: F401
from sqlalchemy.dialects import postgresql  # noqa: F401

# Autogenerate renders custom types by qualified name, so the module has to be
# importable here. It also means the migration carries the JSONB variant and the
# UTC timestamp decorator rather than degrading to plain JSON and naive
# timestamps on Postgres, which is how a hand tidied first migration drifts away
# from the models without anyone noticing.
import sca.models.base  # noqa: F401

revision: str = '6cba230ba358'
down_revision: str | None = 'e9c9a63e291f'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default on revision so the column can be NOT NULL against rows that
    # already exist: every order written before this migration is revision zero.
    op.add_column(
        "purchase_orders",
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("purchase_orders", sa.Column("revision_reason", sa.String(length=200)))
    op.add_column(
        "purchase_orders", sa.Column("cancelled_at", sca.models.base.UTCDateTime())
    )
    op.add_column("purchase_orders", sa.Column("cancel_reason", sa.String(length=200)))


def downgrade() -> None:
    op.drop_column("purchase_orders", "cancel_reason")
    op.drop_column("purchase_orders", "cancelled_at")
    op.drop_column("purchase_orders", "revision_reason")
    op.drop_column("purchase_orders", "revision")
