"""four weeks for a three week mill and a six week one alike

The reorder point and the cover target were one pair of numbers for the whole
catalogue. Four weeks is late for a mill that takes six and early for one that
takes one; it is thin for a line that arrives in lumps and generous for one that
sells the same amount every week. Lead time, spread and the mill's own minimum
were all already measured, and none of them reached the decision.

These hold what the forecast derives per line, written by the run that already
writes the weekly rate and read by the planner — so the buying half needs no
knowledge of the forecasting half to use them, and the two cannot drift into
disagreeing about a line.

Null where nothing could be derived, and the deployment default governs that
line alone. A guess dressed as a measurement is worse than the constant it
replaces.

Revision ID: e5f9c02a71b8
Revises: d3b7e0a51c94
Create Date: 2026-08-16 12:40:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'e5f9c02a71b8'
down_revision: str | None = 'd3b7e0a51c94'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'stock_snapshots',
        sa.Column('model_reorder_weeks', sa.Numeric(6, 2), nullable=True),
    )
    op.add_column(
        'stock_snapshots',
        sa.Column('model_target_weeks', sa.Numeric(6, 2), nullable=True),
    )
    op.add_column(
        'stock_snapshots',
        sa.Column('model_threshold_basis', sa.String(length=120), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('stock_snapshots', 'model_threshold_basis')
    op.drop_column('stock_snapshots', 'model_target_weeks')
    op.drop_column('stock_snapshots', 'model_reorder_weeks')
