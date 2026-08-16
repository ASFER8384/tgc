"""the desk called the forecast's own number "typed"

One field held the weekly demand rate and two things wrote it: a person who
knows about a launch, and the forecast run every morning. With no way to tell
them apart the desk reported every figure as manual — so a rate the model had
just written was shown as "typed here, so it holds over the forecast", sending
a buyer to look for whoever typed a number nobody typed.

Existing rows are backfilled to 'model' where the last forecast run claims them
and left null otherwise. Null is "neither has claimed it", which is the honest
answer for a row written before this was recorded — not 'manual', which is the
answer that was wrong in the first place.

Revision ID: d3b7e0a51c94
Revises: c8d1a4f27e60
Create Date: 2026-08-16 12:05:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'd3b7e0a51c94'
down_revision: str | None = 'c8d1a4f27e60'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'stock_snapshots',
        sa.Column('weekly_forecast_source', sa.String(length=16), nullable=True),
    )
    # The audit log already records every figure the forecast wrote, so the
    # existing rows can be claimed from evidence rather than assumed. A row whose
    # current rate is the one the last run wrote is the model's; anything else is
    # left null rather than guessed at.
    op.execute(
        """
        UPDATE stock_snapshots AS s
        SET weekly_forecast_source = 'model'
        FROM (
            SELECT (item ->> 'sku') AS sku, (item ->> 'to')::numeric AS rate
            FROM audit_log a
            CROSS JOIN LATERAL jsonb_array_elements(a.meta -> 'changed') AS item
            WHERE a.action = 'forecast.weekly'
              AND a.created_at = (
                  SELECT MAX(created_at) FROM audit_log WHERE action = 'forecast.weekly'
              )
        ) AS wrote
        WHERE s.sku = wrote.sku AND s.weekly_forecast = wrote.rate
        """
    )


def downgrade() -> None:
    op.drop_column('stock_snapshots', 'weekly_forecast_source')
